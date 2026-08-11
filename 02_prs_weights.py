"""Step 2: PRS Weight Calculation using GWAS Catalog Summary Statistics.

Fetches GWAS associations from the GWAS REST API for each disease,
calculates polygenic risk scores for 1000 Genomes individuals,
and computes population PCs for stratification adjustment.
"""

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
with open(ROOT / "config.yaml") as f:
    cfg = yaml.safe_load(f)

OUTPUT_DIR = ROOT / "output"


# ---------------------------------------------------------------------------
# 1. Fetch GWAS summary statistics from GWAS Catalog REST API
# ---------------------------------------------------------------------------
def fetch_gwas_sumstats(efo_id: str, disease_name: str) -> pd.DataFrame:
    """Fetch GWAS Catalog association data for a given EFO trait."""
    logger.info(f"Fetching GWAS stats for: {disease_name} ({efo_id})")

    base_url = "https://www.ebi.ac.uk/gwas/rest/api"

    # Find studies associated with this EFO term
    study_url = f"{base_url}/studies/search/findByEfo?efo={efo_id}"
    try:
        resp = requests.get(study_url, timeout=30)
        resp.raise_for_status()
        studies = resp.json()["_embedded"]["studies"]
    except Exception as e:
        logger.warning(f"  GWAS API fetch failed: {e}")
        return pd.DataFrame()

    if not studies:
        logger.warning(f"  No studies found for {efo_id}")
        return pd.DataFrame()

    logger.info(f"  Found {len(studies)} studies")

    # Get associations from the first study
    study = studies[0]
    assoc_url = study["_links"]["associations"]["href"]

    try:
        resp = requests.get(assoc_url, timeout=30)
        resp.raise_for_status()
        associations = resp.json()["_embedded"]["associations"]
    except Exception as e:
        logger.warning(f"  Failed to fetch associations: {e}")
        return pd.DataFrame()

    records = []
    for assoc in associations:
        for risk_allele in assoc.get("riskAlleles", []):
            variation = risk_allele.get("variation", {})
            rsid = variation.get("rsID")
            if not rsid:
                continue

            for effect in risk_allele.get("effects", []):
                beta = effect.get("beta")
                pvalue = effect.get("pvalue")
                or_val = effect.get("oddsRatio")

                beta_f = float(beta) if beta else None
                pval_f = float(pvalue) if pvalue else None

                # Convert OR to log-odds if beta is missing
                if beta_f is None and or_val:
                    beta_f = np.log(float(or_val))

                if beta_f is not None and pval_f is not None and pval_f > 0:
                    records.append({
                        "rsid": rsid,
                        "risk_allele": risk_allele.get("riskAllele", ""),
                        "beta": beta_f,
                        "pvalue": pval_f,
                        "freq": float(risk_allele.get("riskFrequency", 0)),
                        "disease": disease_name,
                    })

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.drop_duplicates(subset=["rsid"])
        logger.info(f"  Found {len(df)} SNP-effect pairs")
    return df


# ---------------------------------------------------------------------------
# 2. Use curated candidate variants as fallback
# ---------------------------------------------------------------------------
def get_candidate_variants() -> pd.DataFrame:
    """Build GWAS-like summary stats from curated config variants."""
    records = []
    for disease_key, disease in cfg["diseases"].items():
        for v in disease["variants"]:
            # Use realistic effect sizes from literature
            effect_sizes = {
                "rs429358": 0.45,    # APOE e4
                "rs10455872": 0.35,  # LPA
                "rs10757278": 0.25,  # 9p21
                "rs688": 0.15,       # LDLR
                "rs7903146": 0.30,   # TCF7L2
                "rs1801282": 0.20,   # PPARG
                "rs5219": 0.18,      # KCNJ11
                "rs9939609": 0.22,   # FTO
                "rs17782313": 0.15,  # MC4R
                "rs6457617": 0.50,   # HLA-DRB1
                "rs11209026": -0.40, # IL23R (protective)
                "rs3087243": 0.20,   # CTLA4
            }

            records.append({
                "rsid": v["rsid"],
                "risk_allele": "A",
                "beta": effect_sizes.get(v["rsid"], 0.2),
                "pvalue": 1e-6,
                "freq": 0.3,
                "disease": disease["name"],
            })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# 3. Load genotype matrix from Step 1
