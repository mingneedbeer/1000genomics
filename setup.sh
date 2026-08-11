#!/bin/bash
set -euo pipefail

# --- Virtualenv bootstrap ---
if [ ! -d ".venv" ]; then
  echo ">> Creating virtualenv..."
  python3 -m venv .venv
fi
source .venv/bin/activate

PROJECT_ID=$(python3 -c "import yaml; print(yaml.safe_load(open('config.yaml'))['gcp']['project_id'])")
REGION=$(python3 -c "import yaml; print(yaml.safe_load(open('config.yaml'))['gcp']['region'])")
DATASET=$(python3 -c "import yaml; print(yaml.safe_load(open('config.yaml'))['bigquery']['dataset'])")

echo "=== 1000 Genomes Risk Prediction Pipeline Setup ==="
echo "Project: $PROJECT_ID"
echo "Region:  $REGION"

# Install Python dependencies (using venv pip)
echo ">> Installing Python dependencies..."
pip install -r requirements.txt

# Authenticate
echo ""
echo ">> Checking GCP authentication..."
if [ -f "$HOME/.config/gcloud/application_default_credentials.json" ]; then
  echo "   ADC found — skipping interactive login."
else
  echo "   Run this manually in a browser-enabled terminal:"
  echo "   gcloud auth application-default login --project=$PROJECT_ID"
  echo "   Then re-run this script."
  exit 1
fi

# Create BigQuery dataset
echo ">> Creating BigQuery dataset: $DATASET..."
bq --project_id="$PROJECT_ID" mk --dataset \
  --location="$REGION" \
  --description="1000 Genomes disease variant analysis and polygenic risk scores" \
  "$PROJECT_ID:$DATASET" 2>/dev/null || echo "Dataset already exists"

# Create GCS bucket for artifacts
BUCKET="${PROJECT_ID}-1000genomics"
echo ">> Creating GCS bucket: $BUCKET..."
gsutil mb -l "$REGION" -p "$PROJECT_ID" "gs://$BUCKET" 2>/dev/null || echo "Bucket already exists"

echo ""
echo ">> Setup complete. Activate venv before running steps:"
echo "   source .venv/bin/activate"
echo "   python3 01_variant_extraction.py"
