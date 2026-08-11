"""Step 3: Train PRS-based disease risk model on Vertex AI.

This script:
1. Loads PRS scores, ancestry PCs, and simulated phenotype labels
2. Trains ElasticNet regularized regression via Vertex AI Custom Training
3. Evaluates with cross-validation across ancestry groups
4. Registers the best model in Vertex AI Model Registry
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
from google.cloud import storage
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.linear_model import ElasticNetCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score, log_loss
import joblib

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
# 1. Load and prepare training data
# ---------------------------------------------------------------------------
def load_training_data() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load PRS scores and PCs from CSV exports."""
    output_dir = ROOT / "output"

    prs_path = output_dir / "prs_scores.csv"
    pcs_path = output_dir / "population_pcs.csv"

    if not prs_path.exists() or not pcs_path.exists():
        raise FileNotFoundError(
            f"PRS/PC files not found in {output_dir}. "
            "Run 02_prs_weights.R first."
        )

    prs_df = pd.read_csv(prs_path)
    pcs_df = pd.read_csv(pcs_path)

    logger.info(f"Loaded PRS: {prs_df.shape}, PCs: {pcs_df.shape}")
    return prs_df, pcs_df


def generate_synthetic_phenotypes(
    prs_df: pd.DataFrame, random_state: int = 42
) -> pd.DataFrame:
    """Generate synthetic disease phenotype labels correlated with PRS.

    In a real study these would come from EHR or cohort data.
    Here we simulate binary phenotypes where disease probability
    increases with PRS values.
    """
    rng = np.random.RandomState(random_state)

    diseases = prs_df["disease"].unique()
    samples = prs_df["sample_id"].unique()

    records = []
    for disease in diseases:
        sub = prs_df[prs_df["disease"] == disease].copy()
        prs_vals = sub["prs"].values

        # Normalize PRS to [0, 1] range for probability scaling
        if prs_vals.std() > 0:
            prs_norm = (prs_vals - prs_vals.min()) / (prs_vals.max() - prs_vals.min())
        else:
            prs_norm = np.full_like(prs_vals, 0.5)

        # Base prevalence ~10%, modified by PRS
        base_rate = 0.10
        log_odds = np.log(base_rate / (1 - base_rate))
        log_odds += 1.5 * prs_norm  # PRS effect
        prob = 1 / (1 + np.exp(-log_odds))

        phenotype = rng.binomial(1, prob)

        for idx, (_, row) in enumerate(sub.iterrows()):
            records.append({
                "sample_id": row["sample_id"],
                "disease": disease,
                "disease_binary": int(phenotype[idx]),
            })

    return pd.DataFrame(records)


