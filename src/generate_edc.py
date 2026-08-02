"""
generate_edc.py  --  Synthetic but CDISC-conforming raw EDC source data.

Produces three raw EDC extracts for a fictional study VGS-DEMO-101:
  data/raw_edc/dm_raw.csv   50 subjects (one row each)  -> maps to SDTM DM
  data/raw_edc/ae_raw.csv   adverse events (many rows)  -> maps to SDTM AE
  data/raw_edc/vs_raw.csv   vitals, WIDE format         -> maps to SDTM VS (tall)

The raw layout mimics a typical Rave/Veeva EDC export: raw variable names,
DD-MON-YYYY dates, free-text-ish coded fields, wide vitals. A handful of
deliberate data-quality issues are seeded so the validation + human-in-the-loop
review steps have real findings to surface (documented in KNOWN_ISSUES below).

Reproducible: fixed random seed.
"""
import csv
import os
import random
from datetime import date, timedelta

random.seed(20260714)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(HERE, "data", "raw_edc")
os.makedirs(RAW, exist_ok=True)

N_SUBJECTS = 50
STUDYID = "VGS-DEMO-101"
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# ---- KNOWN_ISSUES (seeded on purpose; the pipeline should catch these) --------
# 1. Subject 0007 has SEX = "U" (raw) -> not in CT {M,F} -> validation flag,
#    routed to human review.
# 2. Subject 0021 AE row has AESTDAT after AEENDAT (illogical date) -> flag.
# 3. Subject 0033 VS has a SYSBP of 400 (physiologically implausible) -> flag.
# 4. Subject 0044 has a blank RACE in raw -> maps to CT "UNKNOWN" but flagged
#    for reviewer confirmation.
# -----------------------------------------------------------------------------


def dmon(d: date) -> str:
    return f"{d.day:02d}-{MON[d.month - 1]}-{d.year}"


