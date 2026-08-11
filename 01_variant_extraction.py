"""Step 1: Extract disease-associated variants from 1000 Genomes via BigQuery."""

import yaml
import pandas as pd
from google.cloud import bigquery
from google.cloud import storage
from pathlib import Path

ROOT = Path(__file__).parent
with open(ROOT / "config.yaml") as f:
    cfg = yaml.safe_load(f)

PROJECT = cfg["gcp"]["project_id"]
DATASET = cfg["bigquery"]["dataset"]
BUCKET = cfg["gcp"]["bucket"]

TABLE = "bigquery-public-data.human_genome_variants.1000_genomes_phase_3_variants_20150220"

client = bigquery.Client(project=PROJECT)


def build_variant_list():
    """Build a flat list of target variants from config."""
    variants = []
    for disease_key, disease in cfg["diseases"].items():
        for v in disease["variants"]:
            variants.append({
                "disease": disease["name"],
                "disease_key": disease_key,
                "gene": v["gene"],
                "rsid": v["rsid"],
                "chrom": v["chromosome"].replace("chr", ""),
                "pos": v["position"],
            })
    return pd.DataFrame(variants)


def query_genotypes(variants_df):
    """Query 1000 Genomes Phase 3 genotypes for target variants.

    Each row in the source table has a `call` RECORD array with per-sample
    genotype data (2504 samples). We unnest to get one row per sample-variant.
    """
    rsid_list = ", ".join(f'"{r}"' for r in variants_df["rsid"].unique())

    query = f"""
    SELECT
        v.names[OFFSET(0)] AS rsid,
        v.reference_name AS chrom,
        v.start_position AS pos,
        v.reference_bases AS ref,
        v.alternate_bases[OFFSET(0)].alt AS alt,
        v.alternate_bases[OFFSET(0)].AF AS global_af,
        v.alternate_bases[OFFSET(0)].AFR_AF AS afr_af,
        v.alternate_bases[OFFSET(0)].AMR_AF AS amr_af,
        v.alternate_bases[OFFSET(0)].EUR_AF AS eur_af,
        v.alternate_bases[OFFSET(0)].SAS_AF AS sas_af,
        v.alternate_bases[OFFSET(0)].EAS_AF AS eas_af,
        call.name AS sample_id,
        call.genotype AS genotype_array,
        CONCAT(
            CAST(call.genotype[OFFSET(0)] AS STRING),
            '/',
            CAST(call.genotype[OFFSET(1)] AS STRING)
        ) AS geno
    FROM `{TABLE}` v, UNNEST(v.call) AS call
    WHERE EXISTS(
        SELECT 1 FROM UNNEST(v.names) AS n WHERE n IN ({rsid_list})
    )
    """

    print(f"Querying BigQuery for {len(variants_df['rsid'].unique())} rsIDs...")
    df = client.query(query).to_dataframe()
    print(f"  Retrieved {len(df)} rows, {df['sample_id'].nunique()} samples")
    return df


def compute_genotype_matrix(genotypes_df, variants_df):
    """Pivot genotypes into a sample x variant dosage matrix.

    Dosage encoding:
      0 = homozygous reference (0/0, 0|0)
      1 = heterozygous (0/1, 1/0, 0|1, 1|0)
      2 = homozygous alternate (1/1, 1|1)
    """
    geno_map = {
        "0/0": 0, "0|0": 0,
        "0/1": 1, "0|1": 1, "1/0": 1, "1|0": 1,
        "1/1": 2, "1|1": 2,
    }

    genotypes_df = genotypes_df.copy()
    genotypes_df["dosage"] = genotypes_df["geno"].map(geno_map)

    if genotypes_df["dosage"].isna().any():
        unmapped = genotypes_df.loc[genotypes_df["dosage"].isna(), "geno"].unique()
        print(f"  WARNING: Unmapped genotypes: {unmapped}")
        genotypes_df = genotypes_df.dropna(subset=["dosage"])

    genotypes_df["dosage"] = genotypes_df["dosage"].astype(int)

    matrix = genotypes_df.pivot_table(
        index="sample_id",
        columns="rsid",
        values="dosage",
        aggfunc="first",
    )

    matrix.index.name = "sample_id"
    matrix = matrix.fillna(0).astype(int)

    print(f"  Genotype matrix: {matrix.shape[0]} samples x {matrix.shape[1]} variants")
    return matrix


