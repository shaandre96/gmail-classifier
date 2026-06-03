#!/bin/bash
set -euo pipefail

# Load deployment config from .env (copy .env.example to .env first).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "Missing .env — copy .env.example to .env and fill it in." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"
set +a

: "${GCP_PROJECT:?GCP_PROJECT not set in .env}"
: "${REGION:?REGION not set in .env}"
: "${SERVICE_ACCOUNT:?SERVICE_ACCOUNT not set in .env}"
: "${PUBSUB_TOPIC:?PUBSUB_TOPIC not set in .env}"
: "${CLASSIFIER_MODEL:=claude-haiku-4-5-20251001}"
: "${MAX_MESSAGES:=5}"

deploy_classifier() {
  echo "Deploying classifier..."
  gcloud functions deploy gmail-classifier \
    --gen2 \
    --runtime=python311 \
    --region="$REGION" \
    --source="$SCRIPT_DIR/classifier" \
    --entry-point=classify \
    --trigger-topic="$PUBSUB_TOPIC" \
    --service-account="$SERVICE_ACCOUNT" \
    --set-env-vars="GCP_PROJECT=$GCP_PROJECT,CLASSIFIER_MODEL=$CLASSIFIER_MODEL,MAX_MESSAGES=$MAX_MESSAGES" \
    --memory=256MB \
    --timeout=60s \
    --min-instances=0 \
    --max-instances=10
  echo "✅ Classifier deployed"
}

deploy_renewer() {
  echo "Deploying renewer..."
  gcloud functions deploy gmail-watch-renewer \
    --gen2 \
    --runtime=python311 \
    --region="$REGION" \
    --source="$SCRIPT_DIR/renewer" \
    --entry-point=renew \
    --trigger-http \
    --service-account="$SERVICE_ACCOUNT" \
    --set-env-vars="GCP_PROJECT=$GCP_PROJECT,PUBSUB_TOPIC=$PUBSUB_TOPIC" \
    --memory=256MB \
    --timeout=30s
  echo "✅ Renewer deployed"
}

case "${1:-}" in
  classifier) deploy_classifier ;;
  renewer)    deploy_renewer ;;
  all)        deploy_classifier && deploy_renewer ;;
  *)          echo "Usage: ./deploy.sh [classifier|renewer|all]" ;;
esac
