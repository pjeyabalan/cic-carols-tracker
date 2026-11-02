#!/usr/bin/env zsh
set -euo pipefail

# ======= CONFIGURE THESE =======
PROJECT_ID="linen-flux-207118"
REGION="us-central1"
SERVICE_NAME="carols-tracker"
# ===============================

echo "Setting active project..."
gcloud config set project "${PROJECT_ID}"

echo "Deleting Cloud Run service ${SERVICE_NAME} in ${REGION}..."
gcloud run services delete "${SERVICE_NAME}" --region "${REGION}" --quiet || true

echo "Done."
