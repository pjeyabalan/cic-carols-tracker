#!/bin/zsh
# Script to fully stop the running CarolsTracker instance (Cloud Run)
# WARNING: This will DELETE the Cloud Run service. You can redeploy anytime using deploy.sh.
# Usage: ./scripts/stop_instance.sh

PROJECT_ID="linen-flux-207118"
SERVICE_NAME="carols-tracker"
REGION="us-central1"

echo "Deleting Cloud Run service: $SERVICE_NAME in project $PROJECT_ID, region $REGION..."
gcloud run services delete $SERVICE_NAME --region $REGION --project $PROJECT_ID --quiet

echo "CarolsTracker Cloud Run service deleted. You can redeploy with deploy.sh."
