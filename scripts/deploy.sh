#!/usr/bin/env zsh
set -euo pipefail

# ======= CONFIGURE THESE =======
PROJECT_ID="linen-flux-207118"
REGION="us-central1"
SERVICE_NAME="carols-tracker"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"
INSTANCE_NAME="ciccarolstracker"  # Cloud SQL Postgres instance short name
DB_NAME="ciccarolstracker2025"
DB_USER="ciccarolstracker"
DB_PASSWORD="ciccarolstracker"    # current password set on the Cloud SQL user
SECRET_NAME="pg-password"
# Custom domain settings (optional)
DOMAIN="ciccarols.com"             # apex domain to map to Cloud Run
WWW_DOMAIN="www.ciccarols.com"     # also map www to the same service
# Runtime tuning
MIN_INSTANCES=1                     # keep one warm instance to reduce cold starts
CONCURRENCY=80                      # requests per instance
TIMEOUT=300                         # request timeout seconds
# ===============================

echo "Setting active project..."
gcloud config set project "${PROJECT_ID}"
echo "Setting Cloud Run defaults..."
gcloud config set run/platform managed
gcloud config set run/region "${REGION}"

echo "Enabling required APIs..."
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable sqladmin.googleapis.com

echo "Ensuring gcloud beta commands are installed..."
if ! gcloud beta --version >/dev/null 2>&1; then
  gcloud components install beta --quiet || {
    echo "Failed to install gcloud beta components. Please run: gcloud components install beta" >&2
    exit 1
  }
fi

echo "Building and pushing image (Dockerfile must be in repo root)..."
gcloud builds submit --tag "${IMAGE}"


# echo "Creating/updating Secret Manager secret for DB password..."
# If secret doesn't exist, create; else add a new version.
# echo "Granting Secret access to Cloud Run runtime service account..."


echo "Getting Cloud SQL instance connection name..."
INSTANCE_CONN=$(gcloud sql instances describe "${INSTANCE_NAME}" --format="value(connectionName)")
echo "Instance connection: ${INSTANCE_CONN}"

echo "Deploying Cloud Run service with Cloud SQL connector and secret-injected PG_DSN..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --add-cloudsql-instances "${INSTANCE_CONN}" \
  --set-env-vars DB_PASSWORD="${DB_PASSWORD}",PG_DSN="dbname=${DB_NAME} user=${DB_USER} password=${DB_PASSWORD} host=/cloudsql/${INSTANCE_CONN}",APP_TIMEZONE=CST

# Optional runtime tuning
if [[ ${MIN_INSTANCES} -gt 0 ]]; then
  echo "Updating min instances to ${MIN_INSTANCES}..."
  gcloud run services update "${SERVICE_NAME}" --region "${REGION}" --min-instances ${MIN_INSTANCES}
fi
if [[ -n "${CONCURRENCY}" ]]; then
  echo "Updating concurrency to ${CONCURRENCY}..."
  gcloud run services update "${SERVICE_NAME}" --region "${REGION}" --concurrency ${CONCURRENCY}
fi
if [[ -n "${TIMEOUT}" ]]; then
  echo "Updating request timeout to ${TIMEOUT}s..."
  gcloud run services update "${SERVICE_NAME}" --region "${REGION}" --timeout "${TIMEOUT}"
fi

echo "Service URL:"
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')
echo "${SERVICE_URL}"

echo "Recent logs (Cloud Run):"
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="${SERVICE_NAME}"' --project="${PROJECT_ID}" --limit=50 --format="value(textPayload)" || true

echo "To view logs for the running instance at any time, run:"
echo "gcloud logging read 'resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\"' --project=${PROJECT_ID} --limit=50 --format=\"value(textPayload)\""

echo "Creating/Updating Cloud Run domain mapping for ${DOMAIN}..."
if [[ -n "${DOMAIN}" ]]; then
  if gcloud beta run domain-mappings describe --domain="${DOMAIN}" >/dev/null 2>&1; then
    echo "Domain mapping already exists for ${DOMAIN}."
  else
    gcloud beta run domain-mappings create \
      --domain="${DOMAIN}" \
      --service="${SERVICE_NAME}" \
      --quiet
  fi
  echo "Domain mapping status for ${DOMAIN}:"
  echo "Waiting briefly and describing domain mapping for ${DOMAIN}..."
  sleep 3
  gcloud beta run domain-mappings describe --domain="${DOMAIN}" --format="yaml(status,resourceRecords)" || true
  echo "Polling certificate status for ${DOMAIN} (up to ~15 minutes)..."
  for i in {1..30}; do
    CERT_STATUS=$(gcloud beta run domain-mappings describe --domain="${DOMAIN}" --format="value(status.conditions[2].reason)" 2>/dev/null || echo "")
    READY=$(gcloud beta run domain-mappings describe --domain="${DOMAIN}" --format="value(status.conditions[0].type,status.conditions[0].status)" 2>/dev/null || echo "")
    echo "[${DOMAIN}] Attempt ${i}: condition=${CERT_STATUS} ready=${READY}"
    if gcloud beta run domain-mappings describe --domain="${DOMAIN}" --format="value(status.conditions[1].type,status.conditions[1].status)" | grep -q "CertificateProvisioned  True"; then
      echo "Managed certificate appears provisioned for ${DOMAIN}."
      break
    fi
    sleep 30
  done
fi

if [[ -n "${WWW_DOMAIN}" ]]; then
  echo "Creating/Updating Cloud Run domain mapping for ${WWW_DOMAIN}..."
  if gcloud beta run domain-mappings describe --domain="${WWW_DOMAIN}" >/dev/null 2>&1; then
    echo "Domain mapping already exists for ${WWW_DOMAIN}."
  else
    gcloud beta run domain-mappings create \
      --domain="${WWW_DOMAIN}" \
      --service="${SERVICE_NAME}" \
      --quiet
  fi
  echo "Domain mapping status for ${WWW_DOMAIN}:"
  echo "Waiting briefly and describing domain mapping for ${WWW_DOMAIN}..."
  sleep 3
  gcloud beta run domain-mappings describe --domain="${WWW_DOMAIN}" --format="yaml(status,resourceRecords)" || true
  echo "Polling certificate status for ${WWW_DOMAIN} (up to ~15 minutes)..."
  for i in {1..30}; do
    CERT_STATUS=$(gcloud beta run domain-mappings describe --domain="${WWW_DOMAIN}" --format="value(status.conditions[2].reason)" 2>/dev/null || echo "")
    READY=$(gcloud beta run domain-mappings describe --domain="${WWW_DOMAIN}" --format="value(status.conditions[0].type,status.conditions[0].status)" 2>/dev/null || echo "")
    echo "[${WWW_DOMAIN}] Attempt ${i}: condition=${CERT_STATUS} ready=${READY}"
    if gcloud beta run domain-mappings describe --domain="${WWW_DOMAIN}" --format="value(status.conditions[1].type,status.conditions[1].status)" | grep -q "CertificateProvisioned  True"; then
      echo "Managed certificate appears provisioned for ${WWW_DOMAIN}."
      break
    fi
    sleep 30
  done
fi

echo "Done. Visit: ${SERVICE_URL}"
if [[ -n "${DOMAIN}" ]]; then
  echo "Once DNS is updated, visit: https://${DOMAIN}"
fi
if [[ -n "${WWW_DOMAIN}" ]]; then
  echo "If you set www, visit: https://${WWW_DOMAIN}"
fi
