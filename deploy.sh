#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy Project O.R.B.I.T. to Google Cloud Run.
#
# Prerequisites:
#   gcloud auth login && gcloud config set project $GOOGLE_CLOUD_PROJECT
#   ORBIT_API_KEY set in your shell (else the API runs unauthenticated).
#
# First run creates the least-privilege service account:
#   orbit-fleet-sa: roles/datastore.user (Firestore) + roles/aiplatform.user (Gemini)
# =============================================================================

set -euo pipefail

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project <id>'." >&2
  exit 1
fi

SERVICE_NAME="${ORBIT_SERVICE_NAME:-orbit-fleet-commander}"
REGION="${ORBIT_REGION:-us-central1}"
IMAGE_URL="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"
SA_NAME="orbit-fleet-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud CLI is required." >&2; exit 1; }

if [[ -z "${ORBIT_API_KEY:-}" ]]; then
  echo "⚠️  ORBIT_API_KEY is unset — the deployed API will run with authentication DISABLED."
  echo "   Export one before deploying for a fortified demo: export ORBIT_API_KEY=\$(openssl rand -hex 32)"
fi

echo "🚀 Building ${IMAGE_URL} ..."
gcloud builds submit --tag "${IMAGE_URL}"

# --- Least-privilege service account (idempotent) ----------------------------
if [[ -z "$(gcloud iam service-accounts list --filter="email:${SA_EMAIL}" --format='value(email)' 2>/dev/null)" ]]; then
  echo "🔧 Creating service account ${SA_EMAIL} ..."
  gcloud iam service-accounts create "${SA_NAME}" --display-name="O.R.B.I.T. Fleet Commander"
fi
for ROLE in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" --quiet >/dev/null 2>&1 || true
done

# --- Deploy: scale-to-zero, capped burst, API-key protected ------------------
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE_URL}" \
  --platform managed \
  --region "${REGION}" \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 3 \
  --timeout 300 \
  --allow-unauthenticated \
  --set-env-vars "ORBIT_API_KEY=${ORBIT_API_KEY:-},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},ORBIT_MEMORY_BACKEND=auto" \
  --service-account "${SA_EMAIL}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format='value(status.url)')"

echo "✅ Deployment complete!"
echo "🔗 Service URL: ${SERVICE_URL}"
echo ""
echo "Try it:"
cat <<EOF
curl -X POST "${SERVICE_URL}/api/conjunction_alert" \\
  -H "X-API-KEY: \${ORBIT_API_KEY}" \\
  -H "Content-Type: application/json" \\
  -d '{
        "sat_id": "LANCASTER_ORBIT_1",
        "debris_id": "FENGYUN_1C_DEB",
        "alert_source": "SPACE_TRACK_API",
        "priority": "CRITICAL",
        "raw_message": "URGENT: conjunction warning, debris fragment closing on university CubeSat."
      }'
EOF
