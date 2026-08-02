#!/usr/bin/env Rscript
# =============================================================================
# map_sdtm_oak.R  --  Production SDTM mapping using the CDISC pharmaverse
#                     package {sdtm.oak}. This is the "real" code-generation
#                     path referenced in the strategy deck (Init 2: generate via
#                     sdtm.oak R). The Python wrapper calls this when R +
#                     sdtm.oak are installed; otherwise it uses the Python
#                     oak-pattern engine, which produces identical output.
#
# Usage:  Rscript R/map_sdtm_oak.R <project_root> <out_dir>
#
# Install once:  install.packages(c("sdtm.oak","dplyr","readr","tidyr"))
# =============================================================================
args <- commandArgs(trailingOnly = TRUE)
root <- if (length(args) >= 1) args[[1]] else "."
outd <- if (length(args) >= 2) args[[2]] else file.path(root, "outputs", "sdtm")
dir.create(outd, showWarnings = FALSE, recursive = TRUE)

suppressPackageStartupMessages({
  library(sdtm.oak)
  library(dplyr)
  library(readr)
  library(tidyr)
})

raw_dir <- file.path(root, "data", "raw_edc")

# sdtm.oak reads CT from a study CT spec. We adapt the project CT file into the
# columns oak expects (codelist_code / collected_value / term_synonyms / term_value).
ct <- read_csv(file.path(root, "ct", "controlled_terminology.csv"),
               show_col_types = FALSE) |>
  transmute(
    codelist_code  = codelist,
    collected_value = decode,
    term_synonyms  = synonyms,
    term_value     = submission_value
  )

usubjid <- function(df) mutate(df, USUBJID = paste(STUDYID, SUBJID, sep = "-"))

# --------------------------------------------------------------------- DM ------
dm_raw <- read_csv(file.path(raw_dir, "dm_raw.csv"), show_col_types = FALSE) |>
  generate_oak_id_vars(pat_var = "SUBJID", raw_src = "dm_raw")

dm <- dm_raw |>
  hardcode_no_ct(raw_var = "SUBJID", tgt_var = "DOMAIN",
                 tgt_val = "DM", id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "STUDYID", tgt_var = "STUDYID", id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "SUBJID",  tgt_var = "SUBJID",  id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "SITEID",  tgt_var = "SITEID",  id_vars = oak_id_vars()) |>
  assign_ct(dm_raw, raw_var = "SEX",    tgt_var = "SEX",    ct_spec = ct, ct_clst = "SEX",    id_vars = oak_id_vars()) |>
  assign_ct(dm_raw, raw_var = "RACE",   tgt_var = "RACE",   ct_spec = ct, ct_clst = "RACE",   id_vars = oak_id_vars()) |>
  assign_ct(dm_raw, raw_var = "ETHNIC", tgt_var = "ETHNIC", ct_spec = ct, ct_clst = "ETHNIC", id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "ARMCD", tgt_var = "ARMCD", id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "ARM",   tgt_var = "ARM",   id_vars = oak_id_vars()) |>
  assign_no_ct(dm_raw, raw_var = "COUNTRY", tgt_var = "COUNTRY", id_vars = oak_id_vars()) |>
  mutate(
    AGE     = as.numeric(dm_raw$AGE),
    AGEU    = dm_raw$AGEU,
    ACTARMCD = ARMCD, ACTARM = ARM,
    RFSTDTC = create_iso8601(dm_raw$FRSTDSDAT, .format = "d-m-y"),
    RFENDTC = create_iso8601(dm_raw$LSTVISDAT, .format = "d-m-y"),
    RFICDTC = create_iso8601(dm_raw$RFICDAT,   .format = "d-m-y"),
    BRTHDTC = create_iso8601(dm_raw$BRTHDAT,   .format = "d-m-y")
  ) |>
  usubjid() |>
  select(STUDYID, DOMAIN, USUBJID, SUBJID, RFSTDTC, RFENDTC, RFICDTC,
         SITEID, BRTHDTC, AGE, AGEU, SEX, RACE, ETHNIC,
         ARMCD, ARM, ACTARMCD, ACTARM, COUNTRY)

write_csv(dm, file.path(outd, "dm.csv"))

# --------------------------------------------------------------------- AE ------
ae_raw <- read_csv(file.path(raw_dir, "ae_raw.csv"), show_col_types = FALSE) |>
  generate_oak_id_vars(pat_var = "SUBJID", raw_src = "ae_raw")

