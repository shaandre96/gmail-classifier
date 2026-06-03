# Gmail Classifier

Serverless Gmail auto-labeller. When a new email lands in an inbox, Gmail pushes a
notification to Pub/Sub, a Cloud Function fetches the message metadata, asks
**Claude** to classify it into a single label, and applies that label back to the
message in Gmail.

Everything account-specific — which addresses to manage, how to authenticate them,
and the label taxonomy each one uses — is **configuration**, so you can fork this
and point it at your own accounts without touching the code.

It supports two kinds of account out of the box:

| Auth method | Use for | How it authenticates |
| --- | --- | --- |
| `oauth` | a personal Gmail account | OAuth user token (authorised once in a browser, stored in Secret Manager) |
| `delegated` | a Google Workspace account | service-account domain-wide delegation (no browser) |

## How it works

```
                ┌──────────────┐   push    ┌────────────────────────┐
  New email ───▶│  Gmail watch │──────────▶│ Pub/Sub                │
                └──────────────┘           │ topic: $PUBSUB_TOPIC   │
                                           └───────────┬────────────┘
                                                       │ trigger
                                                       ▼
                                        ┌──────────────────────────┐
                                        │ Cloud Function: classify  │
                                        │  1. load config (secret)  │
                                        │  2. route by email addr   │
                                        │  3. fetch new message(s)  │
                                        │  4. Claude → label        │
                                        │  5. apply Gmail label     │
                                        └──────────────────────────┘

  Cloud Scheduler ──(every 5 days, HTTP)──▶ Cloud Function: renew
                                            re-registers every account's watch
                                            (Gmail watches expire after 7 days)
```

Gmail push subscriptions expire after 7 days, so a second Cloud Function
(`gmail-watch-renewer`) is invoked by Cloud Scheduler every 5 days to call
`users.watch()` again for every configured account.

## Configuration

Three files drive everything. The `*.example` files are committed templates; copy
each to its real (gitignored) name and fill it in.

| File | Committed | Purpose |
| --- | --- | --- |
| `taxonomies.yaml` ← `taxonomies.example.yaml` | local only | label **profiles** — named sets of labels + descriptions the model classifies into |
| `accounts.yaml` ← `accounts.example.yaml` | local only | which email addresses to manage, their auth method, and which profile each uses |
| `.env` ← `.env.example` | local only | deploy-time vars: project, region, service account, topic, model |

`taxonomies.yaml` — a profile is a reusable label set; label `name` must match the
Gmail label (it gets created for you), and `description` is what the model reads:

```yaml
profiles:
  personal:
    - { name: Important, description: "time-sensitive or needs action — bills, deadlines, alerts" }
    - { name: Finance,   description: "bank/card statements, tax, invoices, receipts" }
    - { name: Noise,     description: "promotions, marketing, cold outreach" }
  business:
    - { name: Business/Lead,   description: "potential client enquiries / new business" }
    - { name: Business/Client, description: "emails from known or active clients" }
```

`accounts.yaml` — map each address to an auth method and a profile:

```yaml
accounts:
  - { email: you@gmail.com,       auth: oauth,     profile: personal, token_secret: gmail-token-you }
  - { email: you@yourcompany.com, auth: delegated, profile: business }
```

Edit either file, then run `python scripts/sync_config.py` — it validates the
config, uploads it to the `classifier-config` secret the functions read, and
creates any missing Gmail labels. **No redeploy needed to change labels.**

## Repository layout

```
classifier/      Cloud Function — classifies & labels incoming mail (Pub/Sub trigger)
renewer/         Cloud Function — renews each account's Gmail watch (HTTP, scheduled)
scripts/
  appconfig.py   Shared local helpers (config loading, Gmail auth)
  setup_auth.py  One-time: OAuth flow (oauth accounts) + register Gmail watch
  sync_config.py Validate config → upload to Secret Manager → create Gmail labels
  backfill.py    One-off: classify & label existing inbox mail
deploy.sh        Deploy either/both Cloud Functions (reads .env)
*.example.yaml   Config templates
```

## GCP services required

- **Cloud Functions (gen2)** — hosts the `classify` and `renew` functions (gen2 also
  uses Cloud Run + Artifact Registry + Cloud Build under the hood).
- **Cloud Pub/Sub** — the topic Gmail publishes to; the classifier is deployed with
  `--trigger-topic`.
- **Cloud Scheduler** — invokes the renewer over HTTP every ~5 days.
- **Secret Manager** — stores the Anthropic key, the merged config, per-account OAuth
  tokens, and (for delegated accounts) the service-account key.
- **Gmail API** — enabled on the project.

```bash
gcloud services enable \
  cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com pubsub.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com gmail.googleapis.com
```

## IAM permissions

### Runtime service account

