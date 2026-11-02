# Carols Tracker

This project holds the scaffolding for a simple carols route planner. It can load stops from a local CSV file or a Postgres table, summarize each family’s prayer request, and simulate a GPS-driven route with ETA estimates so you can see where the group will head next.

## Features

- `tracker.data_sources`: abstract loader for CSV or Postgres data.
- `tracker.gps`: helper that generates deterministic coordinates from zip codes, computes distances, and streams simulated live GPS updates.
- `tracker.main`: CLI runner that prints stop summaries and (optionally) simulates the current route.
- `tracker.web`: phone-friendly track-line view where each stop renders as a transparent coach/house and you can copy its address for navigation.
- Test coverage for CSV loading, GPS simulation, and the Flask page.

The Flask UI defaults to today’s day name, highlights completed stops in green, pending visits in orange, and lets late starters tap into any home to read the family members and prayer points.

## Setup


2. The sample data lives in `data/carols_schedule.csv`. Replace it with your actual itinerary or point the CLI to a Postgres table (see below).

## Songbook parsing (song-by-song)

You can split the provided PDF (CICAG Christmas Carol Song Book) into individual plain-text files per song, plus an index JSON.

Outputs are written to `data/songs/`:
- `NNN_Title.txt` for each detected song
- `index.json` with number, title, language guess, and filename

Run:

```
source .venv/bin/activate  # if using local.sh venv
pip install -r requirements.txt
python scripts/parse_songbook.py "CICAG Christmas Carol Song Book V4.4 -Year 2025 (1).pdf"
```

Optionally pass a custom output directory:

```
python scripts/parse_songbook.py "CICAG Christmas Carol Song Book V4.4 -Year 2025 (1).pdf" data/songs
```

Notes:
- Song detection uses numbered title lines (e.g., `1. Title`, `2 Title`).
- Language guess is heuristic based on Unicode ranges (Tamil, Malayalam, Telugu, Hindi/Devanagari, English).
- Front matter before the first numbered title is skipped.

## Usage

Generate a visit summary from the CSV:

```bash
python -m tracker.main --source csv
```

Simulate route + live GPS updates starting from a custom location:

```bash
python -m tracker.main --simulate --start-lat 40.1 --start-lon -74.1 --speed 30
```

Filter by day/time:

```bash
python -m tracker.main --day Sunday --start-time 18:00 --end-time 20:00
```

Switch to a Postgres data source (requires table `carols_schedule` with the same columns as the CSV):

```bash
python -m tracker.main --source postgres --pg-dsn "dbname=carols user=..."
```

Run the lightweight UI in your browser:

```bash
export FLASK_APP=tracker.web:app
python3 -m flask run
```

Open http://localhost:5000 on your phone. The app now launches directly into the track view: each stop becomes a transparent glassy house sitting beside a road, green cards are completed, orange marks the current stop, and red indicates pending homes. Tap any card to reveal family and prayer details, then hit “Copy address” to grab the location for navigation.

## Testing

```bash
pytest
```

## Deploying to Google Cloud Run

This repo includes a production-ready Dockerfile and a minimal setup to deploy on Cloud Run and connect to Cloud SQL for PostgreSQL.

### 1) Build and push the image

```bash
gcloud builds submit --tag gcr.io/PROJECT_ID/carols-tracker
```

### 2) Deploy on Cloud Run using Cloud SQL connector (recommended)

This avoids managing public IP allowlists. Attach your Cloud SQL instance to the service and connect via Unix socket.

Required values:
- INSTANCE: the instance connection name, e.g. `PROJECT_ID:REGION:INSTANCE_NAME`
- DB: `ciccarolstracker2025`
- USER: `ciccarolstracker`
- PASSWORD: your Postgres password

Set `PG_DSN` to a psycopg2 DSN that uses the Unix socket:

```bash
gcloud run deploy carols-tracker \
   --image gcr.io/PROJECT_ID/carols-tracker \
   --region REGION \
   --allow-unauthenticated \
   --add-cloudsql-instances PROJECT_ID:REGION:INSTANCE_NAME \
   --update-env-vars "PG_DSN=dbname=ciccarolstracker2025 user=ciccarolstracker password=YOUR_PASSWORD host=/cloudsql/PROJECT_ID:REGION:INSTANCE_NAME"
```

No code changes are required—`psycopg2.connect` accepts this DSN format.

### 3) Alternative: Public IP (simpler, but manage egress IP)

If your Cloud SQL instance has a public IP and you prefer a URL DSN, set:

```bash
gcloud run deploy carols-tracker \
   --image gcr.io/PROJECT_ID/carols-tracker \
   --region REGION \
   --allow-unauthenticated \
   --update-env-vars "PG_DSN=postgresql://USER:PASSWORD@PUBLIC_IP:5432/ciccarolstracker2025?sslmode=require"
```

Note: Cloud Run egress IPs are not fixed unless you configure a Serverless VPC Connector + Cloud NAT with static egress. You’ll need to allow that IP on the Cloud SQL instance authorized networks.

### Verify

Open the service URL. The app will connect to Postgres if `PG_DSN` is set; otherwise it falls back to CSV.

## Extending

- Replace the simulated `simulate_live_updates` generator with actual GPS updates.
- Add an inline “Mark visited” button per card to update `address_visited` without leaving the tracker.
- Improve schedule filters and search.