"""Step 4: Risk Prediction Pipeline + Evaluation.

This script:
1. Loads trained models from Vertex AI Model Registry
2. Predicts disease risk for all 1000 Genomes individuals
3. Evaluates model performance (AUC-ROC, calibration, ancestry breakdown)
4. Saves per-individual risk reports to BigQuery
5. Generates population-level visualization plots
"""

import os
import json
import yaml
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from google.cloud import aiplatform
from google.cloud import bigquery
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import (
    roc_auc_score,
    log_loss,
    brier_score_loss,
    precision_recall_curve,
    average_precision_score,
    confusion_matrix,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
with open(ROOT / "config.yaml") as f:
    cfg = yaml.safe_load(f)

PROJECT = cfg["gcp"]["project_id"]
REGION = cfg["gcp"]["region"]
BUCKET = cfg["gcp"]["bucket"]
DATASET = cfg["bigquery"]["dataset"]
OUTPUT_DIR = ROOT / "output"


# ---------------------------------------------------------------------------
# 0. Synthetic phenotype generator (shared across pipeline steps)
# ---------------------------------------------------------------------------
def generate_synthetic_phenotypes(
    prs_df: pd.DataFrame, random_state: int = 42
) -> pd.DataFrame:
    """Generate synthetic disease phenotype labels correlated with PRS.

    In a real study these would come from EHR or cohort data.
    Here we simulate binary phenotypes where disease probability
    increases with PRS values.
    """
    rng = np.random.RandomState(random_state)

    records = []
    for disease in prs_df["disease"].unique():
        sub = prs_df[prs_df["disease"] == disease].copy()
        prs_vals = sub["prs"].values

        if prs_vals.std() > 0:
            prs_norm = (prs_vals - prs_vals.min()) / (prs_vals.max() - prs_vals.min())
        else:
            prs_norm = np.full_like(prs_vals, 0.5)

        base_rate = 0.10
        log_odds = np.log(base_rate / (1 - base_rate))
        log_odds += 1.5 * prs_norm
        prob = 1 / (1 + np.exp(-log_odds))
        phenotype = rng.binomial(1, prob)

        for idx, (_, row) in enumerate(sub.iterrows()):
            records.append({
                "sample_id": row["sample_id"],
                "disease": disease,
                "disease_binary": int(phenotype[idx]),
            })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 1. Load data and models
# ---------------------------------------------------------------------------
def load_prs_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load PRS scores, PCs, and phenotype labels."""
    prs_df = pd.read_csv(OUTPUT_DIR / "prs_scores.csv")
    pcs_df = pd.read_csv(OUTPUT_DIR / "population_pcs.csv")
    ancestry_df = pd.read_csv(OUTPUT_DIR / "sample_ancestry.csv")

    logger.info(
        f"Loaded PRS ({prs_df.shape}), PCs ({pcs_df.shape}), "
        f"Ancestry ({ancestry_df.shape})"
    )
    return prs_df, pcs_df, ancestry_df


def load_trained_models(disease_key: str) -> Tuple:
    """Load locally saved model and scaler."""
    import joblib

    model_dir = OUTPUT_DIR / "models" / disease_key
    model = joblib.load(model_dir / "model.joblib")
    scaler = joblib.load(model_dir / "scaler.joblib")
    with open(model_dir / "metadata.json") as f:
        metadata = json.load(f)

    return model, scaler, metadata


# ---------------------------------------------------------------------------
# 2. Predict risk
# ---------------------------------------------------------------------------
def predict_risk(
    model, scaler, X: np.ndarray, disease_key: str
) -> pd.DataFrame:
    """Run model prediction and return risk scores."""
    X_scaled = scaler.transform(X)

    # Predicted probabilities
    risk_prob = np.clip(model.predict(X_scaled), 0, 1)

    return pd.DataFrame({
        "disease": disease_key,
        "risk_probability": risk_prob,
    })


def build_prediction_dataset(
    prs_df: pd.DataFrame,
    pcs_df: pd.DataFrame,
    ancestry_df: pd.DataFrame,
) -> pd.DataFrame:
    """Build full prediction dataset and run all disease models."""
    results = []

    for disease_key in cfg["diseases"]:
        model_dir = OUTPUT_DIR / "models" / disease_key
        if not (model_dir / "model.joblib").exists():
            logger.warning(f"Skipping {disease_key}: no trained model found")
            continue
        model, scaler, metadata = load_trained_models(disease_key)

        # Prepare features
        prs_sub = prs_df[prs_df["disease"] == disease_key][
            ["sample_id", "prs"]
        ].copy()
        merged = prs_sub.merge(pcs_df, on="sample_id")

        feature_cols = ["prs"] + [
            c for c in merged.columns if c.startswith("PC")
        ]
        X = merged[feature_cols].values

        predictions = predict_risk(model, scaler, X, disease_key)
        predictions["sample_id"] = merged["sample_id"].values
        results.append(predictions)

    all_predictions = pd.concat(results, ignore_index=True)

    # Merge ancestry — keep only needed columns
    if "population" not in ancestry_df.columns and "Population" in ancestry_df.columns:
        ancestry_df.columns = ["sample_id", "population", "super_population"]
    all_predictions = all_predictions.merge(
        ancestry_df[["sample_id", "population"]], on="sample_id", how="left"
    )

    logger.info(f"Predictions complete: {all_predictions.shape}")
    return all_predictions


# ---------------------------------------------------------------------------
# 3. Evaluation metrics
# ---------------------------------------------------------------------------
def evaluate_predictions(
    predictions: pd.DataFrame,
    prs_df: pd.DataFrame,
) -> pd.DataFrame:
    """Compute evaluation metrics per disease and per ancestry."""
    phenotypes = generate_synthetic_phenotypes(prs_df)

    eval_records = []

    for disease_key in cfg["diseases"]:
        disease_pheno = phenotypes[phenotypes["disease"] == disease_key]
        disease_pred = predictions[predictions["disease"] == disease_key]

        merged = disease_pred.merge(
            disease_pheno[["sample_id", "disease_binary"]],
            on="sample_id",
        )

        y_true = merged["disease_binary"].values
        y_prob = merged["risk_probability"].values

        if len(np.unique(y_true)) < 2:
            logger.warning(f"Skipping eval for {disease_key}: single class")
            continue

        # Overall metrics
        auc = roc_auc_score(y_true, y_prob)
        logloss = log_loss(y_true, y_prob)
        brier = brier_score_loss(y_true, y_prob)
        ap = average_precision_score(y_true, y_prob)

        eval_records.append({
            "disease": disease_key,
            "ancestry": "ALL",
            "n_samples": len(y_true),
            "n_cases": int(y_true.sum()),
            "auc_roc": auc,
            "log_loss": logloss,
            "brier_score": brier,
            "avg_precision": ap,
        })

        # Per-ancestry breakdown
        for ancestry in merged["population"].dropna().unique():
            mask = merged["population"] == ancestry
            if mask.sum() < 10 or len(np.unique(y_true[mask])) < 2:
                continue

            sub_auc = roc_auc_score(y_true[mask], y_prob[mask])
            eval_records.append({
                "disease": disease_key,
                "ancestry": ancestry,
                "n_samples": int(mask.sum()),
                "n_cases": int(y_true[mask].sum()),
                "auc_roc": sub_auc,
                "log_loss": log_loss(y_true[mask], y_prob[mask]),
                "brier_score": brier_score_loss(y_true[mask], y_prob[mask]),
                "avg_precision": average_precision_score(y_true[mask], y_prob[mask]),
            })

    eval_df = pd.DataFrame(eval_records)

    logger.info("\nEvaluation Results:")
    logger.info(eval_df.to_string(index=False))

    return eval_df


# ---------------------------------------------------------------------------
# 4. Visualizations
# ---------------------------------------------------------------------------
def plot_risk_distributions(
    predictions: pd.DataFrame, save_path: Path
):
    """Plot risk probability distributions by disease and ancestry."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()

    diseases = predictions["disease"].unique()
    palette = {
        "AFR": "#E63946", "AMR": "#457B9D", "EUR": "#2A9D8F",
        "SAS": "#E9C46A", "EAS": "#F4A261",
    }

    for idx, disease in enumerate(diseases[:4]):
        ax = axes[idx]
        sub = predictions[predictions["disease"] == disease]

        for ancestry in palette:
            anc_data = sub[sub["population"] == ancestry]["risk_probability"]
            if len(anc_data) > 0:
                ax.hist(
                    anc_data, bins=30, alpha=0.5, label=ancestry,
                    color=palette[ancestry], density=True
                )

        ax.set_title(f"{disease.replace('_', ' ').title()}")
        ax.set_xlabel("Risk Probability")
        ax.set_ylabel("Density")
        ax.legend(fontsize=8)
        ax.set_xlim(0, 1)

    plt.suptitle("1000 Genomes Disease Risk Distribution by Ancestry", fontsize=14)
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"Saved risk distribution plot: {save_path}")


