# Deployable image for Cloud Run
# Uses Python 3.11 slim and gunicorn to serve the Flask app

FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies (none needed for psycopg2-binary)
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (leverage Docker cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app source
COPY tracker ./tracker
RUN mkdir -p /app/data
COPY README.md ./README.md

# Cloud Run provides $PORT; default to 8080 for local runs
ENV PORT=8080

# Gunicorn entrypoint for Flask app `tracker.web:app`
# Use shell form so $PORT is expanded by the shell
CMD sh -c 'gunicorn --threads 8 -w 1 --timeout 120 -b 0.0.0.0:$PORT tracker.web:app'