ae <- ae_raw |>
  hardcode_no_ct(raw_var = "SUBJID", tgt_var = "DOMAIN", tgt_val = "AE", id_vars = oak_id_vars()) |>
  assign_no_ct(ae_raw, raw_var = "STUDYID", tgt_var = "STUDYID", id_vars = oak_id_vars()) |>
  assign_no_ct(ae_raw, raw_var = "AETERM",  tgt_var = "AETERM",  id_vars = oak_id_vars()) |>
  assign_no_ct(ae_raw, raw_var = "AEDECOD", tgt_var = "AEDECOD", id_vars = oak_id_vars()) |>
  assign_ct(ae_raw, raw_var = "AESEV", tgt_var = "AESEV", ct_spec = ct, ct_clst = "AESEV", id_vars = oak_id_vars()) |>
  assign_ct(ae_raw, raw_var = "AESER", tgt_var = "AESER", ct_spec = ct, ct_clst = "NY",    id_vars = oak_id_vars()) |>
  assign_ct(ae_raw, raw_var = "AEOUT", tgt_var = "AEOUT", ct_spec = ct, ct_clst = "OUT",   id_vars = oak_id_vars()) |>
  assign_no_ct(ae_raw, raw_var = "AEREL", tgt_var = "AEREL", id_vars = oak_id_vars()) |>
  mutate(
    AEPTCD  = ae_raw$AEPTCD,
    AESTDTC = create_iso8601(ae_raw$AESTDAT, .format = "d-m-y"),
    AEENDTC = create_iso8601(ae_raw$AEENDAT, .format = "d-m-y")
  ) |>
  usubjid() |>
  arrange(USUBJID, AEDECOD, AESTDTC) |>
  derive_seq(tgt_var = "AESEQ", rec_vars = c("USUBJID"), sort_vars = c("AEDECOD", "AESTDTC")) |>
  select(STUDYID, DOMAIN, USUBJID, AESEQ, AETERM, AEDECOD, AEPTCD,
         AESTDTC, AEENDTC, AESEV, AESER, AEREL, AEOUT)

write_csv(ae, file.path(outd, "ae.csv"))

# --------------------------------------------------------------------- VS ------
# Wide -> tall (Findings pattern). oak maps each measurement column to VSTESTCD.
vs_raw <- read_csv(file.path(raw_dir, "vs_raw.csv"), show_col_types = FALSE)

measures <- tibble::tribble(
  ~column,  ~VSTESTCD, ~VSTEST,                       ~VSORRESU,
  "SYSBP",  "SYSBP",   "Systolic Blood Pressure",     "mmHg",
  "DIABP",  "DIABP",   "Diastolic Blood Pressure",    "mmHg",
  "PULSE",  "PULSE",   "Pulse Rate",                  "beats/min",
  "TEMP",   "TEMP",    "Temperature",                 "C",
  "HEIGHT", "HEIGHT",  "Height",                      "cm",
  "WEIGHT", "WEIGHT",  "Weight",                      "kg"
)

vs <- vs_raw |>
  pivot_longer(cols = all_of(measures$column),
               names_to = "column", values_to = "VSORRES") |>
  filter(!is.na(VSORRES) & VSORRES != "") |>
  left_join(measures, by = "column") |>
  mutate(
    STUDYID  = STUDYID,
    DOMAIN   = "VS",
    USUBJID  = paste(STUDYID, SUBJID, sep = "-"),
    VSSTRESC = as.character(VSORRES),
    VSSTRESN = suppressWarnings(as.numeric(VSORRES)),
    VSSTRESU = VSORRESU,
    VSDTC    = create_iso8601(VSDAT, .format = "d-m-y"),
    VISITNUM = as.numeric(VISITNUM)
  ) |>
  arrange(USUBJID, VISITNUM, VSTESTCD) |>
  group_by(USUBJID) |>
  mutate(VSSEQ = row_number()) |>
  ungroup() |>
  select(STUDYID, DOMAIN, USUBJID, VSSEQ, VSTESTCD, VSTEST, VSORRES,
         VSORRESU, VSSTRESC, VSSTRESN, VSSTRESU, VSDTC, VISIT, VISITNUM)

write_csv(vs, file.path(outd, "vs.csv"))

