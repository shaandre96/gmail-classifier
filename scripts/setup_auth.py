"""
One-time auth + watch setup, driven by accounts.yaml.

For each `oauth` account it opens a browser to authorise Gmail, saves the token
locally, and uploads it to that account's `token_secret` in Secret Manager.
For every account (oauth and delegated) it registers the Gmail push watch so new
mail triggers the classifier.

Run `python scripts/sync_config.py` afterwards to create the Gmail labels and
push the config the Cloud Functions read.

Usage:
    python scripts/setup_auth.py                 # all accounts
    python scripts/setup_auth.py you@gmail.com   # one account
"""

import json
import sys

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from appconfig import (
    CREDENTIALS_FILE,
    PROJECT_ID,
    SCOPES,
    build_gmail_service,
    load_config,
    topic_name,
    token_path,
    upload_secret,
)


def authorise_oauth_account(account: dict) -> None:
    """Browser OAuth flow → local token file → Secret Manager."""
    local = token_path(account)
    if local.exists():
        creds = Credentials.from_authorized_user_info(json.loads(local.read_text()), SCOPES)
        print(f"  reusing existing token {local.name}")
    else:
        if not CREDENTIALS_FILE.exists():
            sys.exit(f"Missing {CREDENTIALS_FILE.name} (OAuth desktop client) — see README")
        print("  opening browser to authorise...")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
        if not isinstance(creds, Credentials):
            raise TypeError("Expected OAuth2 user credentials from the flow")

    local.write_text(creds.to_json())
    upload_secret(account["token_secret"], creds.to_json())


def register_watch(account: dict) -> None:
    service = build_gmail_service(account)
    result = service.users().watch(
        userId="me", body={"labelIds": ["INBOX"], "topicName": topic_name()}
    ).execute()
    print(f"  watch active, expires: {result.get('expiration')}")


def main() -> None:
    if not PROJECT_ID:
        sys.exit("GCP_PROJECT is not set — copy .env.example to .env and fill it in")

    only = sys.argv[1] if len(sys.argv) > 1 else None
    accounts = load_config()["accounts"]
    if only:
        accounts = [a for a in accounts if a["email"].lower() == only.lower()]
        if not accounts:
            sys.exit(f"No account '{only}' in accounts.yaml")

    failures = []
    for account in accounts:
        print(f"\n=== {account['email']} ({account.get('auth')}) ===")
        try:
            if account["auth"] == "oauth":
                authorise_oauth_account(account)
            register_watch(account)
        except Exception as e:
            failures.append(account["email"])
            print(f"  ✗ Failed: {e}")

    if failures:
        sys.exit(f"\n⚠️  Setup failed for: {', '.join(failures)}")
    print("\n✅ Setup complete. Now run: python scripts/sync_config.py")


if __name__ == "__main__":
    main()