def extract_variant_info(genotypes_df):
    """Extract allele frequency info per variant."""
    info = genotypes_df.drop_duplicates(subset=["rsid"])[
        ["rsid", "chrom", "pos", "ref", "alt", "global_af",
         "afr_af", "amr_af", "eur_af", "sas_af", "eas_af"]
    ].copy()
    return info


def extract_ancestry_from_sample_ids(genotypes_df):
    """Infer ancestry from 1000 Genomes sample ID prefix.

    1000 Genomes sample IDs encode population:
      HG00096 = EUR, HG01243 = AFR, NA19700 = AMR, etc.
    We use the sample_info table for reliable mapping.
    """
    print("Querying sample ancestry from BigQuery...")
    query = f"""
    SELECT
        Sample AS sample_id,
        Population AS population,
        Super_Population AS super_population
    FROM `bigquery-public-data.human_genome_variants.1000_genomes_sample_info`
    """

    ancestry_df = client.query(query).to_dataframe()
    print(f"  Loaded {len(ancestry_df)} sample records")

    # Filter to samples present in our genotype data
    samples_in_data = set(genotypes_df["sample_id"].unique())
    ancestry_df = ancestry_df[ancestry_df["sample_id"].isin(samples_in_data)].copy()
    print(f"  Matched {len(ancestry_df)} samples in our dataset")
    return ancestry_df[["sample_id", "population", "super_population"]]


def save_to_bigquery(df, table_name, description):
    """Save DataFrame to BigQuery."""
    table_id = f"{PROJECT}.{DATASET}.{table_name}"

    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()
    print(f"  Saved to {table_id}: {len(df)} rows")


def save_to_gcs(df, filename):
    """Upload DataFrame as parquet to GCS."""
    local_path = ROOT / filename
    df.to_parquet(local_path, index=True)

    bucket = storage.Client(project=PROJECT).bucket(BUCKET.replace("gs://", ""))
    blob = bucket.blob(f"variants/{filename}")
    blob.upload_from_filename(str(local_path))
    print(f"  Uploaded to {BUCKET}/variants/{filename}")


def main():
    print("=" * 60)
    print("Step 1: Extract Disease-Associated Variants")
    print("=" * 60)

    # 1. Build variant list
    variants_df = build_variant_list()
    print(f"\nTarget variants: {len(variants_df)}")
    print(variants_df[["disease", "gene", "rsid", "chrom", "pos"]].to_string(index=False))

    # 2. Query genotypes from BigQuery
    genotypes_df = query_genotypes(variants_df)

    # 3. Build genotype dosage matrix
    genotype_matrix = compute_genotype_matrix(genotypes_df, variants_df)

    # 4. Extract variant info (allele frequencies)
    variant_info = extract_variant_info(genotypes_df)

    # 5. Annotate ancestry from sample info table
    ancestry = extract_ancestry_from_sample_ids(genotypes_df)

    # 6. Save outputs
    print("\nSaving outputs...")
    save_to_bigquery(genotype_matrix.reset_index(), "genotype_matrix",
                     "1000 Genomes dosage matrix for disease-associated variants")
    save_to_bigquery(ancestry, "sample_ancestry",
                     "1000 Genomes sample ancestry labels")
    save_to_bigquery(variant_info.reset_index(drop=True), "target_variants",
                     "Disease-associated variant annotations with allele frequencies")

    save_to_gcs(genotype_matrix, "genotype_matrix.parquet")
    save_to_gcs(ancestry, "sample_ancestry.parquet")
    save_to_gcs(variant_info, "target_variants.parquet")

    # Also save locally for Steps 2-4
    genotype_matrix.to_parquet(ROOT / "output" / "genotype_matrix.parquet")
    ancestry.to_csv(ROOT / "output" / "sample_ancestry.csv", index=False)
    variant_info.to_csv(ROOT / "output" / "target_variants.csv", index=False)

    print("\n" + "=" * 60)
    print("Done. Next: Run 02_prs_weights.R")
    print("=" * 60)


if __name__ == "__main__":
    main()