def plot_calibration_curves(
    predictions: pd.DataFrame,
    prs_df: pd.DataFrame,
    save_path: Path,
):
    """Plot calibration curves (predicted vs observed risk)."""
    phenotypes = generate_synthetic_phenotypes(prs_df)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()

    diseases = predictions["disease"].unique()

    for idx, disease in enumerate(diseases[:4]):
        ax = axes[idx]
        disease_pheno = phenotypes[phenotypes["disease"] == disease]
        disease_pred = predictions[predictions["disease"] == disease]

        merged = disease_pred.merge(
            disease_pheno[["sample_id", "disease_binary"]], on="sample_id"
        )

        y_true = merged["disease_binary"].values
        y_prob = merged["risk_probability"].values

        # Bin predictions
        n_bins = 10
        bin_edges = np.linspace(0, 1, n_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

        observed_rates = []
        for i in range(n_bins):
            mask = (y_prob >= bin_edges[i]) & (y_prob < bin_edges[i + 1])
            if mask.sum() > 0:
                observed_rates.append(y_true[mask].mean())
            else:
                observed_rates.append(np.nan)

        ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="Perfect calibration")
        ax.plot(bin_centers, observed_rates, "o-", label="Model")
        ax.set_title(f"{disease.replace('_', ' ').title()}")
        ax.set_xlabel("Predicted Risk")
        ax.set_ylabel("Observed Frequency")
        ax.legend(fontsize=8)

    plt.suptitle("Calibration Curves by Disease", fontsize=14)
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"Saved calibration plot: {save_path}")


