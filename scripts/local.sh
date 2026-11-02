#!/usr/bin/env zsh
set -euo pipefail

# Local dev runner for Flask with a dedicated virtualenv.
# Defaults to Cloud SQL public IP; override via env vars if needed.

: "${DB_HOST:=136.116.86.130}"
: "${DB_PORT:=5432}"
: "${DB_NAME:=ciccarolstracker2025}"
: "${DB_USER:=ciccarolstracker}"
: "${DB_PASSWORD:=ciccarolstracker}"

export PG_DSN="dbname=${DB_NAME} user=${DB_USER} password=${DB_PASSWORD} host=${DB_HOST} port=${DB_PORT}"

# Create and use a local venv to ensure Flask and deps are available regardless of system Python.
VENV_DIR=".venv"
if [[ ! -d "${VENV_DIR}" ]]; then
	echo "[local] Creating virtualenv in ${VENV_DIR}"
	python3 -m venv "${VENV_DIR}"
fi
source "${VENV_DIR}/bin/activate"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
python3 -m pip install -r requirements.txt

echo "[local] Starting Flask dev server on http://127.0.0.1:5000"
exec python3 -m flask --app tracker.web run --port 5000