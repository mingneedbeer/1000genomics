#!/usr/bin/env Rscript
# ============================================================================
# Step 2: PRS Weight Calculation using GWAS Summary Statistics
# ============================================================================
# This script fetches GWAS Catalog summary statistics for target diseases,
# calculates polygenic risk scores for each 1000 Genomes individual, and
# exports scores for downstream ML training.
# ============================================================================

suppressPackageStartupMessages({
  library(data.table)
  library(yaml)
  library(curl)
})

ROOT <- dirname(sys.frame(1)$ofile)
cfg <- yaml::read_yaml(file.path(ROOT, "config.yaml"))

PROJECT <- cfg$gcp$project_id
DATASET <- cfg$bigquery$dataset
BUCKET <- cfg$gcp$bucket

# ---------------------------------------------------------------------------
# 1. Load GWAS Catalog summary statistics from public FTP
# ---------------------------------------------------------------------------
fetch_gwas_sumstats <- function(efo_id, disease_name) {
  cat(sprintf("Fetching GWAS Catalog stats for: %s (%s)\n", disease_name, efo_id))

  base_url <- "https://www.ebi.ac.uk/gwas/rest/api/studies"
  url <- sprintf("%s/search/findByEfo?efo=%s", base_url, efo_id)

  resp <- tryCatch(
    curl::curl_fetch_memory(url),
    error = function(e) {
      cat(sprintf("  WARN: GWAS API fetch failed: %s\n", e$message))
      return(NULL)
    }
  )

  if (is.null(resp)) return(NULL)

  data <- jsonlite::fromJSON(rawToChar(resp$content))
  if (length(data$_embedded$studies) == 0) {
    cat("  No studies found\n")
    return(NULL)
  }

  # Get the first study with summary stats
  study <- data$_embedded$studies[1, ]
  cat(sprintf("  Study: %s\n", study$authorShort))

  # Fetch SNP associations
  assoc_url <- sprintf("%s/studies/%s/associations",
                       base_url, study$accession)
  assoc_resp <- curl::curl_fetch_memory(assoc_url)
  assoc_data <- jsonlite::fromJSON(rawToChar(assoc_resp$content))

  if (length(assoc_data$_embedded$associations) == 0) return(NULL)

  assocs <- assoc_data$_embedded$associations
  results <- data.table()

  for (i in seq_len(nrow(assocs))) {
    row <- assocs[i, ]

    if (is.null(row$riskAlleles) || length(row$riskAlleles) == 0) next

    for (j in seq_along(row$riskAlleles)) {
      allele <- row$riskAlleles[[j]]
      if (is.null(allele$riskFrequency) || is.null(allele$effects)) next

      for (k in seq_along(allele$effects)) {
        eff <- allele$effects[[k]]
        beta_val <- tryCatch(as.numeric(eff$beta), error = function(e) NA_real_)
        pval <- tryCatch(as.numeric(eff$pvalue), error = function(e) NA_real_)
        or_val <- tryCatch(as.numeric(eff$oddsRatio), error = function(e) NA_real_)

        # Convert OR to log-odds if beta missing
        if (is.na(beta_val) && !is.na(or_val)) beta_val <- log(or_val)

        results <- rbind(results, data.table(
          rsid = allele$riskAllele$variation$rsID %||% NA_character_,
          risk_allele = allele$riskAllele$riskAllele,
          beta = beta_val,
          pvalue = pval,
          or = or_val,
          freq = as.numeric(allele$riskFrequency),
          disease = disease_name
        ))
      }
    }
  }

  results <- results[!is.na(beta) & !is.na(pvalue) & pvalue > 0]
  results <- unique(results, by = "rsid")

  cat(sprintf("  Found %d SNP-effect pairs\n", nrow(results)))
  return(results)
}

`%||%` <- function(a, b) if (!is.null(a)) a else b


# ---------------------------------------------------------------------------
# 2. Read genotype matrix from local parquet (exported by Step 1)
# ---------------------------------------------------------------------------
read_genotype_matrix <- function() {
  cat("Reading genotype matrix...\n")
  gmat_path <- file.path(ROOT, "genotype_matrix.parquet")

  if (!file.exists(gmat_path)) {
    stop("genotype_matrix.parquet not found. Run 01_variant_extraction.py first.")
  }

  gmat <- arrow::read_parquet(gmat_path)
  cat(sprintf("  Loaded %d samples x %d variants\n", nrow(gmat), ncol(gmat) - 1))
  return(gmat)
}


# ---------------------------------------------------------------------------
# 3. Calculate PRS per individual per disease
# ---------------------------------------------------------------------------
calculate_prs <- function(gmat, gwas_stats, disease_key) {
  cat(sprintf("Calculating PRS for: %s\n", disease_key))

  variant_col <- names(gmat)[1]
  sample_ids <- gmat[[variant_col]]
  snp_cols <- names(gmat)[-1]

  # Match variants between genotype matrix and GWAS stats
  matched <- merge(
    data.table(rsid = snp_cols),
    gwas_stats[, .(rsid, beta, pvalue)],
    by = "rsid",
    all.x = FALSE
  )

  cat(sprintf("  Matched %d / %d variants with GWAS stats\n",
              nrow(matched), length(snp_cols)))

  if (nrow(matched) == 0) {
    warning(sprintf("No variants matched for %s", disease_key))
    return(data.table(sample_id = sample_ids, prs = NA_real_,
                      disease = disease_key))
  }

  # PRS = sum(dosage_i * beta_i)
  dosages <- as.matrix(gmat[, matched$rsid, with = FALSE])
  betas <- matched$beta

  prs <- dosages %*% betas
  prs <- as.numeric(prs)

  result <- data.table(
    sample_id = sample_ids,
    prs = prs,
    disease = disease_key,
    n_snps = nrow(matched)
  )

  cat(sprintf("  PRS range: [%.2f, %.2f], mean: %.2f\n",
              min(prs), max(prs), mean(prs)))

  return(result)
}