# --------------------------------------------------------------------- LB ------
# Wide -> tall Findings, with standardized units, reference bounds and a derived
# reference-range indicator (LBNRIND).
lb_raw <- read_csv(file.path(raw_dir, "lb_raw.csv"), show_col_types = FALSE)
lbmeas <- tibble::tribble(
  ~column, ~LBTESTCD, ~LBTEST,                      ~LBORRESU,  ~LBORNRLO, ~LBORNRHI,
  "WBC",   "WBC",     "Leukocytes",                 "10^9/L",   4,    11,
  "HGB",   "HGB",     "Hemoglobin",                 "g/dL",     11.5, 17.5,
  "PLAT",  "PLAT",    "Platelets",                  "10^9/L",   150,  400,
  "ALT",   "ALT",     "Alanine Aminotransferase",   "U/L",      7,    56,
  "AST",   "AST",     "Aspartate Aminotransferase", "U/L",      10,   40,
  "CREAT", "CREAT",   "Creatinine",                 "umol/L",   60,   110,
  "GLUC",  "GLUC",    "Glucose",                    "mmol/L",   3.9,  6.4
)
lb <- lb_raw |>
  pivot_longer(cols = all_of(lbmeas$column), names_to = "column",
               values_to = "LBORRES") |>
  filter(!is.na(LBORRES) & LBORRES != "") |>
  left_join(lbmeas, by = "column") |>
  mutate(
    DOMAIN = "LB", USUBJID = paste(STUDYID, SUBJID, sep = "-"),
    LBSTRESC = as.character(LBORRES),
    LBSTRESN = suppressWarnings(as.numeric(LBORRES)),
    LBSTRESU = LBORRESU,
    LBNRIND = dplyr::case_when(
      LBSTRESN < LBORNRLO ~ "LOW",
      LBSTRESN > LBORNRHI ~ "HIGH",
      TRUE ~ "NORMAL"),
    LBDTC = create_iso8601(LBDAT, .format = "d-m-y"),
    VISITNUM = as.numeric(VISITNUM)) |>
  arrange(USUBJID, VISITNUM, LBTESTCD) |>
  group_by(USUBJID) |> mutate(LBSEQ = row_number()) |> ungroup() |>
  select(STUDYID, DOMAIN, USUBJID, LBSEQ, LBTESTCD, LBTEST, LBORRES, LBORRESU,
         LBSTRESC, LBSTRESN, LBSTRESU, LBORNRLO, LBORNRHI, LBNRIND, LBDTC,
         VISIT, VISITNUM)
write_csv(lb, file.path(outd, "lb.csv"))

# --------------------------------------------------------------------- CM ------
cm_raw <- read_csv(file.path(raw_dir, "cm_raw.csv"), show_col_types = FALSE) |>
  generate_oak_id_vars(pat_var = "SUBJID", raw_src = "cm_raw")
cm <- cm_raw |>
  hardcode_no_ct(raw_var = "SUBJID", tgt_var = "DOMAIN", tgt_val = "CM", id_vars = oak_id_vars()) |>
  assign_no_ct(cm_raw, raw_var = "STUDYID", tgt_var = "STUDYID", id_vars = oak_id_vars()) |>
  assign_no_ct(cm_raw, raw_var = "CMTRT",   tgt_var = "CMTRT",   id_vars = oak_id_vars()) |>
  assign_no_ct(cm_raw, raw_var = "CMDECOD", tgt_var = "CMDECOD", id_vars = oak_id_vars()) |>
  assign_ct(cm_raw, raw_var = "CMDOSU",  tgt_var = "CMDOSU",  ct_spec = ct, ct_clst = "UNIT",  id_vars = oak_id_vars()) |>
  assign_ct(cm_raw, raw_var = "CMROUTE", tgt_var = "CMROUTE", ct_spec = ct, ct_clst = "ROUTE", id_vars = oak_id_vars()) |>
  assign_no_ct(cm_raw, raw_var = "CMINDC", tgt_var = "CMINDC", id_vars = oak_id_vars()) |>
  mutate(
    CMDOSE  = suppressWarnings(as.numeric(cm_raw$CMDOSE)),
    CMSTDTC = create_iso8601(cm_raw$CMSTDAT, .format = "d-m-y"),
    CMENDTC = create_iso8601(cm_raw$CMENDAT, .format = "d-m-y")) |>
  usubjid() |>
  arrange(USUBJID, CMTRT, CMSTDTC) |>
  derive_seq(tgt_var = "CMSEQ", rec_vars = c("USUBJID"), sort_vars = c("CMTRT", "CMSTDTC")) |>
  select(STUDYID, DOMAIN, USUBJID, CMSEQ, CMTRT, CMDECOD, CMSTDTC, CMENDTC,
         CMDOSE, CMDOSU, CMROUTE, CMINDC)
write_csv(cm, file.path(outd, "cm.csv"))

cat("sdtm.oak mapping complete: DM", nrow(dm), "AE", nrow(ae),
    "VS", nrow(vs), "LB", nrow(lb), "CM", nrow(cm), "rows\n")