def plot_ancestry_auc(
    eval_df: pd.DataFrame, save_path: Path
):
    """Bar chart of AUC-ROC by disease and ancestry."""
    plot_data = eval_df[eval_df["ancestry"] != "ALL"]

    if plot_data.empty:
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    sns.barplot(
        data=plot_data,
        x="disease",
        y="auc_roc",
        hue="ancestry",
        ax=ax,
    )

    ax.set_title("AUC-ROC by Disease and Ancestry Group")
    ax.set_xlabel("")
    ax.set_ylabel("AUC-ROC")
    ax.set_ylim(0.5, 1.0)
    ax.axhline(y=0.5, color="gray", linestyle="--", alpha=0.5)
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info(f"Saved ancestry AUC plot: {save_path}")


# ---------------------------------------------------------------------------
# 5. Save results to BigQuery
# ---------------------------------------------------------------------------
def save_risk_reports_to_bq(
    predictions: pd.DataFrame, eval_df: pd.DataFrame
):
    """Save predictions and evaluation to BigQuery tables."""
    client = bigquery.Client(project=PROJECT)

    # Save predictions
    pred_table = f"{PROJECT}.{DATASET}.risk_predictions"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    job = client.load_table_from_dataframe(
        predictions, pred_table, job_config=job_config
    )
    job.result()
    logger.info(f"Saved predictions to {pred_table}: {len(predictions)} rows")

    # Save evaluation
    eval_table = f"{PROJECT}.{DATASET}.model_evaluation"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    job = client.load_table_from_dataframe(
        eval_df, eval_table, job_config=job_config
    )
    job.result()
    logger.info(f"Saved evaluation to {eval_table}: {len(eval_df)} rows")


