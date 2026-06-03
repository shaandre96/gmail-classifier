"""
Cloud Function: Gmail Email Classifier
Triggered by Pub/Sub when a new email arrives.
Fetches the email, classifies it with Claude, applies a Gmail label.

Accounts, auth methods, and label taxonomies are all defined in the
`classifier-config` Secret Manager secret (synced from taxonomies.yaml +
accounts.yaml by scripts/sync_config.py). Nothing about a specific account is
hardcoded here.

Error handling: setup-level failures (bad config, auth, Gmail/Claude API
errors) raise RuntimeError with context so they surface clearly in Cloud
Logging. Per-message failures are logged and skipped so one bad email does not
abort the whole batch.
"""

import base64
import json
import logging
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gmail-classifier")

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
    """Fetch a secret from Secret Manager, with a clear error on failure."""
    if not PROJECT_ID:
        raise RuntimeError("GCP_PROJECT env var is not set; cannot resolve secrets")
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Failed to read secret '{secret_id}' from project '{PROJECT_ID}': {e}"
        ) from e


def load_config() -> dict:
    """Load and cache the merged config (profiles + accounts) from Secret Manager."""
    global _CONFIG
    if _CONFIG is None:
        raw = get_secret(CONFIG_SECRET)
        try:
            cfg = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise RuntimeError(f"Secret '{CONFIG_SECRET}' is not valid YAML: {e}") from e
        if not isinstance(cfg, dict) or "accounts" not in cfg or "profiles" not in cfg:
            raise RuntimeError(
                f"Secret '{CONFIG_SECRET}' must contain 'accounts' and 'profiles' "
                "(run scripts/sync_config.py)"
            )
        _CONFIG = cfg
    return _CONFIG


def find_account(config: dict, email: str) -> dict | None:
    for account in config.get("accounts", []):
        if account.get("email", "").lower() == email.lower():
            return account
    return None


def add_secret_version(secret_id: str, value: str) -> None:
    """Persist a new secret version (best-effort — non-fatal if it fails)."""
    try:
        client = secretmanager.SecretManagerServiceClient()
        client.add_secret_version(
            request={
                "parent": f"projects/{PROJECT_ID}/secrets/{secret_id}",
                "payload": {"data": value.encode("utf-8")},
            }
        )
    except Exception as e:
        # The in-memory creds are still valid for this invocation; just warn.
        log.warning("Could not persist refreshed token to secret '%s': %s", secret_id, e)


def build_gmail_service(account: dict):
    """Build a Gmail service for an account based on its auth method."""
    email = account.get("email", "<unknown>")
    auth = account.get("auth")

    if auth == "oauth":
        token_secret = account.get("token_secret")
        if not token_secret:
            raise RuntimeError(f"Account '{email}' uses auth 'oauth' but has no 'token_secret'")
        try:
            creds = Credentials.from_authorized_user_info(json.loads(get_secret(token_secret)), SCOPES)
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"OAuth token in secret '{token_secret}' for '{email}' is invalid: {e}"
            ) from e
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                raise RuntimeError(f"Failed to refresh OAuth token for '{email}': {e}") from e
            add_secret_version(token_secret, creds.to_json())
        return build("gmail", "v1", credentials=creds)

    if auth == "delegated":
        # Each delegated account may point at its own service-account key secret
        # (one per Workspace domain). Defaults to the shared SA_KEY_SECRET.
        key_secret = account.get("key_secret", SA_KEY_SECRET)
        try:
            creds = service_account.Credentials.from_service_account_info(
                json.loads(get_secret(key_secret)), scopes=SCOPES, subject=email
            )
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"Service-account key in secret '{key_secret}' for '{email}' is invalid: {e}"
            ) from e
        return build("gmail", "v1", credentials=creds)

    raise RuntimeError(
        f"Account '{email}' has unknown auth method '{auth}' (expected 'oauth' or 'delegated')"
    )


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
    try:
        labels = service.users().labels().list(userId="me").execute()
    except Exception as e:
        raise RuntimeError(f"Failed to list Gmail labels: {e}") from e
    for label in labels.get("labels", []):
        if label["name"] == label_name:
            return label["id"]
    return None


