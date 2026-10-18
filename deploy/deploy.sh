#!/usr/bin/env bash
# Deploy DecisionOS to Cloud Run with Firestore, Vertex AI Gemini and Firebase Authentication.
# Usage: PROJECT_ID=my-project FIREBASE_API_KEY=AIza... ./deploy/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-decisionos}"
FIRESTORE_LOCATION="${FIRESTORE_LOCATION:-asia-south1}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
SA_NAME="${SERVICE}-run"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BASE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com aiplatform.googleapis.com secretmanager.googleapis.com \
  firebase.googleapis.com identitytoolkit.googleapis.com

echo "==> Firestore (Native mode)"
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' --location="$FIRESTORE_LOCATION" --type=firestore-native
fi
for group in sessions auth_tokens oauth invitations rate_limits; do
  gcloud firestore fields ttls update expires_at --collection-group="$group" --enable-ttl --async >/dev/null 2>&1 || true
done

echo "==> Secrets"
create_secret() {
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic
  fi
}
create_secret decisionos-flask-secret "$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
create_secret decisionos-encryption-key "$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"

echo "==> Service account"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name="DecisionOS Cloud Run"
for role in roles/datastore.user roles/aiplatform.user roles/secretmanager.secretAccessor roles/firebaseauth.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role" --condition=None >/dev/null
done

echo "==> Cloud Run deploy ($BASE_URL)"
ENV_VARS="COS_ENV=production,COS_BASE_URL=${BASE_URL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global"
ENV_VARS+=",GOOGLE_GENAI_USE_VERTEXAI=true,GEMINI_MODEL=${GEMINI_MODEL},COS_WORKER_INLINE=1"
[ -n "${FIREBASE_API_KEY:-}" ] && ENV_VARS+=",FIREBASE_API_KEY=${FIREBASE_API_KEY}"
[ -n "${EXTRA_ENV_VARS:-}" ] && ENV_VARS+=",${EXTRA_ENV_VARS}"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --cpu 1 --memory 1Gi --timeout 300 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "FLASK_SECRET_KEY=decisionos-flask-secret:latest,COS_ENCRYPTION_KEY=decisionos-encryption-key:latest"

LEGACY_HOST="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)' | sed 's#https://##')"
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "COS_ALLOWED_HOSTS=${LEGACY_HOST}" >/dev/null

echo
echo "Deployed: ${BASE_URL}"
echo "Health:   ${BASE_URL}/health"
echo "Add ${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app and ${LEGACY_HOST} to Firebase Authentication > Settings > Authorized domains."