# ---------------------------------------------------------------------------
def load_genotype_matrix() -> pd.DataFrame:
    """Load the dosage matrix from parquet."""
    path = OUTPUT_DIR / "genotype_matrix.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Run 01_variant_extraction.py first: {path}")
    gmat = pd.read_parquet(path).reset_index()
    logger.info(f"Loaded genotype matrix: {gmat.shape}")
    return gmat


# ---------------------------------------------------------------------------
# 4. Calculate PRS per individual per disease
# ---------------------------------------------------------------------------
def calculate_prs(gmat: pd.DataFrame, gwas_stats: pd.DataFrame, disease_key: str) -> pd.DataFrame:
    """PRS = sum(dosage_i * beta_i) for matched variants."""
    logger.info(f"Calculating PRS for: {disease_key}")

    sample_ids = gmat["sample_id"]
    snp_cols = [c for c in gmat.columns if c != "sample_id"]

    # Match variants
    matched = gwas_stats[gwas_stats["rsid"].isin(snp_cols)]
    logger.info(f"  Matched {len(matched)} / {len(snp_cols)} variants with GWAS stats")

    if matched.empty:
        logger.warning(f"  No variants matched for {disease_key}")
        return pd.DataFrame({"sample_id": sample_ids, "prs": 0.0, "disease": disease_key})

    dosages = gmat[matched["rsid"].values].values
    betas = matched["beta"].values

    prs = dosages @ betas

    result = pd.DataFrame({
        "sample_id": sample_ids.values,
        "prs": prs,
        "disease": disease_key,
        "n_snps": len(matched),
    })

    logger.info(f"  PRS range: [{prs.min():.2f}, {prs.max():.2f}], mean: {prs.mean():.2f}")
    return result


# ---------------------------------------------------------------------------
# 5. Compute population PCs
# ---------------------------------------------------------------------------
def compute_pcs(gmat: pd.DataFrame, n_pcs: int = 10) -> pd.DataFrame:
    """Compute principal components from genotype matrix."""
    logger.info(f"Computing top {n_pcs} PCs...")

    snp_cols = [c for c in gmat.columns if c != "sample_id"]
    dosages = gmat[snp_cols].values.astype(float)

    # Standardize
    scaler = StandardScaler()
    dosages_scaled = scaler.fit_transform(dosages)

    pca = PCA(n_components=min(n_pcs, len(snp_cols)))
    pc_values = pca.fit_transform(dosages_scaled)

    pc_df = pd.DataFrame(pc_values, columns=[f"PC{i+1}" for i in range(pc_values.shape[1])])
    pc_df.insert(0, "sample_id", gmat["sample_id"].values)

    logger.info(f"  Explained variance: {pca.explained_variance_ratio_.sum():.2%}")
    return pc_df


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("Step 2: PRS Weight Calculation (Python)")
    logger.info("=" * 60)

    # Load genotype matrix
    gmat = load_genotype_matrix()

    # Try GWAS Catalog API, fallback to curated variants
    all_prs = []

    for disease_key, disease in cfg["diseases"].items():
        gwas_stats = fetch_gwas_sumstats(disease["gwas_efo"], disease["name"])

        if gwas_stats.empty:
            logger.info(f"  Using curated candidate variants for {disease['name']}")
            gwas_stats = get_candidate_variants()
            gwas_stats = gwas_stats[gwas_stats["disease"] == disease["name"]]

        prs = calculate_prs(gmat, gwas_stats, disease_key)
        all_prs.append(prs)

    # Combine PRS across diseases
    combined_prs = pd.concat(all_prs, ignore_index=True)

    # Compute PCs
    pcs = compute_pcs(gmat, n_pcs=cfg["prs"]["n_pcs"])

    # Save
    combined_prs.to_csv(OUTPUT_DIR / "prs_scores.csv", index=False)
    pcs.to_csv(OUTPUT_DIR / "population_pcs.csv", index=False)

    logger.info(f"\nSaved PRS scores: {combined_prs.shape}")
    logger.info(f"Saved PCs: {pcs.shape}")

    # Summary
    logger.info("\nPRS Summary by Disease:")
    summary = combined_prs.groupby("disease")["prs"].agg(["count", "mean", "std", "min", "max"])
    logger.info(f"\n{summary.to_string()}")

    logger.info("\nDone. Next: Run 03_vertex_prs_train.py")


if __name__ == "__main__":
    main()