def classify_email(sender: str, subject: str, snippet: str, profile_labels: list) -> dict:
    """Send email metadata to Claude for classification against a profile's labels."""
    client = anthropic.Anthropic(api_key=get_secret(ANTHROPIC_API_KEY_SECRET))

    labels_block = "\n".join(f"- {l['name']}: {l['description']}" for l in profile_labels)
    prompt = CLASSIFICATION_PROMPT.format(
        sender=sender, subject=subject, snippet=snippet[:300], labels=labels_block
    )

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=256,
            system="Reply with a single JSON object only. No markdown fences or other text.",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"Claude classification request failed (model '{MODEL}'): {e}") from e

    raw = "".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
    if not raw:
        raise ValueError("No text block in classification response")
    return parse_classification_response(raw)


def process_one_message(service, msg_id: str, profile_labels: list, allowed: set) -> None:
    """Fetch, classify, and label a single message. Raises on any failure."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except Exception as e:
        raise RuntimeError(f"Failed to fetch message {msg_id}: {e}") from e

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    sender = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    snippet = msg.get("snippet", "")
    log.info("Processing %s | %.60s | from %.40s", msg_id, subject, sender)

    result = classify_email(sender, subject, snippet, profile_labels)
    label_name = result["label"]
    log.info("  → %s (%s)", label_name, result.get("confidence"))

    if label_name not in allowed:
        log.warning("  Model returned unknown label '%s' — leaving %s unlabelled", label_name, msg_id)
        return

    label_id = get_label_id(service, label_name)
    if not label_id:
        log.warning("  Label '%s' not found in Gmail (run sync_config.py) — %s", label_name, msg_id)
        return

    try:
        service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [label_id]}
        ).execute()
    except Exception as e:
        raise RuntimeError(f"Failed to apply label '{label_name}' to {msg_id}: {e}") from e
    log.info("  Applied %s to %s", label_name, msg_id)


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
        raise RuntimeError(f"Failed to fetch Gmail history from id {history_id}: {e}") from e

    changes = history.get("history", [])
    if not changes:
        log.info("No new inbox messages to process.")
        return

    processed = errors = 0
    for record in changes:
        for msg_added in record.get("messagesAdded", []):
            msg_id = msg_added.get("message", {}).get("id")
            if not msg_id:
                continue
            try:
                process_one_message(service, msg_id, profile_labels, allowed)
                processed += 1
            except Exception as e:
                # Keep going — one bad message must not abort the batch.
                errors += 1
                log.exception("Skipping message %s after error: %s", msg_id, e)

    log.info("Batch complete: %d processed, %d errors", processed, errors)


@functions_framework.cloud_event
def classify(cloud_event):
    """Entry point: triggered by Pub/Sub when Gmail sends a push notification."""
    try:
        pubsub_data = base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        notification = json.loads(pubsub_data)
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"Malformed Pub/Sub event payload: {e}") from e

    email_address = notification.get("emailAddress", "")
    history_id = str(notification.get("historyId", ""))
    if not email_address or not history_id:
        raise RuntimeError(f"Notification missing emailAddress/historyId: {notification!r}")
    log.info("Notification for %s (historyId %s)", email_address, history_id)

    config = load_config()
    account = find_account(config, email_address)
    if account is None:
        # Not an error — we simply don't manage this address.
        log.warning("No configured account for %s — ignoring", email_address)
        return

    profile = account.get("profile")
    if profile not in config.get("profiles", {}):
        raise RuntimeError(f"Account '{email_address}' references unknown profile '{profile}'")

    service = build_gmail_service(account)
    process_new_emails(service, history_id, config["profiles"][profile])
