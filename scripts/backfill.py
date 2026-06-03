"""
One-off backfill script.
Classifies existing inbox emails and applies labels. Run locally.

Selects the account by email from accounts.yaml and uses its label profile.

Usage:
    python scripts/backfill.py --account you@gmail.com --days 90 --api-key sk-ant-...
    python scripts/backfill.py --account you@gmail.com --all --api-key sk-ant-...
    python scripts/backfill.py --account you@gmail.com --days 30 --dry-run --api-key sk-ant-...
"""

import argparse
import json
import re
import time
from datetime import datetime, timedelta

import anthropic
from anthropic.types import TextBlock

from appconfig import CLASSIFIER_MODEL, build_gmail_service, find_account, load_config

CLASSIFICATION_PROMPT = """You are an email classifier. Classify this email into exactly one label.

From: {sender}
Subject: {subject}
Body preview: {snippet}

Available labels (choose exactly one):
{labels}

Pick the single best-matching label. Respond with JSON only. No explanation. No markdown.
Format: {{"label": "<label>", "confidence": "high|medium|low"}}"""


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


def get_label_map(service) -> dict[str, str]:
    result = service.users().labels().list(userId="me").execute()
    return {l["name"]: l["id"] for l in result.get("labels", [])}


def classify_email(client, sender, subject, snippet, profile_labels) -> dict:
    labels_block = "\n".join(f"- {l['name']}: {l['description']}" for l in profile_labels)
    prompt = CLASSIFICATION_PROMPT.format(
        sender=sender, subject=subject, snippet=snippet[:300], labels=labels_block
    )
    message = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=256,
        system="Reply with a single JSON object only. No markdown fences or other text.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
    if not raw:
        raise ValueError("No text block in classification response")
    return parse_classification_response(raw)


def fetch_messages(service, days: int | None) -> list:
    query = "in:inbox"
    if days:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        query += f" after:{since}"

    messages, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def already_labelled(msg: dict, our_labels: set) -> bool:
    return bool(set(msg.get("labelIds", [])) & our_labels)


def backfill(email: str, days: int | None, dry_run: bool, api_key: str):
    config = load_config()
    account = find_account(config["accounts"], email)
    profile_labels = config["profiles"][account["profile"]]
    allowed_names = {l["name"] for l in profile_labels}

    print(f"\n{'='*60}")
    print(f"Backfill: {email} (profile '{account['profile']}') | "
          f"{'all time' if not days else f'last {days} days'}")
    print("DRY RUN — no labels will be applied" if dry_run else "LIVE — labels will be applied")
    print("=" * 60)

    service = build_gmail_service(account)
    label_map = get_label_map(service)
    our_label_ids = {label_map[n] for n in allowed_names if n in label_map}
    anthropic_client = anthropic.Anthropic(api_key=api_key)

    print("\nFetching messages from inbox...")
    messages = fetch_messages(service, days)
    print(f"Found {len(messages)} messages to process\n")

    stats = {"processed": 0, "skipped": 0, "errors": 0, "by_label": {}}

    for i, msg_ref in enumerate(messages):
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()

            if already_labelled(msg, our_label_ids):
                stats["skipped"] += 1
                continue

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            sender = headers.get("From", "")
            subject = headers.get("Subject", "(no subject)")
            snippet = msg.get("snippet", "")

            result = classify_email(anthropic_client, sender, subject, snippet, profile_labels)
            label_name = result["label"]
            confidence = result.get("confidence")

            if label_name not in allowed_names:
                print(f"  ⚠️  unknown label '{label_name}' for {subject[:40]} — skipping")
                stats["errors"] += 1
                continue

            if not dry_run:
                label_id = label_map.get(label_name)
                if label_id:
                    service.users().messages().modify(
                        userId="me", id=msg_ref["id"], body={"addLabelIds": [label_id]}
                    ).execute()

            stats["processed"] += 1
            stats["by_label"][label_name] = stats["by_label"].get(label_name, 0) + 1

            if (i + 1) % 10 == 0:
                print(f"  [{i+1}/{len(messages)}] {label_name} ({confidence}) — {subject[:50]}")

            time.sleep(0.1)  # stay well under Gmail API quota

        except KeyboardInterrupt:
            print("\n\nInterrupted by user.")
            break
        except Exception as e:
            stats["errors"] += 1
            print(f"  ⚠️  Error on message {msg_ref['id']}: {e}")
            time.sleep(1)

    print(f"\n{'='*60}\nCOMPLETE")
    print(f"  Processed:  {stats['processed']}")
    print(f"  Skipped:    {stats['skipped']} (already labelled)")
    print(f"  Errors:     {stats['errors']}")
    print("\nLabel breakdown:")
    for label, count in sorted(stats["by_label"].items(), key=lambda x: -x[1]):
        print(f"  {label:<25} {count}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill Gmail labels using Claude")
    parser.add_argument("--account", required=True, help="email address from accounts.yaml")
    parser.add_argument("--days", type=int, default=None, help="only emails from the last N days")
    parser.add_argument("--all", action="store_true", help="process all inbox mail (no date limit)")
    parser.add_argument("--dry-run", action="store_true", help="classify but don't apply labels")
    parser.add_argument("--api-key", required=True, help="Anthropic API key")
    args = parser.parse_args()

    backfill(
        email=args.account,
        days=None if args.all else args.days,
        dry_run=args.dry_run,
        api_key=args.api_key,
    )
