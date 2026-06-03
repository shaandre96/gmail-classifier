"""
Cloud Function: Gmail Watch Renewer
Triggered by Cloud Scheduler (e.g. every 5 days).
Re-registers Gmail push notifications for every configured account so they don't
expire after 7 days.

Accounts and auth methods come from the `classifier-config` Secret Manager secret
(synced by scripts/sync_config.py) — nothing account-specific is hardcoded here.

Error handling: a config/secret failure raises (HTTP 500) so Cloud Scheduler
records the run as failed. A single account failing is logged and the others are
still renewed; the response reports how many succeeded.
"""

import json
import logging
import os

import functions_framework
import yaml
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.cloud import secretmanager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gmail-watch-renewer")

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "gmail-notifications")
CONFIG_SECRET = "classifier-config"
SA_KEY_SECRET = "service-account-key"
TOPIC = f"projects/{PROJECT_ID}/topics/{PUBSUB_TOPIC}"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]


def get_secret(secret_id: str) -> str:
    if not PROJECT_ID:
        raise RuntimeError("GCP_PROJECT env var is not set; cannot resolve secrets")
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
        return client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Failed to read secret '{secret_id}' from project '{PROJECT_ID}': {e}"
        ) from e


def load_accounts() -> list:
    raw = get_secret(CONFIG_SECRET)
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise RuntimeError(f"Secret '{CONFIG_SECRET}' is not valid YAML: {e}") from e
    if not isinstance(cfg, dict) or "accounts" not in cfg:
        raise RuntimeError(
            f"Secret '{CONFIG_SECRET}' must contain 'accounts' (run scripts/sync_config.py)"
        )
    return cfg["accounts"]


def build_gmail_service(account: dict):
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


def renew_watch(service, account_name: str):
    result = service.users().watch(
        userId="me", body={"labelIds": ["INBOX"], "topicName": TOPIC}
    ).execute()
    log.info("✅ Watch renewed for %s. Expires: %s", account_name, result.get("expiration"))


@functions_framework.http
def renew(request):
    """Entry point: triggered by Cloud Scheduler HTTP request."""
    accounts = load_accounts()  # raises -> HTTP 500 if config can't be read
    if not accounts:
        return "No accounts configured", 200

    succeeded, failed = 0, 0
    for account in accounts:
        email = account.get("email", "<unknown>")
        try:
            service = build_gmail_service(account)
            renew_watch(service, email)
            succeeded += 1
        except Exception as e:
            failed += 1
            log.error("Failed to renew watch for %s: %s", email, e)

    summary = f"Renewed {succeeded}/{len(accounts)} watches ({failed} failed)"
    log.info(summary)
    # If every account failed, signal failure so the scheduler retries/alerts.
    status = 500 if succeeded == 0 else 200
    return summary, status
