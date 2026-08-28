#!/usr/bin/env bash
# =============================================================================
# deploy.sh: deploy Project O.R.B.I.T. to Google Cloud Run.
#
# Prerequisites:
#   gcloud auth login && gcloud config set project $GOOGLE_CLOUD_PROJECT
#   ORBIT_API_KEY, SPACETRACK_USERNAME, SPACETRACK_PASSWORD in your shell.
#
# Deploys TWO scale-to-zero services:
#   orbit-fleet-commander: the ADK agent API
#   orbit-command-center:  the console. It proxies /api and attaches the API
#                          key server-side, so the browser never holds one.
#
# Two service accounts, each holding only what it needs:
#   orbit-fleet-sa: datastore.user + aiplatform.user, and read access to the
#                   three secrets below.
#   orbit-web-sa:   read access to the API key, nothing more. The console has
#                   no business reaching Firestore or Vertex AI.
#
# Three values travel through Secret Manager instead of --set-env-vars.
# Anyone holding roles/viewer can read a service's environment variables, and
# `gcloud run services describe` prints them in full:
#   orbit-api-key, orbit-spacetrack-password, orbit-command-signing-key
#
# Escape hatches: ORBIT_SKIP_FRONTEND=1 (API only),
# ORBIT_ALLOW_UNAUTHENTICATED_API=1, ORBIT_ALLOW_SIMULATED_DATA=1.
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
FIRESTORE_LOCATION="${ORBIT_FIRESTORE_LOCATION:-nam5}"
IMAGE_URL="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"
WEB_IMAGE_URL="gcr.io/${PROJECT_ID}/${WEB_SERVICE_NAME}:latest"
SA_NAME="orbit-fleet-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
WEB_SA_NAME="orbit-web-sa"
WEB_SA_EMAIL="${WEB_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

SECRET_API_KEY="orbit-api-key"
SECRET_SPACETRACK_PW="orbit-spacetrack-password"
SECRET_SIGNING_KEY="orbit-command-signing-key"

command -v gcloud >/dev/null 2>&1 || { echo "ERROR: gcloud CLI is required." >&2; exit 1; }

# --- Preflight-----

if [[ -z "${ORBIT_API_KEY:-}" ]]; then
  if [[ "${ORBIT_ALLOW_UNAUTHENTICATED_API:-}" == "1" ]]; then
    echo "WARNING: ORBIT_API_KEY unset and ORBIT_ALLOW_UNAUTHENTICATED_API=1 — deploying WITHOUT auth."
  else
    echo "ERROR: ORBIT_API_KEY is unset. The service would deploy publicly with" >&2
    echo "       authentication DISABLED. Generate one and retry:" >&2
    echo "         export ORBIT_API_KEY=\$(openssl rand -hex 32)" >&2
    echo "       To deploy unauthenticated anyway: export ORBIT_ALLOW_UNAUTHENTICATED_API=1" >&2
    exit 1
  fi
fi

if [[ -z "${SPACETRACK_USERNAME:-}" || -z "${SPACETRACK_PASSWORD:-}" ]]; then
  if [[ "${ORBIT_ALLOW_SIMULATED_DATA:-}" == "1" ]]; then
    echo "WARNING: Space-Track credentials unset — the fleet will run on SIMULATED orbits."
    LIVE_MODE="auto"
  else
    echo "ERROR: SPACETRACK_USERNAME / SPACETRACK_PASSWORD are unset. The deployed" >&2
    echo "       fleet would silently fall back to the simulated catalogue while" >&2
    echo "       the README claims live Space-Track data. Export both and retry." >&2
    echo "       To deploy on simulated data anyway: export ORBIT_ALLOW_SIMULATED_DATA=1" >&2
    exit 1
  fi
else
  LIVE_MODE="1"
fi

if [[ -z "${ORBIT_COMMAND_SIGNING_KEY:-}" ]]; then
  echo "WARNING: ORBIT_COMMAND_SIGNING_KEY unset — manoeuvre commands will be signed"
  echo "    with a per-process key that no other replica can verify. Set one with:"
  echo "      export ORBIT_COMMAND_SIGNING_KEY=\$(openssl rand -hex 32)"
fi

# --- Enable the APIs this deployment touches (idempotent, ~1 min first run) ---
echo "Enabling required APIs ..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --project "${PROJECT_ID}"

# --- Firestore (idempotent) --------------------------------------------------
if [[ -z "$(gcloud firestore databases list --project "${PROJECT_ID}" --format='value(name)' 2>/dev/null)" ]]; then
  echo "Creating Firestore database in ${FIRESTORE_LOCATION} ..."
  gcloud firestore databases create --location="${FIRESTORE_LOCATION}" --project "${PROJECT_ID}"
