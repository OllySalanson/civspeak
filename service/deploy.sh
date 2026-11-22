#!/usr/bin/env bash
# Civspeak's answer service on Google Cloud Run (src/civspeak/answer_service.py).
#
#   service/deploy.sh setup   once: Firestore, its expiry policies, the service account, the image repository
#   service/deploy.sh key     store (or replace) the key from ~/.config/civspeak/answer-service-key as a secret
#   service/deploy.sh         build the image and deploy it (the limits below; raised IDs are kept)
#   service/deploy.sh raise CS-XXXX-XXXX-XXXX 500
#                             give one install a higher daily limit (no code change, no rebuild)
#
# Run from the repository root. The key is never printed: it goes to Secret Manager on standard input.
set -euo pipefail

PROJECT=pixelreel-515501
REGION=europe-west1
SERVICE=civspeak-answers
ACCOUNT="$SERVICE@$PROJECT.iam.gserviceaccount.com"
SECRET=civspeak-answer-key
REPOSITORY=civspeak
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPOSITORY/$SERVICE"
KEY_FILE="$HOME/.config/civspeak/answer-service-key"

# The limits (see answer_service.py): 100 questions a day per computer, 300 per internet address, and
# a hard $18 a month for everyone, under the key's own $20 limit in the Claude Console.
LIMITS="CIVSPEAK_DAILY_QUESTIONS=100,CIVSPEAK_IP_DAILY_QUESTIONS=300,CIVSPEAK_MONTHLY_CAP_USD=18"

g() { gcloud --project "$PROJECT" --quiet "$@"; }

setup() {
  g services enable firestore.googleapis.com run.googleapis.com secretmanager.googleapis.com \
    artifactregistry.googleapis.com cloudbuild.googleapis.com
  g firestore databases describe --database="(default)" >/dev/null 2>&1 ||
    g firestore databases create --database="(default)" --location="$REGION" --type=firestore-native
  # Daily counters carry an `expires` time; Firestore deletes them after it.
  for group in civspeak_installs civspeak_addresses; do
    g firestore fields ttls update expires --collection-group="$group" --enable-ttl --async
  done
  g iam service-accounts describe "$ACCOUNT" >/dev/null 2>&1 ||
    g iam service-accounts create "$SERVICE" --display-name="Civspeak answer service"
  g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$ACCOUNT" --role=roles/datastore.user \
    --condition=None >/dev/null
  g artifacts repositories describe "$REPOSITORY" --location="$REGION" >/dev/null 2>&1 ||
    g artifacts repositories create "$REPOSITORY" --location="$REGION" --repository-format=docker
}

key() {
  [ -s "$KEY_FILE" ] || { echo "No key at $KEY_FILE" >&2; exit 1; }
  if g secrets describe "$SECRET" >/dev/null 2>&1; then
    tr -d '\r\n' <"$KEY_FILE" | g secrets versions add "$SECRET" --data-file=- >/dev/null
  else
    tr -d '\r\n' <"$KEY_FILE" | g secrets create "$SECRET" --replication-policy=automatic --data-file=- >/dev/null
  fi
  g secrets add-iam-policy-binding "$SECRET" --member="serviceAccount:$ACCOUNT" \
    --role=roles/secretmanager.secretAccessor >/dev/null
  echo "The key is stored as the secret $SECRET."
}

deploy() {
  local tag
  tag="$IMAGE:$(git rev-parse --short=12 HEAD)$(git diff --quiet HEAD -- src service pyproject.toml || echo -dirty)"
  g builds submit --region="$REGION" --config=service/cloudbuild.yaml --substitutions="_IMAGE=$tag" .
  g run deploy "$SERVICE" --region="$REGION" --image="$tag" --service-account="$ACCOUNT" \
    --allow-unauthenticated --set-secrets="CIVSPEAK_SERVICE_KEY=$SECRET:latest" --update-env-vars="$LIMITS" \
    --cpu=1 --memory=512Mi --concurrency=40 --min-instances=0 --max-instances=3 --timeout=120
  g run services describe "$SERVICE" --region="$REGION" --format="value(status.url)"
}

raise() {
  local install="${1:?the Civspeak ID}" limit="${2:?the new daily limit}" current
  current=$(g run services describe "$SERVICE" --region="$REGION" \
    --format="value(spec.template.spec.containers[0].env.filter(name=CIVSPEAK_RAISED).extract(value).flatten())" |
    tr -d '\r')
  # One ID=limit per install, separated by spaces (gcloud's lists use commas, so ^|^ sets another separator).
  local kept
  kept=$(tr ' ' '\n' <<<"$current" | grep -v "^${install}=" | grep . | tr '\n' ' ' || true)
  g run services update "$SERVICE" --region="$REGION" --update-env-vars="^|^CIVSPEAK_RAISED=${kept}${install}=${limit}"
}

case "${1:-deploy}" in
  setup) setup ;;
  key) key ;;
  deploy) deploy ;;
  raise) shift; raise "$@" ;;
  *) echo "usage: service/deploy.sh [setup|key|deploy|raise ID LIMIT]" >&2; exit 2 ;;
esac