Both functions run as a dedicated service account (`$SERVICE_ACCOUNT` in `.env`).
Grant it:

| Role | Why |
| --- | --- |
| `roles/secretmanager.secretAccessor` | read the config, Anthropic key, tokens, SA key |
| `roles/secretmanager.secretVersionAdder` | classifier writes back refreshed OAuth tokens |
| `roles/run.invoker` | allow the Pub/Sub trigger + Scheduler to invoke the functions |

```bash
SA="gmail-classifier-sa@<project>.iam.gserviceaccount.com"
for ROLE in roles/secretmanager.secretAccessor \
            roles/secretmanager.secretVersionAdder \
            roles/run.invoker; do
  gcloud projects add-iam-policy-binding <project> \
    --member="serviceAccount:$SA" --role="$ROLE"
done
```

### Gmail → Pub/Sub publish rights

Gmail's push service must be allowed to publish to your topic. Grant the Gmail
system service account the Publisher role on the topic:

```bash
gcloud pubsub topics add-iam-policy-binding <topic> \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

### Workspace accounts — domain-wide delegation

For each `delegated` account, authorise the service account's client ID in the
Google Workspace Admin console (Security → API controls → Domain-wide delegation)
for these scopes:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.labels`

Store the service-account key JSON in the `service-account-key` secret.

### Deployer (you)

To run `deploy.sh` and the setup scripts you need (at least)
`roles/cloudfunctions.developer`, `roles/iam.serviceAccountUser` (to deploy as the
runtime SA), `roles/secretmanager.admin`, `roles/pubsub.editor`, and
`roles/cloudscheduler.admin`.

## Secrets (Secret Manager)

| Secret ID | Contents | Created by |
| --- | --- | --- |
| `anthropic-api-key` | Anthropic API key for Claude | you, once |
| `classifier-config` | merged `taxonomies.yaml` + `accounts.yaml` | `sync_config.py` |
| `<token_secret>` (per oauth account) | that account's OAuth token | `setup_auth.py` |
| `service-account-key` | SA key JSON for delegated accounts | you, once |

## Setup

1. **Create GCP prerequisites** — project, the runtime service account, the Pub/Sub
   topic, enable the services above, and apply the IAM bindings. Add the
   `anthropic-api-key` (and, if you use delegated accounts, `service-account-key`)
   secrets.

2. **OAuth client** — create an OAuth 2.0 *Desktop app* client in the GCP console and
   download it as `credentials.json` in the repo root (used to authorise `oauth`
   accounts in a browser).

3. **Fill in config:**

   ```bash
   cp .env.example .env                          # project, region, SA, topic, model
   cp accounts.example.yaml accounts.yaml        # your addresses + auth + profile
   cp taxonomies.example.yaml taxonomies.yaml    # your label profiles
   python -m venv .venv && source .venv/bin/activate
   pip install -r scripts/requirements.txt
   ```

4. **Authorise accounts & register watches:**

   ```bash
   python scripts/setup_auth.py        # browser opens for each oauth account
   ```

5. **Push config & create labels:**

   ```bash
   python scripts/sync_config.py
   ```

6. **Deploy the functions:**

   ```bash
   ./deploy.sh all                     # or: ./deploy.sh classifier | renewer
   ```

7. **Schedule the renewer** — point Cloud Scheduler at the renewer's HTTP URL every
   5 days:

   ```bash
   gcloud scheduler jobs create http gmail-watch-renew \
     --schedule="0 6 */5 * *" \
     --uri="<renewer-trigger-url>" \
     --http-method=GET \
     --oidc-service-account-email="$SERVICE_ACCOUNT"
   ```

## Changing your labels later

Edit `taxonomies.yaml` (or `accounts.yaml`) and run `python scripts/sync_config.py`.
The functions pick up the new config on their next cold start — no redeploy.

## Backfilling existing mail

Classify and label mail already in the inbox. Selects the account by email from
`accounts.yaml`:

```bash
# Preview only — classify but don't apply labels
python scripts/backfill.py --account you@gmail.com --days 90 --dry-run --api-key sk-ant-...

# Apply labels to the last 90 days
python scripts/backfill.py --account you@gmail.com --days 90 --api-key sk-ant-...

# Apply to all inbox mail
python scripts/backfill.py --account you@gmail.com --all --api-key sk-ant-...
```

It skips messages that already carry one of the profile's labels and rate-limits
itself to stay within Gmail API quota.

## Security notes

`credentials.json`, OAuth tokens (`token_*.json`), `.env`, `accounts.yaml`,
`taxonomies.yaml`, and any other `*.json` are git-ignored and must **never** be
committed — they hold your credentials and email addresses. Only the `*.example`
templates are tracked. Secrets live in Secret Manager and on your machine for setup.
