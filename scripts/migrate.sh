#!/usr/bin/env zsh
set -euo pipefail

# ======= CONFIGURE THESE =======
PROJECT_ID="linen-flux-207118"
REGION="us-central1"
INSTANCE_NAME="ciccarolstracker"  # Cloud SQL Postgres instance short name
DB_NAME="ciccarolstracker2025"
DB_USER="ciccarolstracker"
DB_PASSWORD="ciccarolstracker"
# ===============================

# Optional: start Cloud SQL Auth Proxy (requires installation)
# cloud-sql-proxy "${PROJECT_ID}:${REGION}:${INSTANCE_NAME}" --port 5432 &
# sleep 2

export PGPASSWORD="${DB_PASSWORD}"

# Run migrations located in the repo's data/ folder
psql -h 136.116.86.130 -p 5432 -U "${DB_USER}" -d "${DB_NAME}" -f data/add_primary_key_id.sql
psql -h 136.116.86.130 -p 5432 -U "${DB_USER}" -d "${DB_NAME}" -f data/add_visit_order.sql
psql -h 136.116.86.130 -p 5432 -U "${DB_USER}" -d "${DB_NAME}" -f data/add_address_visited.sql

echo "Migrations applied."
