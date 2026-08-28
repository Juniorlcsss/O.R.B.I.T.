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
WEB_SERVICE_NAME="${ORBIT_WEB_SERVICE_NAME:-orbit-command-center}"
REGION="${ORBIT_REGION:-us-central1}"
VERTEX_LOCATION="${ORBIT_VERTEX_LOCATION:-global}"
MEDIA_LOCATION="${ORBIT_MEDIA_LOCATION:-us-central1}"
IMAGE_URL="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"
WEB_IMAGE_URL="gcr.io/${PROJECT_ID}/${WEB_SERVICE_NAME}:latest"
SA_NAME="orbit-fleet-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud CLI is required." >&2; exit 1; }

if [[ -z "${ORBIT_API_KEY:-}" ]]; then
  if [[ "${ORBIT_ALLOW_UNAUTHENTICATED_API:-}" == "1" ]]; then
    echo "⚠️  ORBIT_API_KEY unset and ORBIT_ALLOW_UNAUTHENTICATED_API=1 — deploying WITHOUT auth."
  else
    echo "ERROR: ORBIT_API_KEY is unset. The service would deploy publicly with" >&2
    echo "       authentication DISABLED. Generate one and retry:" >&2
    echo "         export ORBIT_API_KEY=\$(openssl rand -hex 32)" >&2
    echo "       To deploy unauthenticated anyway: export ORBIT_ALLOW_UNAUTHENTICATED_API=1" >&2
    exit 1
  fi
fi

if [[ -z "${ORBIT_COMMAND_SIGNING_KEY:-}" ]]; then
  echo "⚠️  ORBIT_COMMAND_SIGNING_KEY unset — manoeuvre commands will be signed with a"
  echo "    per-process key that no other replica can verify. Set one with:"
  echo "      export ORBIT_COMMAND_SIGNING_KEY=\$(openssl rand -hex 32)"
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
  --set-env-vars "^@^ORBIT_API_KEY=${ORBIT_API_KEY:-}@GOOGLE_CLOUD_PROJECT=${PROJECT_ID}@GOOGLE_GENAI_USE_VERTEXAI=TRUE@GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}@ORBIT_MEDIA_LOCATION=${MEDIA_LOCATION}@ORBIT_MEMORY_BACKEND=auto@ORBIT_COMMAND_SIGNING_KEY=${ORBIT_COMMAND_SIGNING_KEY:-}" \
  --service-account "${SA_EMAIL}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format='value(status.url)')"

# --- Frontend: static bundle + authenticating reverse proxy -----------------
if [[ "${ORBIT_SKIP_FRONTEND:-}" == "1" ]]; then
  echo "⏭️  ORBIT_SKIP_FRONTEND=1 — backend only."
  WEB_URL=""
else
  echo "🚀 Building ${WEB_IMAGE_URL} ..."
  BUILD_CFG="$(mktemp -t orbit-web-cloudbuild.XXXXXX.yaml)"
  trap 'rm -f "${BUILD_CFG}"' EXIT
  cat >"${BUILD_CFG}" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "Dockerfile.frontend", "-t", "${WEB_IMAGE_URL}", "."]
images: ["${WEB_IMAGE_URL}"]
YAML
  gcloud builds submit --config "${BUILD_CFG}" .

  gcloud run deploy "${WEB_SERVICE_NAME}"     --image "${WEB_IMAGE_URL}"     --platform managed     --region "${REGION}"     --port 8080     --memory 512Mi     --cpu 1     --min-instances 0     --max-instances 3     --allow-unauthenticated     --set-env-vars "^@^BACKEND_URL=${SERVICE_URL}@ORBIT_API_KEY=${ORBIT_API_KEY:-}"

  WEB_URL="$(gcloud run services describe "${WEB_SERVICE_NAME}" --platform managed --region "${REGION}" --format='value(status.url)')"


  echo "🔒 Restricting backend CORS to ${WEB_URL} ..."
  gcloud run services update "${SERVICE_NAME}"     --platform managed --region "${REGION}"     --update-env-vars "ORBIT_CORS_ORIGINS=${WEB_URL}" >/dev/null
fi

echo "✅ Deployment complete!"
echo "🔗 Backend URL:  ${SERVICE_URL}"
[[ -n "${WEB_URL}" ]] && echo "🛰️  Command center: ${WEB_URL}"
echo ""
echo "Try it:"
cat <<EOF
curl -X POST "${SERVICE_URL}/api/conjunction_alert" \\
  -H "X-API-KEY: \${ORBIT_API_KEY}" \\
  -H "Content-Type: application/json" \\
  -d '{
        "sat_id": "<protagonist id>",
        "debris_id": "<counterparty id>",
        "alert_source": "SPACE_TRACK_API",
        "priority": "CRITICAL",
        "raw_message": "URGENT: conjunction warning from the public CDM feed."
      }'

# Object ids are not fixed: the protected asset is whichever real payload
# currently faces the worst approach. Discover the pair first with
#   curl "${SERVICE_URL}/api/live_protagonist" -H "X-API-KEY: \${ORBIT_API_KEY}"
EOF
