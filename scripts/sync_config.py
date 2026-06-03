"""
Sync local config to the cloud.

Validates `taxonomies.yaml` + `accounts.yaml`, uploads the merged config to the
`classifier-config` secret (which the Cloud Functions read at runtime), and
creates any missing Gmail labels for each account's profile.

Run this after editing your labels or accounts — no redeploy needed.

Usage:
    python scripts/sync_config.py
    python scripts/sync_config.py --skip-labels   # config only, don't touch Gmail
"""

import argparse
import sys

import yaml

from appconfig import (
    CONFIG_SECRET,
    PROJECT_ID,
    build_gmail_service,
    load_config,
    upload_secret,
)


def validate(config: dict) -> None:
    profiles = config["profiles"]
    if not config["accounts"]:
        raise SystemExit("accounts.yaml has no accounts")
    for account in config["accounts"]:
        email = account.get("email", "<missing email>")
        if account.get("profile") not in profiles:
            raise SystemExit(f"{email}: profile '{account.get('profile')}' not in taxonomies.yaml")
        if account.get("auth") == "oauth" and not account.get("token_secret"):
            raise SystemExit(f"{email}: oauth accounts need a 'token_secret'")
        if account.get("auth") not in ("oauth", "delegated"):
            raise SystemExit(f"{email}: auth must be 'oauth' or 'delegated'")


def create_missing_labels(config: dict) -> None:
    for account in config["accounts"]:
        wanted = [label["name"] for label in config["profiles"][account["profile"]]]
        print(f"\n{account['email']} — ensuring labels for profile '{account['profile']}'")
        try:
            service = build_gmail_service(account)
        except Exception as e:
            print(f"  ⚠️  could not authenticate ({e}); run setup_auth.py first. Skipping.")
            continue
        existing = {
            l["name"] for l in service.users().labels().list(userId="me").execute().get("labels", [])
        }
        for name in wanted:
            if name in existing:
                print(f"  ✓ {name}")
                continue
            try:
                service.users().labels().create(userId="me", body={"name": name}).execute()
                print(f"  + created {name}")
            except Exception as e:
                print(f"  ✗ could not create '{name}': {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync config to Secret Manager + create labels")
    parser.add_argument("--skip-labels", action="store_true", help="upload config only")
    args = parser.parse_args()

    if not PROJECT_ID:
        sys.exit("GCP_PROJECT is not set — copy .env.example to .env and fill it in")

    config = load_config()
    validate(config)
    print(f"Config valid: {len(config['accounts'])} account(s), {len(config['profiles'])} profile(s)")

    print(f"\nUploading merged config to secret '{CONFIG_SECRET}'...")
    try:
        upload_secret(CONFIG_SECRET, yaml.safe_dump(config, sort_keys=False))
    except Exception as e:
        sys.exit(f"Failed to upload config to '{CONFIG_SECRET}': {e}")

    if not args.skip_labels:
        create_missing_labels(config)

    print("\n✅ Sync complete")


if __name__ == "__main__":
    main()
