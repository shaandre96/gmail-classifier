"""
Shared helpers for the local scripts (sync_config, setup_auth, backfill).

Loads `.env`, reads the local `taxonomies.yaml` + `accounts.yaml`, and builds an
authenticated Gmail service for an account (OAuth user token or Workspace
domain-wide delegation). Designed to be imported when a script is run as
`python scripts/<name>.py` (the scripts/ directory is on sys.path).
"""

import json
import os
import re
from pathlib import Path

import yaml
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.cloud import secretmanager

ROOT = Path(__file__).resolve().parent.parent
TAXONOMIES_FILE = ROOT / "taxonomies.yaml"
ACCOUNTS_FILE = ROOT / "accounts.yaml"
ENV_FILE = ROOT / ".env"
CREDENTIALS_FILE = ROOT / "credentials.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

SA_KEY_SECRET = "service-account-key"
CONFIG_SECRET = "classifier-config"


def load_env(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE pairs from .env into the environment (without overriding)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
PUBSUB_TOPIC = os.environ.get("PUBSUB_TOPIC", "gmail-notifications")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")


def topic_name() -> str:
    return f"projects/{PROJECT_ID}/topics/{PUBSUB_TOPIC}"


def load_config() -> dict:
    """Return {'profiles': {...}, 'accounts': [...]} from the local YAML files."""
    profiles = yaml.safe_load(TAXONOMIES_FILE.read_text())["profiles"]
    accounts = yaml.safe_load(ACCOUNTS_FILE.read_text())["accounts"]
    return {"profiles": profiles, "accounts": accounts}


def find_account(accounts: list, email: str) -> dict:
    for account in accounts:
        if account["email"].lower() == email.lower():
            return account
    raise SystemExit(f"No account '{email}' found in accounts.yaml")


def slugify(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")


def token_path(account: dict) -> Path:
    return ROOT / f"token_{slugify(account['email'])}.json"


def get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode("utf-8")


def upload_secret(secret_id: str, value: str) -> None:
    """Create the secret if needed, then add a new version."""
    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{PROJECT_ID}"
    try:
        client.create_secret(
            request={
                "parent": parent,
                "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}}},
            }
        )
        print(f"  secret {secret_id} created")
    except Exception:
        pass  # already exists
    client.add_secret_version(
        request={
            "parent": f"{parent}/secrets/{secret_id}",
            "payload": {"data": value.encode("utf-8")},
        }
    )
    print(f"  secret {secret_id} version added")


def build_gmail_service(account: dict):
    """Build an authenticated Gmail service for an account (no interactive flow)."""
    auth = account["auth"]
    if auth == "oauth":
        local = token_path(account)
        if local.exists():
            creds = Credentials.from_authorized_user_info(json.loads(local.read_text()), SCOPES)
        else:
            creds = Credentials.from_authorized_user_info(
                json.loads(get_secret(account["token_secret"])), SCOPES
            )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            local.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)
    if auth == "delegated":
        creds = service_account.Credentials.from_service_account_info(
            json.loads(get_secret(SA_KEY_SECRET)), scopes=SCOPES, subject=account["email"]
        )
        return build("gmail", "v1", credentials=creds)
    raise ValueError(f"Unknown auth method '{auth}' for {account['email']}")