# ---------------------------------------------------------------------------
# 6. Generate per-individual risk report
# ---------------------------------------------------------------------------
def generate_risk_reports(predictions: pd.DataFrame) -> pd.DataFrame:
    """Create structured risk reports for each individual."""
    reports = []

    for sample_id in predictions["sample_id"].unique():
        sample_pred = predictions[predictions["sample_id"] == sample_id]
        population = sample_pred["population"].iloc[0] if "population" in sample_pred.columns else "Unknown"

        risk_levels = {}
        for _, row in sample_pred.iterrows():
            risk_prob = row["risk_probability"]
            if risk_prob > 0.75:
                level = "HIGH"
            elif risk_prob > 0.50:
                level = "MODERATE"
            elif risk_prob > 0.25:
                level = "LOW"
            else:
                level = "VERY_LOW"

            risk_levels[row["disease"]] = {
                "probability": round(risk_prob, 4),
                "level": level,
            }

        reports.append({
            "sample_id": sample_id,
            "population": population,
            "risk_report": json.dumps(risk_levels),
            "max_risk_probability": max(r["probability"] for r in risk_levels.values()),
            "max_risk_disease": max(risk_levels, key=lambda k: risk_levels[k]["probability"]),
        })

    return pd.DataFrame(reports)


# ---------------------------------------------------------------------------
# 7. Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Step 4: Risk Prediction + Evaluation")
    logger.info("=" * 60)

    # Load data
    prs_df, pcs_df, ancestry_df = load_prs_data()

    # Run predictions
    predictions = build_prediction_dataset(prs_df, pcs_df, ancestry_df)

    # Evaluate
    eval_df = evaluate_predictions(predictions, prs_df)

    # Visualizations
    plot_risk_distributions(predictions, OUTPUT_DIR / "risk_distributions.png")
    plot_calibration_curves(predictions, prs_df, OUTPUT_DIR / "calibration_curves.png")
    plot_ancestry_auc(eval_df, OUTPUT_DIR / "ancestry_auc.png")

    # Save to BigQuery
    save_risk_reports_to_bq(predictions, eval_df)

    # Generate individual risk reports
    risk_reports = generate_risk_reports(predictions)
    reports_path = OUTPUT_DIR / "risk_reports.csv"
    risk_reports.to_csv(reports_path, index=False)

    # Upload to GCS
    os.system(f"gsutil cp {reports_path} {BUCKET}/reports/")
    os.system(f"gsutil cp {OUTPUT_DIR}/risk_distributions.png {BUCKET}/reports/")
    os.system(f"gsutil cp {OUTPUT_DIR}/calibration_curves.png {BUCKET}/reports/")
    os.system(f"gsutil cp {OUTPUT_DIR}/ancestry_auc.png {BUCKET}/reports/")

    # Print summary
    logger.info("\n" + "=" * 60)
    logger.info("ANALYSIS COMPLETE")
    logger.info("=" * 60)

    logger.info("\nRisk Reports (first 5 individuals):")
    logger.info(risk_reports.head().to_string(index=False))

    logger.info("\nOverall Model Performance:")
    overall = eval_df[eval_df["ancestry"] == "ALL"]
    for _, row in overall.iterrows():
        logger.info(
            f"  {row['disease']}: AUC={row['auc_roc']:.4f}, "
            f"Brier={row['brier_score']:.4f}, "
            f"N={row['n_samples']} ({row['n_cases']} cases)"
        )

    logger.info(f"\nOutput files: {OUTPUT_DIR}")
    logger.info(f"BigQuery tables: {DATASET}.risk_predictions, {DATASET}.model_evaluation")
    logger.info(f"GCS reports: {BUCKET}/reports/")


if __name__ == "__main__":
    main()
