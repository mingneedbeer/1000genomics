# 1000 Genomes PRS Risk Analysis

Genome-wide risk analysis pipeline that builds **polygenic risk score (PRS)** models for 4 diseases from the 1000 Genomes Phase 3 cohort, using **Google BigQuery + Vertex AI**, and visualizes results in an **Astro web dashboard** deployed to Vercel.

## Overview

```
BigQuery 1000 Genomes variants ──► Extract 12 target variants ──► PRS scoring + PCA
        │                                                            │
        └──────────────────► ElasticNet training (4 diseases) ◄─────┘
                                    │
                                    ▼
                        Risk prediction + evaluation
                                    │
                                    ▼
                  Astro dashboard (Vercel) + BigQuery tables
```

- **Cohort**: 2,504 samples from 1000 Genomes Phase 3 (26 sub-populations, 5 super-populations)
- **Model**: ElasticNet with L1/L2 grid search, 5-fold CV, 10 ancestry PCs as covariates
- **Diseases**: Cardiovascular, Cancer Predisposition, Type 2 Diabetes, Autoimmune
- **Evaluation**: AUC-ROC, log-loss, Brier score, calibration curves

## Pipeline

| Step | Script | Description |
|------|--------|-------------|
| 0 | `setup.sh` | Create venv, BigQuery dataset, GCS bucket |
| 1 | `01_variant_extraction.py` | Query BigQuery for 16 rsIDs → 12 matched variants → genotype dosage matrix (2504 × 12) |
| 2 | `02_prs_weights.py` | Compute PRS scores per disease + top 10 population PCs |
| 3 | `03_vertex_prs_train.py` | Train ElasticNet models (4 diseases), save locally, upload to Vertex AI |
| 4 | `04_risk_prediction.py` | Risk prediction, evaluation metrics, visualization, BigQuery export |

### Configuration (`config.yaml`)

- GCP project / region / GCS bucket
- BigQuery source tables (1000 Genomes Phase 3, dbSNP, ClinVar)
- 4 disease definitions × 12 curated variants (rsIDs, genes, genomic positions)
- Vertex AI training/prediction machine types
- PRS hyperparameters (alpha, L1 ratio, PCs, CV folds, p-value thresholds)

### Results (`output/`)

| Artifact | Description |
|----------|-------------|
| `models/` | Trained ElasticNet models (4 diseases) |
| `prs_scores.csv` | PRS scores for 2,504 samples × 4 diseases |
| `risk_reports.csv` | Per-sample risk predictions with ancestry |
| `model_evaluation.json` | AUC / log-loss / Brier by disease × sub-population |
| `population_pcs.csv` | Top 10 principal components |
| `*.png` | AUC heatmap, calibration curves, risk distributions |

## Web Dashboard

Located in [`web/`](web/), an **Astro 5 + Tailwind** static site deployed to Vercel.

- **Pages**: Overview, Variants (allele frequency), Predictions (risk table), Evaluation (AUC heatmap)
- **Data**: `web/src/data/*.json` (exported pipeline outputs)
- **Deployment**: https://web-mu-beryl-t605nfxw5q.vercel.app

```sh
cd web
npm install
npm run dev      # local dev
npm run build    # production build
npx vercel --prod  # deploy
```

## Setup

```sh
./setup.sh                 # venv + GCP resources
source .venv/bin/activate
pip install -r requirements.txt
python 01_variant_extraction.py
python 02_prs_weights.py
python 03_vertex_prs_train.py
python 04_risk_prediction.py
```

Requires `gcloud` authenticated with a service account (`GOOGLE_APPLICATION_CREDENTIALS` or `gcloud auth application-default login`).

## Known Limitations

- **Cancer PRS shows no signal** (AUC ≈ 0.50) — BRCA1/2, TP53, APC pathogenic mutations are absent from the 1000 Genomes cohort (healthy individuals), so cancer predisposition PRS is uninformative here.
- **Vertex AI endpoint deployment fails** — sklearn serving container exits due to joblib serialization incompatibility; models are trained/uploaded but run locally for prediction.
- **GWAS Catalog REST API** returns 404 for EFO term searches → falls back to curated candidate variants.
- PRS models are research-only; not validated for clinical use.

## Infrastructure

- **Google Cloud**: `project-c68dc618-1950-4820-a3c` (`us-central1`)
- **BigQuery dataset**: `genome_risk_analysis`
- **GCS bucket**: `gs://project-c68dc618-1950-4820-a3c-1000genomics`