else
  echo "Firestore database already present."
fi

# --- Secrets -----
put_secret() {
  local name="$1" value="$2"
  [[ -z "${value}" ]] && return 0
  if gcloud secrets describe "${name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    printf '%s' "${value}" | gcloud secrets versions add "${name}" \
      --data-file=- --project "${PROJECT_ID}" >/dev/null
  else
    printf '%s' "${value}" | gcloud secrets create "${name}" \
      --data-file=- --replication-policy=automatic --project "${PROJECT_ID}" >/dev/null
  fi
  echo "   stored ${name}"
}

echo "Storing secrets in Secret Manager ..."
put_secret "${SECRET_API_KEY}"       "${ORBIT_API_KEY:-}"
put_secret "${SECRET_SPACETRACK_PW}" "${SPACETRACK_PASSWORD:-}"
put_secret "${SECRET_SIGNING_KEY}"   "${ORBIT_COMMAND_SIGNING_KEY:-}"

echo "Building ${IMAGE_URL} ..."
gcloud builds submit --tag "${IMAGE_URL}"

# --- Service accounts (idempotent) -------------------------------------------
ensure_sa() {
  local name="$1" display="$2"
  local email="${name}@${PROJECT_ID}.iam.gserviceaccount.com"
  if [[ -z "$(gcloud iam service-accounts list --filter="email:${email}" --format='value(email)' 2>/dev/null)" ]]; then
    echo "Creating service account ${email} ..."
    gcloud iam service-accounts create "${name}" --display-name="${display}"
  fi
}
ensure_sa "${SA_NAME}"     "O.R.B.I.T. Fleet Commander"
ensure_sa "${WEB_SA_NAME}" "O.R.B.I.T. Command Center"

for ROLE in roles/datastore.user roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" --quiet >/dev/null 2>&1 || true
done

grant_secret() {
  local secret="$1" member="$2"
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${member}" \
    --role="roles/secretmanager.secretAccessor" \
    --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
}
for S in "${SECRET_API_KEY}" "${SECRET_SPACETRACK_PW}" "${SECRET_SIGNING_KEY}"; do
  if gcloud secrets describe "${S}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    grant_secret "${S}" "${SA_EMAIL}"
  fi
done
grant_secret "${SECRET_API_KEY}" "${WEB_SA_EMAIL}"

# --- Backend deploy ----------------------------------------------------------
# Model IDs are pinned on purpose. Code defaults exist, but they disagree with
# the local .env, so leaving these unset would put a fleet into production
# running different models from the one you tested.
# gcloud's "^X^" prefix chooses the separator for this list, and the separator
# has to be a character no value contains. Both the default comma and the "@"
# this script started with fail that test: SPACETRACK_USERNAME is an email
# address, so "@" tore it into a bogus second entry and gcloud rejected it
# with "Bad syntax for dict arg: [gmail.com]".
BACKEND_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
BACKEND_ENV+="|GOOGLE_GENAI_USE_VERTEXAI=TRUE"
BACKEND_ENV+="|GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}"
BACKEND_ENV+="|ORBIT_MEDIA_LOCATION=${MEDIA_LOCATION}"
BACKEND_ENV+="|ORBIT_MEMORY_BACKEND=firestore"
BACKEND_ENV+="|ORBIT_LIVE_MODE=${LIVE_MODE}"
BACKEND_ENV+="|SPACETRACK_USERNAME=${SPACETRACK_USERNAME:-}"
for VAR in ORBIT_COMMANDER_MODEL_ID ORBIT_ASTRO_MODEL_ID ORBIT_STRATEGIST_MODEL_ID \
           ORBIT_DEBATE_JUDGE_MODEL_ID ORBIT_DIPLOMAT_MODEL_ID ORBIT_EDGE_MODEL_ID \
           ORBIT_EMBED_MODEL_ID ORBIT_LEARNING_MODEL_ID ORBIT_META_CRITIC_MODEL_ID \
           ORBIT_VERTEX_MODEL_ID ORBIT_VEO_MODEL_ID ORBIT_LYRIA_MODEL_ID; do
  if [[ -n "${!VAR:-}" ]]; then
    BACKEND_ENV+="|${VAR}=${!VAR}"
  fi
done

BACKEND_SECRETS=""
if [[ -n "${ORBIT_API_KEY:-}" ]]; then
  BACKEND_SECRETS+="ORBIT_API_KEY=${SECRET_API_KEY}:latest,"
