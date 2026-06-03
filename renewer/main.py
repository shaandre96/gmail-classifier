"""
Cloud Function: Gmail Watch Renewer
Triggered by Cloud Scheduler (e.g. every 5 days).
Re-registers Gmail push notifications for every configured account so they don't
expire after 7 days.

Accounts and auth methods come from the `classifier-config` Secret Manager secret
(synced by scripts/sync_config.py) — nothing account-specific is hardcoded here.
"""

import json
import os

import functions_framework
import yaml
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.cloud import secretmanager

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
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def build_gmail_service(account: dict):
    auth = account["auth"]
    if auth == "oauth":
        creds = Credentials.from_authorized_user_info(
            json.loads(get_secret(account["token_secret"])), SCOPES
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("gmail", "v1", credentials=creds)
    if auth == "delegated":
        creds = service_account.Credentials.from_service_account_info(
            json.loads(get_secret(SA_KEY_SECRET)), scopes=SCOPES, subject=account["email"]
        )
        return build("gmail", "v1", credentials=creds)
    raise ValueError(f"Unknown auth method '{auth}' for {account['email']}")


def renew_watch(service, account_name: str):
    result = service.users().watch(
        userId="me", body={"labelIds": ["INBOX"], "topicName": TOPIC}
    ).execute()
    print(f"✅ Watch renewed for {account_name}. Expires: {result.get('expiration')}")


@functions_framework.http
def renew(request):
    """Entry point: triggered by Cloud Scheduler HTTP request."""
    config = yaml.safe_load(get_secret(CONFIG_SECRET))

    for account in config.get("accounts", []):
        try:
            service = build_gmail_service(account)
            renew_watch(service, account["email"])
        except Exception as e:
            print(f"Failed to renew watch for {account['email']}: {e}")

    return "Watch renewal complete", 200