def w(path, header, rows):
    with open(path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        wr.writerows(rows)
    print(f"  wrote {len(rows):>4} rows -> {os.path.relpath(path, HERE)}")


# ---------------------------------------------------------------- DM -----------
SEX_RAW = ["Male", "Female"]
RACE_RAW = ["White", "Black or African American", "Asian",
            "American Indian or Alaska Native",
            "Native Hawaiian or Other Pacific Islander"]
ETHNIC_RAW = ["Hispanic or Latino", "Not Hispanic or Latino"]
COUNTRY = ["USA", "GBR", "DEU", "IND", "JPN"]
ARMS = [("A", "Placebo"), ("B", "Drug 10 mg"), ("C", "Drug 20 mg")]

dm_rows = []
subj_meta = {}  # subjid -> dict for cross-file reference
study_start = date(2025, 1, 6)
for i in range(1, N_SUBJECTS + 1):
    subjid = f"{i:04d}"
    site = f"{100 + (i % 5):03d}"
    armcd, arm = ARMS[i % 3]
    sex = "U" if subjid == "0007" else random.choice(SEX_RAW)
    race = "" if subjid == "0044" else random.choice(RACE_RAW)
    ethnic = random.choice(ETHNIC_RAW)
    country = random.choice(COUNTRY)
    age = random.randint(19, 78)
    brth_year = 2025 - age
    brth = date(brth_year, random.randint(1, 12), random.randint(1, 28))
    ic = study_start + timedelta(days=random.randint(0, 40))
    first_dose = ic + timedelta(days=random.randint(1, 7))
    last_visit = first_dose + timedelta(days=random.randint(60, 120))
    dm_rows.append([
        STUDYID, subjid, site, dmon(ic), dmon(brth), age, "YEARS",
        sex, race, ethnic, armcd, arm, country,
        dmon(first_dose), dmon(last_visit),
    ])
    subj_meta[subjid] = {"first_dose": first_dose, "last_visit": last_visit}

w(os.path.join(RAW, "dm_raw.csv"),
  ["STUDYID", "SUBJID", "SITEID", "RFICDAT", "BRTHDAT", "AGE", "AGEU",
   "SEX", "RACE", "ETHNIC", "ARMCD", "ARM", "COUNTRY",
   "FRSTDSDAT", "LSTVISDAT"],
  dm_rows)

# ---------------------------------------------------------------- AE -----------
AE_TERMS = [
    ("Headache", "Headache", "10019211"),
    ("Nausea", "Nausea", "10028813"),
    ("Fatigue", "Fatigue", "10016256"),
    ("Dizziness", "Dizziness", "10013573"),
    ("Diarrhoea", "Diarrhoea", "10012735"),
    ("Vomiting", "Vomiting", "10047700"),
    ("Insomnia", "Insomnia", "10022437"),
    ("Rash", "Rash", "10037844"),
]
SEV_RAW = ["Mild", "Moderate", "Severe"]
OUT_RAW = ["Recovered/Resolved", "Recovering/Resolving", "Not Recovered/Not Resolved"]
ae_rows = []
ae_seq = {}
for subjid, meta in subj_meta.items():
    n_ae = random.choices([0, 1, 2, 3, 4], weights=[15, 30, 30, 15, 10])[0]
    for _ in range(n_ae):
        term, decod, ptcode = random.choice(AE_TERMS)
        start = meta["first_dose"] + timedelta(days=random.randint(1, 90))
        dur = random.randint(1, 20)
        end = start + timedelta(days=dur)
        # Seeded illogical date for subject 0021
        if subjid == "0021":
            start, end = end, start
        ser = "Y" if random.random() < 0.08 else "N"
        # decod deliberately left blank ~15% of the time -> needs MedDRA coding/HITL
        decod_val = decod if random.random() > 0.15 else ""
        ptcode_val = ptcode if decod_val else ""
        ae_rows.append([
            STUDYID, subjid, term, decod_val, ptcode_val,
            dmon(start), dmon(end),
            random.choice(SEV_RAW), ser,
            random.choice(["Related", "Not Related", "Possibly Related"]),
            random.choice(OUT_RAW),
        ])

w(os.path.join(RAW, "ae_raw.csv"),
  ["STUDYID", "SUBJID", "AETERM", "AEDECOD", "AEPTCD",
   "AESTDAT", "AEENDAT", "AESEV", "AESER", "AEREL", "AEOUT"],
  ae_rows)

# ---------------------------------------------------------------- VS (wide) ----
VISITS = [("SCREENING", 1), ("BASELINE", 2), ("WEEK 2", 3),
          ("WEEK 4", 4), ("WEEK 8", 5)]
vs_rows = []
for subjid, meta in subj_meta.items():
    for visit, vnum in VISITS:
        vdate = meta["first_dose"] + timedelta(days=(vnum - 2) * 14)
        sysbp = random.randint(105, 145)
        diabp = random.randint(65, 95)
        pulse = random.randint(55, 95)
        temp = round(random.uniform(36.2, 37.6), 1)
        height = round(random.uniform(150, 195), 1) if visit == "SCREENING" else ""
        weight = round(random.uniform(52, 105), 1)
        # Seeded implausible SBP for subject 0033 at WEEK 4
        if subjid == "0033" and visit == "WEEK 4":
            sysbp = 400
        vs_rows.append([
            STUDYID, subjid, visit, vnum, dmon(vdate),
            sysbp, diabp, pulse, temp, height, weight,
        ])

w(os.path.join(RAW, "vs_raw.csv"),
  ["STUDYID", "SUBJID", "VISIT", "VISITNUM", "VSDAT",
   "SYSBP", "DIABP", "PULSE", "TEMP", "HEIGHT", "WEIGHT"],
  vs_rows)

# ---------------------------------------------------------------- LB (wide) ----
# Labs collected wide (one column per test). Maps to SDTM LB (Findings), tall.
# LB_TESTS: raw column -> (low, high, decimals) plausible sampling range.
LB_TESTS = [
    ("WBC", 4.0, 11.0, 1), ("HGB", 11.5, 17.5, 1), ("PLAT", 150, 400, 0),
    ("ALT", 7, 56, 0), ("AST", 10, 40, 0), ("CREAT", 60, 110, 0),
    ("GLUC", 3.9, 6.4, 1),
]
LB_VISITS = [("SCREENING", 1), ("WEEK 4", 4), ("WEEK 8", 5)]
lb_rows = []
for subjid, meta in subj_meta.items():
    for visit, vnum in LB_VISITS:
        vdate = meta["first_dose"] + timedelta(days=(vnum - 2) * 14)
        vals = []
        for tc, lo, hi, dec in LB_TESTS:
            v = round(random.uniform(lo * 0.9, hi * 1.1), dec)
            v = int(v) if dec == 0 else v
            # Seeded implausible ALT for subject 0015 at WEEK 4
            if subjid == "0015" and tc == "ALT" and visit == "WEEK 4":
                v = 999
            vals.append(v)
        lb_rows.append([STUDYID, subjid, visit, vnum, dmon(vdate)] + vals)

w(os.path.join(RAW, "lb_raw.csv"),
  ["STUDYID", "SUBJID", "VISIT", "VISITNUM", "LBDAT"] + [t[0] for t in LB_TESTS],
  lb_rows)

# ---------------------------------------------------------------- CM (tall) ----
# Concomitant meds, one row per med per subject. Maps to SDTM CM (Interventions).
CM_MEDS = [
    ("Paracetamol", "PARACETAMOL", 500), ("Ibuprofen", "IBUPROFEN", 400),
    ("Amoxicillin", "AMOXICILLIN", 250), ("Metformin", "METFORMIN", 500),
    ("Aspirin", "ACETYLSALICYLIC ACID", 100),
]
CM_INDIC = ["Headache", "Hypertension", "Infection", "Diabetes", "Pain"]
cm_rows = []
for subjid, meta in subj_meta.items():
    n_cm = random.choices([0, 1, 2, 3], weights=[20, 35, 30, 15])[0]
    for _ in range(n_cm):
        trt, decod, dose = random.choice(CM_MEDS)
        start = meta["first_dose"] - timedelta(days=random.randint(0, 30))
        ongoing = random.random() < 0.3
        end = "" if ongoing else dmon(start + timedelta(days=random.randint(3, 40)))
        # WHODrug decode blank ~15% of the time -> needs coding / HITL
        decod_val = decod if random.random() > 0.15 else ""
        cm_rows.append([
            STUDYID, subjid, trt, decod_val, dmon(start), end,
            dose, "mg", "ORAL", random.choice(CM_INDIC),
        ])

w(os.path.join(RAW, "cm_raw.csv"),
  ["STUDYID", "SUBJID", "CMTRT", "CMDECOD", "CMSTDAT", "CMENDAT",
   "CMDOSE", "CMDOSU", "CMROUTE", "CMINDC"],
  cm_rows)

print("EDC generation complete:", N_SUBJECTS, "subjects.")
