"""
Cloud Function: Gmail Email Classifier
Triggered by Pub/Sub when a new email arrives.
Fetches the email, classifies it with Claude, applies a Gmail label.

Accounts, auth methods, and label taxonomies are all defined in the
`classifier-config` Secret Manager secret (synced from taxonomies.yaml +
accounts.yaml by scripts/sync_config.py). Nothing about a specific account is
hardcoded here.
"""

import base64
import json
import os
import re

import functions_framework
import yaml
import anthropic
from anthropic.types import TextBlock
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.cloud import secretmanager

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
CONFIG_SECRET = "classifier-config"
SA_KEY_SECRET = "service-account-key"
ANTHROPIC_API_KEY_SECRET = "anthropic-api-key"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

CLASSIFICATION_PROMPT = """You are an email classifier. Classify this email into exactly one label.

From: {sender}
Subject: {subject}
Body preview: {snippet}

Available labels (choose exactly one):
{labels}

Pick the single best-matching label. Respond with JSON only. No explanation. No markdown.
Format: {{"label": "<label>", "confidence": "high|medium|low"}}"""

_CONFIG = None


def get_secret(secret_id: str) -> str:
    """Fetch a secret from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def load_config() -> dict:
    """Load and cache the merged config (profiles + accounts) from Secret Manager."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = yaml.safe_load(get_secret(CONFIG_SECRET))
    return _CONFIG


def find_account(config: dict, email: str) -> dict | None:
    for account in config.get("accounts", []):
        if account["email"].lower() == email.lower():
            return account
    return None


def add_secret_version(secret_id: str, value: str) -> None:
    client = secretmanager.SecretManagerServiceClient()
    client.add_secret_version(
        request={
            "parent": f"projects/{PROJECT_ID}/secrets/{secret_id}",
            "payload": {"data": value.encode("utf-8")},
        }
    )


def build_gmail_service(account: dict):
    """Build a Gmail service for an account based on its auth method."""
    auth = account["auth"]
    if auth == "oauth":
        token_secret = account["token_secret"]
        creds = Credentials.from_authorized_user_info(json.loads(get_secret(token_secret)), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            add_secret_version(token_secret, creds.to_json())  # persist refreshed token
        return build("gmail", "v1", credentials=creds)
    if auth == "delegated":
        creds = service_account.Credentials.from_service_account_info(
            json.loads(get_secret(SA_KEY_SECRET)), scopes=SCOPES, subject=account["email"]
        )
        return build("gmail", "v1", credentials=creds)
    raise ValueError(f"Unknown auth method '{auth}' for {account['email']}")


def parse_classification_response(raw: str) -> dict:
    """Parse Claude's reply into {label, confidence}, tolerating fences or extra text."""
    text = raw.strip()
    if not text:
        raise ValueError("Empty classification response")

    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"Could not parse classification JSON: {text[:200]!r}") from None
        data = json.loads(text[start : end + 1])

    if not isinstance(data, dict) or "label" not in data:
        raise ValueError(f"Invalid classification payload: {data!r}")
    return data


def get_label_id(service, label_name: str) -> str | None:
    """Get the Gmail label ID for a given label name."""
    labels = service.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def classify_email(sender: str, subject: str, snippet: str, profile_labels: list) -> dict:
    """Send email metadata to Claude for classification against a profile's labels."""
    api_key = get_secret(ANTHROPIC_API_KEY_SECRET)
    client = anthropic.Anthropic(api_key=api_key)

    labels_block = "\n".join(f"- {l['name']}: {l['description']}" for l in profile_labels)
    prompt = CLASSIFICATION_PROMPT.format(
        sender=sender, subject=subject, snippet=snippet[:300], labels=labels_block
    )

    message = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system="Reply with a single JSON object only. No markdown fences or other text.",
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
    if not raw:
        raise ValueError("No text block in classification response")
    return parse_classification_response(raw)


def process_new_emails(service, history_id: str, profile_labels: list):
    """Fetch and classify emails added since the last historyId."""
    allowed = {l["name"] for l in profile_labels}
    try:
        history = service.users().history().list(
            userId="me",
            startHistoryId=history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
    except Exception as e:
        print(f"History fetch error: {e}")
        return

    changes = history.get("history", [])
    if not changes:
        print("No new inbox messages to process.")
        return

    for record in changes:
        for msg_added in record.get("messagesAdded", []):
            msg_id = msg_added["message"]["id"]

            msg = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            sender = headers.get("From", "")
            subject = headers.get("Subject", "(no subject)")
            snippet = msg.get("snippet", "")

            print(f"Processing: {subject[:60]} | From: {sender[:40]}")

            try:
                result = classify_email(sender, subject, snippet, profile_labels)
                label_name = result["label"]
                print(f"  → {label_name} ({result.get('confidence')})")
            except Exception as e:
                print(f"  Classification error: {e} — leaving unlabelled")
                continue

            if label_name not in allowed:
                print(f"  Model returned unknown label '{label_name}' — leaving unlabelled")
                continue

            label_id = get_label_id(service, label_name)
            if label_id:
                service.users().messages().modify(
                    userId="me", id=msg_id, body={"addLabelIds": [label_id]}
                ).execute()
                print(f"  Label applied: {label_name}")
            else:
                print(f"  Label not found in Gmail: {label_name} (run sync_config.py)")


@functions_framework.cloud_event
def classify(cloud_event):
    """Entry point: triggered by Pub/Sub when Gmail sends a push notification."""
    pubsub_data = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
    notification = json.loads(pubsub_data)

    email_address = notification.get("emailAddress", "")
    history_id = str(notification.get("historyId", ""))
    print(f"Notification for: {email_address} | historyId: {history_id}")

    config = load_config()
    account = find_account(config, email_address)
    if account is None:
        print(f"No configured account for {email_address}")
        return

    service = build_gmail_service(account)
    profile_labels = config["profiles"][account["profile"]]
    process_new_emails(service, history_id, profile_labels)