# ---------------------------------------------------------------------------
# 4. Population stratification: compute PCs using flashpca2
# ---------------------------------------------------------------------------
compute_pcs <- function(gmat, n_pcs = 10) {
  cat("Computing population PCs via flashpca2...\n")

  variant_col <- names(gmat)[1]
  sample_ids <- gmat[[variant_col]]
  dosages <- as.matrix(gmat[, -1, with = FALSE])

  # Write temporary PLINK files for flashpca2
  tmp_prefix <- file.path(tempdir(), "tmp_pca")

  # .bed file
  bed <- matrix(as.raw(dosages), nrow = nrow(dosages))
  writeBin(as.vector(bed), paste0(tmp_prefix, ".bed"))

  # .bim file
  bim <- data.table(
    chr = 1, snp = colnames(dosages),
    pos = seq_along(colnames(dosages)),
    pos2 = seq_along(colnames(dosages)),
    A1 = "A", A2 = "G"
  )
  fwrite(bim, paste0(tmp_prefix, ".bim"), sep = "\t", col.names = FALSE)

  # .fam file
  fam <- data.table(
    fam1 = 1, iid = sample_ids,
    fid = 0, pid = 0, sex = 0, pheno = -9
  )
  fwrite(fam, paste0(tmp_prefix, ".fam"), sep = "\t", col.names = FALSE)

  # Run flashpca2
  pc_file <- paste0(tmp_prefix, ".pcs.txt")
  cmd <- sprintf("flashpca2 --bfile %s --ndim %d --out %s 2>&1",
                 tmp_prefix, n_pcs, pc_file)

  result <- tryCatch(
    system(cmd, intern = TRUE),
    error = function(e) {
      cat("  flashpca2 not available, falling back to SVD\n")
      return(NULL)
    }
  )

  if (!is.null(result) && file.exists(pc_file)) {
    pcs <- fread(pc_file)
    cat(sprintf("  Computed %d PCs\n", ncol(pcs) - 1))
    return(pcs)
  }

  # Fallback: PCA via SVD
  cat("  Using SVD-based PCA\n")
  scaled <- scale(dosages, center = TRUE, scale = FALSE)
  svd_result <- svd(scaled, nu = n_pcs, nv = 0)
  pc_matrix <- svd_result$u[, 1:n_pcs] * svd_result$d[1:n_pcs]

  pcs <- data.table(sample_id = sample_ids, as.data.table(pc_matrix))
  names(pcs) <- c("sample_id", paste0("PC", 1:n_pcs))

  return(pcs)
}


# ---------------------------------------------------------------------------
# 5. Main execution
# ---------------------------------------------------------------------------
main() {
  cat("=" |> rep(60) |> paste(collapse = ""), "\n")
  cat("Step 2: PRS Weight Calculation\n")
  cat("=" |> rep(60) |> paste(collapse = ""), "\n\n")

  # Load genotype data
  gmat <- read_genotype_matrix()

  # Fetch GWAS stats for each disease
  all_prs <- list()

  for (disease_key in names(cfg$diseases)) {
    disease <- cfg$diseases[[disease_key]]
    efo_id <- disease$gwas_efo

    gwas_stats <- fetch_gwas_sumstats(efo_id, disease$name)

    if (is.null(gwas_stats) || nrow(gwas_stats) == 0) {
      # Fallback: use curated candidate variants from config
      cat(sprintf("  Using curated candidate variants for %s\n", disease$name))
      gwas_stats <- rbindlist(lapply(disease$variants, function(v) {
        data.table(
          rsid = v$rsid,
          risk_allele = "A",
          beta = 0.3,   # placeholder effect sizes
          pvalue = 1e-5,
          or = 1.35,
          freq = 0.3,
          disease = disease$name
        )
      }))
    }

    prs <- calculate_prs(gmat, gwas_stats, disease_key)
    all_prs[[disease_key]] <- prs
  }

  # Combine PRS across diseases
  combined_prs <- rbindlist(all_prs)

  # Compute ancestry PCs
  pcs <- compute_pcs(gmat, n_pcs = cfg$prs$n_pcs)

  # Save PRS scores
  out_dir <- file.path(ROOT, "output")
  dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)

  fwrite(combined_prs, file.path(out_dir, "prs_scores.csv"))
  fwrite(pcs, file.path(out_dir, "population_pcs.csv"))

  cat(sprintf("\nSaved PRS scores: %d rows\n", nrow(combined_prs)))
  cat(sprintf("Saved PCs: %d samples x %d components\n", nrow(pcs), cfg$prs$n_pcs))

  # Upload to GCS
  system(sprintf(
    "gsutil cp %s/gs://%s/prs_scores.csv",
    file.path(out_dir, "prs_scores.csv"),
    BUCKET
  ))
  system(sprintf(
    "gsutil cp %s/gs://%s/population_pcs.csv",
    file.path(out_dir, "population_pcs.csv"),
    BUCKET
  ))

  cat("\nDone. Next: Run 03_vertex_prs_train.py\n")
}

main()
