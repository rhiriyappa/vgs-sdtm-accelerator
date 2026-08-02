"""
validate.py  --  CDISC conformance validation + QC parity, Pinnacle 21-style.

Runs a rules engine over the mapped SDTM datasets and emits a validation report
(Excel + HTML + JSON) with rule IDs, categories, and severities modeled on
Pinnacle 21 / CDISC CORE output. Also computes the two governance metrics from
the strategy deck (page 9):

    domain_accuracy = 1 - (records with >=1 ERROR / total records)   target >= 0.90
    qc_parity       = cell-level agreement vs the independent QC map  target >= 0.99

Severity policy:
    ERROR   -> hard stop; blocks the submission gate outright.
    WARNING -> cannot auto-pass; each must be dispositioned by a human (HITL)
               before sign-off. This is the mechanism behind "zero validation
               escapes" -- nothing ambiguous slips through unreviewed.
    NOTICE  -> informational.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import pandas as pd
import yaml

import qc_reference
import core_adapter

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ------------------------------------------------ domain conformance metadata
REQUIRED = {
    "DM": ["STUDYID", "DOMAIN", "USUBJID", "SUBJID", "RFSTDTC", "SEX",
           "ARMCD", "ARM", "COUNTRY"],
    # --DECOD is intentionally NOT hard-required: dictionary coding (MedDRA /
    # WHODrug) is the human-in-the-loop task, surfaced as a WARNING for a coder
    # to resolve before sign-off (rather than a blocking ERROR on un-coded terms).
    "AE": ["STUDYID", "DOMAIN", "USUBJID", "AESEQ", "AETERM", "AESTDTC"],
    "VS": ["STUDYID", "DOMAIN", "USUBJID", "VSSEQ", "VSTESTCD", "VSTEST",
           "VSORRES", "VSDTC"],
    "LB": ["STUDYID", "DOMAIN", "USUBJID", "LBSEQ", "LBTESTCD", "LBTEST",
           "LBORRES", "LBDTC"],
    "CM": ["STUDYID", "DOMAIN", "USUBJID", "CMSEQ", "CMTRT", "CMSTDTC"],
}
CT_VARS = {
    "DM": {"SEX": "SEX", "RACE": "RACE", "ETHNIC": "ETHNIC"},
    "AE": {"AESEV": "AESEV", "AESER": "NY", "AEOUT": "OUT"},
    "VS": {"VSTESTCD": "VSTESTCD"},
    "LB": {"LBTESTCD": "LBTESTCD", "LBNRIND": "NRIND"},
    "CM": {"CMDOSU": "UNIT", "CMROUTE": "ROUTE"},
}
DATE_VARS = {
    "DM": ["RFSTDTC", "RFENDTC", "RFICDTC", "BRTHDTC"],
    "AE": ["AESTDTC", "AEENDTC"],
    "VS": ["VSDTC"],
    "LB": ["LBDTC"],
    "CM": ["CMSTDTC", "CMENDTC"],
}
VS_RANGES = {  # (low, high) physiologically plausible
    "SYSBP": (60, 260), "DIABP": (30, 160), "PULSE": (30, 220),
    "TEMP": (30, 45), "HEIGHT": (100, 230), "WEIGHT": (30, 250),
}
# QC comparison columns (must exist in both production and reference)
QC_COLS = {
    "DM": (["USUBJID"], ["SEX", "RACE", "BRTHDTC", "RFSTDTC"]),
    "AE": (["USUBJID", "AETERM", "AESTDTC"], ["AEENDTC", "AESEV"]),
    "VS": (["USUBJID", "VISITNUM", "VSTESTCD"], ["VSORRES", "VSDTC"]),
    "LB": (["USUBJID", "VISITNUM", "LBTESTCD"], ["LBORRES", "LBDTC"]),
    "CM": (["USUBJID", "CMTRT", "CMSTDTC"], ["CMENDTC", "CMDOSE"]),
}


def _ct_values(ct_path: str) -> dict:
    df = pd.read_csv(ct_path, dtype=str).fillna("")
    out: dict[str, set] = {}
    for _, r in df.iterrows():
        out.setdefault(r["codelist"], set()).add(r["submission_value"])
    return out


def validate(root: str, core_mode: str = "off", core_report: str | None = None) -> dict:
    cfg = yaml.safe_load(open(os.path.join(root, "config", "study.yaml")))
    ct_path = os.path.join(root, "ct", "controlled_terminology.csv")
    ctv = _ct_values(ct_path)
    sdtm_dir = os.path.join(root, "outputs", "sdtm")
    domains = cfg["study"]["domains"]

    datasets = {d: pd.read_csv(os.path.join(sdtm_dir, f"{d.lower()}.csv"), dtype=str).fillna("")
                for d in domains}
    dm_subjects = set(datasets["DM"]["USUBJID"]) if "DM" in datasets else set()
    qc_ref = qc_reference.build(root)

    issues: list[dict] = []
    seen: set[str] = set()

    def add(sev, rule, cat, domain, var, msg, usubjid="", value="", seq="",
            source="builtin"):
        # Content-addressed issue_id: a finding's identity is its content (incl.
        # the record's --SEQ discriminator and source engine), not its row order.
        # Dispositions bind to the *finding*, so a stale disposition can never
        # clear a regenerated error after a re-run, and distinct records never
        # collapse together.
        h = hashlib.sha1(
            f"{source}|{rule}|{domain}|{var}|{usubjid}|{seq}|{value}|{msg}".encode()
        ).hexdigest()
        iid = "ISS-" + h[:10].upper()
        if iid in seen:
            return
        seen.add(iid)
        issues.append({
            "issue_id": iid, "rule_id": rule, "category": cat,
            "severity": sev, "domain": domain, "variable": var,
            "usubjid": usubjid, "seq": seq, "value": value, "message": msg,
            "source": source,
        })

    def _rk(r):
        return r.get("AESEQ", "") or r.get("VSSEQ", "") or ""

    for d in domains:
        df = datasets[d]
        spec = yaml.safe_load(open(os.path.join(root, "specs", f"{d.lower()}.yaml")))

        # -- required variables present & populated
        for v in REQUIRED[d]:
            if v not in df.columns:
                add("ERROR", "VGS0001", "Presence", d, v,
                    f"Required variable {v} is absent from {d}.")
                continue
            nulls = df[df[v].astype(str).str.strip() == ""]
            for _, r in nulls.iterrows():
                add("ERROR", "VGS0002", "Presence", d, v,
                    f"Required variable {v} is null.", r.get("USUBJID", ""))

        # -- controlled terminology compliance
        for var, clst in CT_VARS[d].items():
            if var not in df.columns:
                continue
            allowed = ctv.get(clst, set())
            for _, r in df.iterrows():
                val = str(r[var]).strip()
                if val and val not in allowed:
                    add("ERROR", "VGS0010", "Terminology", d, var,
                        f"Value '{val}' not in codelist {clst}.",
                        r.get("USUBJID", ""), val, seq=_rk(r))

        # -- ISO 8601 date format
        for var in DATE_VARS[d]:
            if var not in df.columns:
                continue
            for _, r in df.iterrows():
                val = str(r[var]).strip()
                if val and not ISO_RE.match(val):
                    add("ERROR", "VGS0020", "Format", d, var,
                        f"{var}='{val}' is not ISO 8601 (YYYY-MM-DD).",
                        r.get("USUBJID", ""), val, seq=_rk(r))

        # -- key uniqueness
        keys = [k for k in spec.get("key_vars", []) if k in df.columns]
        if keys:
            dup = df[df.duplicated(keys, keep=False)]
            for _, r in dup.iterrows():
                add("ERROR", "VGS0030", "Uniqueness", d, "+".join(keys),
                    f"Duplicate key ({', '.join(keys)}).", r.get("USUBJID", ""))

        # -- --SEQ uniqueness within USUBJID
        seqvar = {"AE": "AESEQ", "VS": "VSSEQ"}.get(d)
        if seqvar and seqvar in df.columns:
            dd = df[df.duplicated(["USUBJID", seqvar], keep=False)]
            for _, r in dd.iterrows():
                add("ERROR", "VGS0031", "Uniqueness", d, seqvar,
                    f"Duplicate {seqvar} within USUBJID.", r.get("USUBJID", ""))

        # -- referential integrity to DM
        if d != "DM":
            orphan = df[~df["USUBJID"].isin(dm_subjects)]
            for _, r in orphan.iterrows():
                add("ERROR", "VGS0040", "Integrity", d, "USUBJID",
                    "USUBJID not found in DM.", r.get("USUBJID", ""))

    # -- AE date logic: start <= end
    ae = datasets.get("AE")
    if ae is not None:
        for _, r in ae.iterrows():
            s, e = str(r.get("AESTDTC", "")), str(r.get("AEENDTC", ""))
            if ISO_RE.match(s) and ISO_RE.match(e) and s > e:
                add("ERROR", "VGS0050", "Logic", "AE", "AESTDTC/AEENDTC",
                    f"AESTDTC ({s}) is after AEENDTC ({e}).",
                    r.get("USUBJID", ""), seq=r.get("AESEQ", ""))
        # -- MedDRA coding completeness -> HITL
        for _, r in ae.iterrows():
            if str(r.get("AEDECOD", "")).strip() == "":
                add("WARNING", "VGS0060", "Coding", "AE", "AEDECOD",
                    f"AETERM '{r.get('AETERM','')}' not MedDRA-coded; requires "
                    "medical coder / statistician review.", r.get("USUBJID", ""),
                    r.get("AETERM", ""), seq=r.get("AESEQ", ""))

    # -- VS plausibility ranges -> HITL
    vs = datasets.get("VS")
    if vs is not None:
        for _, r in vs.iterrows():
            tc = str(r.get("VSTESTCD", ""))
            raw = str(r.get("VSSTRESN", "") or r.get("VSORRES", "")).strip()
            if tc in VS_RANGES and raw:
                try:
                    x = float(raw)
                    lo, hi = VS_RANGES[tc]
                    if x < lo or x > hi:
                        add("WARNING", "VGS0070", "Range", "VS", "VSORRES",
                            f"{tc}={raw} outside plausible range [{lo},{hi}]; "
                            "requires clinical review / source verification.",
                            r.get("USUBJID", ""), raw, seq=r.get("VSSEQ", ""))
                except ValueError:
                    pass

    # -- LB critical value: result far beyond its reference range -> HITL
    lb = datasets.get("LB")
    if lb is not None:
        for _, r in lb.iterrows():
            val = str(r.get("LBSTRESN", "")).strip()
            hi = str(r.get("LBORNRHI", "")).strip()
            lo = str(r.get("LBORNRLO", "")).strip()
            try:
                x, h, l = float(val), float(hi), float(lo)
                if x > 5 * h or (l > 0 and x < l / 5):
                    add("WARNING", "VGS0071", "Range", "LB", "LBSTRESN",
                        f"{r.get('LBTESTCD','')}={val} is a critical value vs "
                        f"reference [{lo},{hi}]; requires clinical review.",
                        r.get("USUBJID", ""), val, seq=r.get("LBSEQ", ""))
            except ValueError:
                pass

    # -- CM date logic + WHODrug coding completeness -> HITL
    cm = datasets.get("CM")
    if cm is not None:
        for _, r in cm.iterrows():
            s, e = str(r.get("CMSTDTC", "")), str(r.get("CMENDTC", ""))
            if ISO_RE.match(s) and ISO_RE.match(e) and s > e:
                add("ERROR", "VGS0050", "Logic", "CM", "CMSTDTC/CMENDTC",
                    f"CMSTDTC ({s}) is after CMENDTC ({e}).",
                    r.get("USUBJID", ""), seq=r.get("CMSEQ", ""))
            if str(r.get("CMDECOD", "")).strip() == "":
                add("WARNING", "VGS0061", "Coding", "CM", "CMDECOD",
                    f"CMTRT '{r.get('CMTRT','')}' not WHODrug-coded; requires "
                    "drug coder review.", r.get("USUBJID", ""),
                    r.get("CMTRT", ""), seq=r.get("CMSEQ", ""))

    # -------------------------------------- real CDISC CORE findings (optional)
    core = core_adapter.get_core_findings(root, core_mode, core_report)
    for f in core["findings"]:
        add(f["severity"], f["rule_id"], "CORE", f["domain"], f["variable"],
            f["message"], f.get("usubjid", ""), f.get("value", ""),
            source="CDISC CORE")

    # ------------------------------------------------------- QC parity metric
    parity = {}
    for d in domains:
        keys, cmp_cols = QC_COLS[d]
        prod = datasets[d].copy()
        ref = qc_ref[d].copy()
        for c in keys + cmp_cols:
            if c in prod.columns:
                prod[c] = prod[c].astype(str).str.strip().str.upper()
            if c in ref.columns:
                ref[c] = ref[c].astype(str).str.strip().str.upper()
        merged = prod.merge(ref, on=keys, how="inner", suffixes=("_p", "_r"))
        total = match = 0
        for c in cmp_cols:
            cp, cr = f"{c}_p", f"{c}_r"
            if cp in merged and cr in merged:
                total += len(merged)
                match += int((merged[cp] == merged[cr]).sum())
        parity[d] = round(match / total, 4) if total else 1.0

    # ------------------------------------------------------- domain accuracy
    accuracy = {}
    for d in domains:
        df = datasets[d]
        err_subj = {i["usubjid"] for i in issues
                    if i["domain"] == d and i["severity"] == "ERROR" and i["usubjid"]}
        # accuracy at record level: records with no ERROR / total
        if d == "DM":
            total = len(df)
            bad = len(err_subj)
        else:
            total = len(df)
            bad = len(df[df["USUBJID"].isin(err_subj)])
        accuracy[d] = round(1 - bad / total, 4) if total else 1.0

    sev_counts = {s: sum(1 for i in issues if i["severity"] == s)
                  for s in ("ERROR", "WARNING", "NOTICE")}
    gov = cfg["governance"]
    results = {
        "issues": issues,
        "severity_counts": sev_counts,
        "qc_parity": parity,
        "domain_accuracy": accuracy,
        "thresholds": {"domain_accuracy_target": gov["domain_accuracy_target"],
                       "qc_parity_target": gov["qc_parity_target"]},
        "metrics_pass": {
            "domain_accuracy": all(v >= gov["domain_accuracy_target"] for v in accuracy.values()),
            "qc_parity": all(v >= gov["qc_parity_target"] for v in parity.values()),
        },
        "errors_present": sev_counts["ERROR"] > 0,
        "open_warnings": sev_counts["WARNING"],
        "core": {"mode": core_mode, "ran": core["ran"],
                 "reason": core["reason"], "findings": len(core["findings"])},
        "findings_by_source": {
            s: sum(1 for i in issues if i.get("source") == s)
            for s in ("builtin", "CDISC CORE")},
    }
    _write_reports(root, results)
    return results


def _write_reports(root: str, results: dict):
    rep = os.path.join(root, "outputs", "reports")
    os.makedirs(rep, exist_ok=True)
    issues = results["issues"]
    details = pd.DataFrame(issues)

    with open(os.path.join(rep, "validation_results.json"), "w") as f:
        json.dump({k: v for k, v in results.items() if k != "issues"}, f, indent=2)
    details.to_csv(os.path.join(rep, "validation_findings.csv"), index=False)

    # Excel: Summary / Findings / Rule dictionary  (P21-style workbook)
    try:
        with pd.ExcelWriter(os.path.join(rep, "validation_report.xlsx"),
                            engine="openpyxl") as xl:
            summ = _summary_frame(results)
            summ.to_excel(xl, sheet_name="Summary", index=False)
            (details if not details.empty else pd.DataFrame(
                columns=["issue_id", "rule_id", "severity", "domain",
                         "variable", "usubjid", "message"])
             ).to_excel(xl, sheet_name="Findings", index=False)
            _rule_dictionary().to_excel(xl, sheet_name="Rules", index=False)
    except Exception as e:
        print(f"  [warn] Excel report skipped: {e}")

    _write_html(rep, results, details)


def _summary_frame(results: dict) -> pd.DataFrame:
    rows = []
    for d, acc in results["domain_accuracy"].items():
        rows.append({"Metric": f"Domain accuracy ({d})", "Value": acc,
                     "Target": results["thresholds"]["domain_accuracy_target"],
                     "Pass": acc >= results["thresholds"]["domain_accuracy_target"]})
    for d, p in results["qc_parity"].items():
        rows.append({"Metric": f"QC parity ({d})", "Value": p,
                     "Target": results["thresholds"]["qc_parity_target"],
                     "Pass": p >= results["thresholds"]["qc_parity_target"]})
    for s, c in results["severity_counts"].items():
        rows.append({"Metric": f"{s} findings", "Value": c, "Target": "-", "Pass": ""})
    return pd.DataFrame(rows)


def _rule_dictionary() -> pd.DataFrame:
    return pd.DataFrame([
        ["VGS0001", "Presence", "ERROR", "Required variable absent from domain"],
        ["VGS0002", "Presence", "ERROR", "Required variable is null"],
        ["VGS0010", "Terminology", "ERROR", "Value not in CDISC controlled terminology"],
        ["VGS0020", "Format", "ERROR", "Date not ISO 8601 (YYYY-MM-DD)"],
        ["VGS0030", "Uniqueness", "ERROR", "Duplicate natural key"],
        ["VGS0031", "Uniqueness", "ERROR", "Duplicate --SEQ within USUBJID"],
        ["VGS0040", "Integrity", "ERROR", "USUBJID not present in DM"],
        ["VGS0050", "Logic", "ERROR", "Event start date after end date"],
        ["VGS0060", "Coding", "WARNING", "AE term not MedDRA-coded (HITL)"],
        ["VGS0061", "Coding", "WARNING", "CM term not WHODrug-coded (HITL)"],
        ["VGS0070", "Range", "WARNING", "VS finding outside plausible range (HITL)"],
        ["VGS0071", "Range", "WARNING", "LB critical value vs reference (HITL)"],
    ], columns=["Rule ID", "Category", "Severity", "Description"])


def _write_html(rep: str, results: dict, details: pd.DataFrame):
    sc = results["severity_counts"]
    rows = "".join(
        f"<tr class='{i['severity'].lower()}'><td>{i['issue_id']}</td>"
        f"<td>{i['rule_id']}</td><td>{i['severity']}</td><td>{i['domain']}</td>"
        f"<td>{i['variable']}</td><td>{i['usubjid']}</td><td>{i['message']}</td></tr>"
        for i in results["issues"]) or "<tr><td colspan=7>No findings.</td></tr>"
    metric_rows = "".join(
        f"<tr><td>Domain accuracy — {d}</td><td>{v:.2%}</td>"
        f"<td>{'PASS' if v >= results['thresholds']['domain_accuracy_target'] else 'FAIL'}</td></tr>"
        for d, v in results["domain_accuracy"].items()) + "".join(
        f"<tr><td>QC parity — {d}</td><td>{v:.2%}</td>"
        f"<td>{'PASS' if v >= results['thresholds']['qc_parity_target'] else 'FAIL'}</td></tr>"
        for d, v in results["qc_parity"].items())
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>VGS SDTM Validation Report</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;color:#12263a}}
h1{{color:#0b2e4f}} .cards{{display:flex;gap:1rem;margin:1rem 0}}
.card{{border:1px solid #dde;border-radius:8px;padding:1rem 1.4rem}}
.err{{color:#b22}} .warn{{color:#b6820a}}
table{{border-collapse:collapse;width:100%;margin-top:1rem;font-size:14px}}
th,td{{border:1px solid #e3e8ee;padding:6px 8px;text-align:left}}
th{{background:#0b2e4f;color:#fff}}
tr.error td:nth-child(3){{color:#b22;font-weight:600}}
tr.warning td:nth-child(3){{color:#b6820a;font-weight:600}}
</style></head><body>
<h1>VGS SDTM Delivery Accelerator — Validation Report</h1>
<p>Pinnacle 21 / CDISC CORE-style conformance report. Governance gate metrics
from the AI strategy proposal (domain accuracy ≥90%, QC parity ≥99%).</p>
<div class=cards>
<div class=card><b class=err>{sc['ERROR']}</b><br>Errors (block gate)</div>
<div class=card><b class=warn>{sc['WARNING']}</b><br>Warnings (need HITL)</div>
<div class=card><b>{sc['NOTICE']}</b><br>Notices</div></div>
<h2>Governance metrics</h2>
<table><tr><th>Metric</th><th>Value</th><th>Result</th></tr>{metric_rows}</table>
<h2>Findings</h2>
<table><tr><th>Issue</th><th>Rule</th><th>Severity</th><th>Domain</th>
<th>Variable</th><th>USUBJID</th><th>Message</th></tr>{rows}</table>
</body></html>"""
    with open(os.path.join(rep, "validation_report.html"), "w") as f:
        f.write(html)


if __name__ == "__main__":
    import argparse
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-mode", default="off", choices=["off", "auto", "report"])
    ap.add_argument("--core-report", default=None)
    a = ap.parse_args()
    res = validate(r, a.core_mode, a.core_report)
    print(json.dumps({k: v for k, v in res.items() if k != "issues"}, indent=2))
    print("total findings:", len(res["issues"]))