fi
if [[ -n "${SPACETRACK_PASSWORD:-}" ]]; then
  BACKEND_SECRETS+="SPACETRACK_PASSWORD=${SECRET_SPACETRACK_PW}:latest,"
fi
if [[ -n "${ORBIT_COMMAND_SIGNING_KEY:-}" ]]; then
  BACKEND_SECRETS+="ORBIT_COMMAND_SIGNING_KEY=${SECRET_SIGNING_KEY}:latest,"
fi
BACKEND_SECRETS="${BACKEND_SECRETS%,}"

BACKEND_ARGS=(
  --image "${IMAGE_URL}"
  --platform managed
  --region "${REGION}"
  --port 8080
  --memory 1Gi
  --cpu 1
  --min-instances 0
  --max-instances 3
  --timeout 300
  --allow-unauthenticated
  --service-account "${SA_EMAIL}"
  --set-env-vars "^|^${BACKEND_ENV}"
)
if [[ -n "${BACKEND_SECRETS}" ]]; then
  BACKEND_ARGS+=(--set-secrets "${BACKEND_SECRETS}")
fi

gcloud run deploy "${SERVICE_NAME}" "${BACKEND_ARGS[@]}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --format='value(status.url)')"

# --- Frontend: static bundle + authenticating reverse proxy -----------------
if [[ "${ORBIT_SKIP_FRONTEND:-}" == "1" ]]; then
  echo "ORBIT_SKIP_FRONTEND=1 — backend only."
  WEB_URL=""
else
  echo "Building ${WEB_IMAGE_URL} ..."
  BUILD_CFG="$(mktemp -t orbit-web-cloudbuild.XXXXXX.yaml)"
  trap 'rm -f "${BUILD_CFG}"' EXIT
  {
    echo "steps:"
    echo "  - name: gcr.io/cloud-builders/docker"
    echo "    args: [\"build\", \"-f\", \"Dockerfile.frontend\", \"-t\", \"${WEB_IMAGE_URL}\", \".\"]"
    echo "images: [\"${WEB_IMAGE_URL}\"]"
  } >"${BUILD_CFG}"
  gcloud builds submit --config "${BUILD_CFG}" .

  WEB_ARGS=(
    --image "${WEB_IMAGE_URL}"
    --platform managed
    --region "${REGION}"
    --port 8080
    --memory 512Mi
    --cpu 1
    --min-instances 0
    --max-instances 3
    --allow-unauthenticated
    --service-account "${WEB_SA_EMAIL}"
    --set-env-vars "BACKEND_URL=${SERVICE_URL}"
  )
  if [[ -n "${ORBIT_API_KEY:-}" ]]; then
    WEB_ARGS+=(--set-secrets "ORBIT_API_KEY=${SECRET_API_KEY}:latest")
  fi

  gcloud run deploy "${WEB_SERVICE_NAME}" "${WEB_ARGS[@]}"

  WEB_URL="$(gcloud run services describe "${WEB_SERVICE_NAME}" --platform managed --region "${REGION}" --format='value(status.url)')"

  echo "Restricting backend CORS to ${WEB_URL} ..."
  gcloud run services update "${SERVICE_NAME}" \
    --platform managed --region "${REGION}" \
    --update-env-vars "ORBIT_CORS_ORIGINS=${WEB_URL}" >/dev/null
fi

echo "Deployment complete."
echo "Backend URL:    ${SERVICE_URL}"
if [[ -n "${WEB_URL}" ]]; then
  echo "Command center: ${WEB_URL}"
fi
echo ""
echo "FIRST: confirm live data actually reached the deployed fleet."
echo "  curl -s \"${SERVICE_URL}/api/orbital_state\" -H \"X-API-KEY: \$ORBIT_API_KEY\" | head -c 400"
echo ""
echo "  Expect \"source\": \"space-track\". Anything else means the fleet is on"
echo "  simulated orbits and the console will correctly, but quietly, say so."
echo ""
echo "THEN: discover the real pair and run a mission."
echo "  curl -s \"${SERVICE_URL}/api/live_protagonist\" -H \"X-API-KEY: \$ORBIT_API_KEY\""
echo ""
echo "  curl -X POST \"${SERVICE_URL}/api/conjunction_alert\" \\"
echo "    -H \"X-API-KEY: \$ORBIT_API_KEY\" -H \"Content-Type: application/json\" \\"
echo "    -d '{\"sat_id\":\"<protagonist>\",\"debris_id\":\"<counterparty>\",\"alert_source\":\"SPACE_TRACK_API\",\"priority\":\"CRITICAL\",\"raw_message\":\"URGENT: conjunction warning from the public CDM feed.\"}'"