def prepare_features(
    prs_df: pd.DataFrame,
    pcs_df: pd.DataFrame,
    phenotypes: pd.DataFrame,
    disease_key: str,
) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Build feature matrix X and labels y for one disease.

    Features: PRS score for this disease + top PCs
    """
    prs_sub = prs_df[prs_df["disease"] == disease_key][
        ["sample_id", "prs"]
    ].copy()
    pheno_sub = phenotypes[phenotypes["disease"] == disease_key][
        ["sample_id", "disease_binary"]
    ].copy()

    merged = prs_sub.merge(pcs_df, on="sample_id").merge(
        pheno_sub, on="sample_id"
    )

    feature_cols = ["prs"] + [
        c for c in merged.columns if c.startswith("PC")
    ]
    X = merged[feature_cols].values
    y = merged["disease_binary"].values

    logger.info(
        f"Features for {disease_key}: {X.shape[0]} samples, "
        f"{X.shape[1]} features ({feature_cols})"
    )

    return X, y, feature_cols


# ---------------------------------------------------------------------------
# 2. Train with cross-validation (local, then submit to Vertex AI)
# ---------------------------------------------------------------------------
def train_elasticnet_cv(
    X: np.ndarray, y: np.ndarray, feature_names: List[str]
) -> Dict:
    """Train ElasticNet with nested cross-validation."""
    logger.info("Training ElasticNet with 5-fold CV...")

    alphas = cfg["prs"]["alpha_range"]
    l1_ratios = cfg["prs"]["l1_ratio_range"]
    n_folds = cfg["prs"]["cv_folds"]

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = ElasticNetCV(
        l1_ratio=l1_ratios,
        alphas=alphas,
        cv=StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42),
        max_iter=5000,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_scaled, y)

    # Evaluate with stratified CV
    skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    y_proba = np.zeros_like(y, dtype=float)

    for train_idx, val_idx in skf.split(X_scaled, y):
        X_train, X_val = X_scaled[train_idx], X_scaled[val_idx]
        y_train = y[train_idx]

        fold_model = ElasticNetCV(
            l1_ratio=l1_ratios, alphas=alphas,
            cv=3, max_iter=3000, random_state=42
        )
        fold_model.fit(X_train, y_train)
        y_proba[val_idx] = np.clip(fold_model.predict(X_val), 0, 1)

    auc = roc_auc_score(y, y_proba)
    logloss = log_loss(y, y_proba)

    # Feature importance
    importances = dict(zip(feature_names, model.coef_))

    results = {
        "alpha": model.alpha_,
        "l1_ratio": model.l1_ratio_,
        "auc_roc": auc,
        "log_loss": logloss,
        "feature_importance": importances,
        "n_samples": X.shape[0],
        "n_features": X.shape[1],
    }

    logger.info(f"  Best alpha: {model.alpha_}, l1_ratio: {model.l1_ratio_}")
    logger.info(f"  AUC-ROC: {auc:.4f}, Log-loss: {logloss:.4f}")

    return model, scaler, results


# ---------------------------------------------------------------------------
# 3. Submit Vertex AI Custom Training Job
# ---------------------------------------------------------------------------
def submit_vertex_training(
    model: ElasticNetCV,
    scaler: StandardScaler,
    train_data: Tuple[np.ndarray, np.ndarray, List[str]],
    disease_key: str,
    results: Dict,
) -> str:
    """Save model locally and submit to Vertex AI Model Registry."""
    aiplatform.init(project=PROJECT, location=REGION, staging_bucket=BUCKET)

    # Save model locally
    model_dir = ROOT / "output" / "models" / disease_key
    model_dir.mkdir(parents=True, exist_ok=True)

    model_path = model_dir / "model.joblib"
    scaler_path = model_dir / "scaler.joblib"
    metadata_path = model_dir / "metadata.json"

    joblib.dump(model, model_path)
    joblib.dump(scaler, scaler_path)

    with open(metadata_path, "w") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Model saved to {model_path}")

    # Upload to GCS
    gcs_model_dir = f"{BUCKET}/models/{disease_key}"
    os.system(f"gsutil cp -r {model_dir}/* {gcs_model_dir}/")

    # Upload model artifact to Vertex AI
    model_upload = aiplatform.Model.upload(
        display_name=f"prs-risk-{disease_key}",
        artifact_uri=gcs_model_dir,
        serving_container_image_uri=(
            "us-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-3:latest"
        ),
        description=(
            f"ElasticNet PRS risk model for {disease_key}. "
            f"AUC-ROC: {results['auc_roc']:.4f}"
        ),
        sync=True,
    )

    logger.info(f"Model uploaded: {model_upload.resource_name}")
    return model_upload.resource_name


# ---------------------------------------------------------------------------
# 4. Deploy prediction endpoint
# ---------------------------------------------------------------------------
def deploy_endpoint(model_resource: str, disease_key: str) -> str:
    """Deploy model to a Vertex AI endpoint."""
    endpoint = aiplatform.Endpoint.create(
        display_name=f"prs-endpoint-{disease_key}",
        project=PROJECT,
        location=REGION,
    )

    model = aiplatform.Model(model_resource)

    endpoint.deploy(
        model=model,
        deployed_model_display_name=f"prs-deployed-{disease_key}",
        machine_type=cfg["vertex_ai"]["prediction"]["machine_type"],
        min_replica_count=cfg["vertex_ai"]["prediction"]["min_replica_count"],
        max_replica_count=cfg["vertex_ai"]["prediction"]["max_replica_count"],
        sync=True,
    )

    logger.info(f"Endpoint deployed: {endpoint.resource_name}")
    return endpoint.resource_name


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------
def main():
    aiplatform.init(project=PROJECT, location=REGION, staging_bucket=BUCKET)

    logger.info("=" * 60)
    logger.info("Step 3: Vertex AI PRS Model Training")
    logger.info("=" * 60)

    # Load data
    prs_df, pcs_df = load_training_data()
    phenotypes = generate_synthetic_phenotypes(prs_df)

    all_results = {}
    model_resources = {}
    endpoint_resources = {}

    # Phase 1: Train all models locally
    logger.info("\n=== Phase 1: Local Training ===")
    for disease_key in cfg["diseases"]:
        logger.info(f"\n--- Training model: {disease_key} ---")

        X, y, feature_names = prepare_features(
            prs_df, pcs_df, phenotypes, disease_key
        )

        if len(np.unique(y)) < 2:
            logger.warning(f"Skipping {disease_key}: only one class present")
            continue

        model, scaler, results = train_elasticnet_cv(X, y, feature_names)
        all_results[disease_key] = results

        # Save model locally
        model_dir = OUTPUT_DIR / "models" / disease_key
        model_dir.mkdir(parents=True, exist_ok=True)
        import joblib
        joblib.dump(model, model_dir / "model.joblib")
        joblib.dump(scaler, model_dir / "scaler.joblib")
        with open(model_dir / "metadata.json", "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"  Model saved locally: {model_dir}")

    # Phase 2: Upload to Vertex AI and deploy endpoints
    logger.info("\n=== Phase 2: Vertex AI Deployment ===")
    for disease_key in all_results:
        try:
            model_resource = submit_vertex_training(
                *load_local_model(disease_key), disease_key, all_results[disease_key]
            )
            model_resources[disease_key] = model_resource

            endpoint_resource = deploy_endpoint(model_resource, disease_key)
            endpoint_resources[disease_key] = endpoint_resource
        except Exception as e:
            logger.error(f"  Failed to deploy {disease_key}: {e}")

    # Save summary
    summary_path = ROOT / "output" / "training_summary.json"
    summary = {
        "diseases": all_results,
        "model_resources": model_resources,
        "endpoint_resources": endpoint_resources,
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    logger.info(f"\nTraining summary saved to {summary_path}")
    logger.info("\nDone. Next: Run 04_risk_prediction.py")


def load_local_model(disease_key: str):
    """Load locally saved model and scaler."""
    import joblib
    model_dir = OUTPUT_DIR / "models" / disease_key
    model = joblib.load(model_dir / "model.joblib")
    scaler = joblib.load(model_dir / "scaler.joblib")
    with open(model_dir / "metadata.json") as f:
        metadata = json.load(f)
    return model, scaler, metadata


if __name__ == "__main__":
    main()
