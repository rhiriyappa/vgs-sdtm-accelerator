"""
test_pipeline.py  --  End-to-end verification of the SDTM accelerator.

Runs standalone (`python3 tests/test_pipeline.py`) or under pytest. Covers the
happy path AND the safety-critical negative paths that back the deck's "zero
validation escapes" claim:

  * mapping shape + ISO-8601 conversion
  * validation catches every seeded data-quality defect
  * QC parity / domain-accuracy meet governance thresholds
  * the gate BLOCKS before human sign-off
  * an unauthorized role CANNOT sign
  * tampering with the audit trail is detected
  * only after correct HITL resolution + signatures is it submission-ready
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import oak_wrapper
import validate as V
import core_adapter
from signoff import SignoffManager
from audit import AuditTrail

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_core_report.json")
CHECKS = []


def check(name, cond):
    CHECKS.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name)


def _fresh_pipeline():
    import subprocess
    subprocess.run([sys.executable, os.path.join(ROOT, "src", "generate_edc.py")],
                   check=True, cwd=ROOT)
    for f in ("review_state.json", "audit_trail.jsonl"):
        p = os.path.join(ROOT, "outputs", "signoff", f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    open(os.path.join(ROOT, "outputs", "signoff", "review_state.json"), "w").write(
        '{"dispositions": {}, "corrections": [], "signatures": {}}')
    manifest = oak_wrapper.run_mapping(ROOT, "python")
    results = V.validate(ROOT)
    return manifest, results


def test_mapping_and_validation():
    manifest, results = _fresh_pipeline()
    check("DM has 50 records", manifest["domains"]["DM"]["records"] == 50)
    check("all 5 domains mapped", set(manifest["domains"]) == {"DM", "AE", "VS", "LB", "CM"})
    check("VS pivoted wide->tall (>1000 rows)",
          manifest["domains"]["VS"]["records"] > 1000)
    import pandas as pd
    dm = pd.read_csv(os.path.join(ROOT, "outputs", "sdtm", "dm.csv"), dtype=str)
    check("DM dates are ISO-8601", bool(dm["RFSTDTC"].str.match(r"\d{4}-\d{2}-\d{2}").all()))
    check("SEX mapped to CT (M/F/U only)", set(dm["SEX"]).issubset({"M", "F", "U"}))
    lb = pd.read_csv(os.path.join(ROOT, "outputs", "sdtm", "lb.csv"), dtype=str)
    check("LBNRIND derived (NORMAL/HIGH/LOW)",
          set(lb["LBNRIND"].unique()).issubset({"NORMAL", "HIGH", "LOW", ""})
          and "HIGH" in set(lb["LBNRIND"]))
    check("2 seeded date-logic errors caught",
          results["severity_counts"]["ERROR"] == 2)
    check("uncoded-AE + range warnings caught",
          results["severity_counts"]["WARNING"] >= 2)
    check("QC parity >= 0.99 all domains",
          all(v >= 0.99 for v in results["qc_parity"].values()))
    check("domain accuracy >= 0.90 all domains",
          all(v >= 0.90 for v in results["domain_accuracy"].values()))
    return results


def test_core_adapter():
    det = core_adapter.detect_core()
    check("CORE detects unavailability cleanly",
          det["available"] is False and "reason" in det)
    got = core_adapter.get_core_findings(ROOT, "report", FIXTURE)
    check("CORE report normalized (3 findings)", len(got["findings"]) == 3)
    sevs = {f["severity"] for f in got["findings"]}
    check("CORE findings carry severities", "ERROR" in sevs and "WARNING" in sevs)
    # integrated: CORE findings merge into the unified report + gate
    res = V.validate(ROOT, "report", FIXTURE)
    check("CORE findings merged by source",
          res["findings_by_source"]["CDISC CORE"] == 3)
    check("CORE off by default", V.validate(ROOT)["findings_by_source"]["CDISC CORE"] == 0)


def test_gate_blocks_before_signoff():
    sm = SignoffManager(ROOT)
    g = sm.gate_status()
    check("gate BLOCKED before sign-off", g["submission_ready"] is False)
    check("gate reports missing signatures", len(g["missing_signatures"]) == 3)


def test_unauthorized_signer_rejected():
    sm = SignoffManager(ROOT)
    try:
        sm.sign("submission_release", "J. Intern", "AI/ML Engineer",
                meaning="attempting release")
        check("unauthorized signer rejected", False)
    except PermissionError:
        check("unauthorized signer rejected", True)


def test_audit_tamper_detected():
    p = os.path.join(ROOT, "outputs", "signoff", "tamper_test.jsonl")
    if os.path.exists(p):
        open(p, "w").close()
    at = AuditTrail(p)
    at.record("A. Rao", "DM", "action1", "t1", {"x": 1})
    at.record("A. Rao", "DM", "action2", "t2", {"x": 2})
    ok, _ = at.verify_chain()
    check("clean chain verifies", ok)
    # tamper: rewrite a past entry's details
    lines = open(p).read().splitlines()
    lines[0] = lines[0].replace('"x": 1', '"x": 999')
    open(p, "w").write("\n".join(lines) + "\n")
    ok2, _ = AuditTrail(p).verify_chain()
    check("tampering detected", ok2 is False)


def test_full_hitl_release():
    sm = SignoffManager(ROOT)
    # resolve the 2 seeded errors at source, then re-derive
    q = sm.review_queue()
    for _, r in q[q["severity"] == "ERROR"].iterrows():
        subj = r["usubjid"].split("-")[-1]
        sm.resolve_error(r["issue_id"], "corrected", "A. Rao",
                         "Data Mgmt & Solutions", "source date query resolved",
                         correction={"raw_dataset": "ae_raw.csv",
                                     "filter": {"SUBJID": subj},
                                     "swap": ["AESTDAT", "AEENDAT"]})
    oak_wrapper.run_mapping(ROOT, "python")
    V.validate(ROOT)
    sm = SignoffManager(ROOT)
    for _, r in sm.review_queue().iterrows():
        if r["severity"] == "WARNING":
            sm.disposition_warning(r["issue_id"], "coded", "Dr. L. Chen",
                                   "CDISC / Biostat SME", "resolved")
    sm.sign("mapping_review", "Dr. L. Chen", "CDISC / Biostat SME", "approved")
    sm.sign("validation_gate", "M. Okafor", "Validation / QA Engineer", "approved")
    sm.sign("submission_release", "Dr. P. Ellsworth", "Qualified Statistician", "approved")
    g = sm.gate_status()
    check("no open errors after correction", len(g["open_errors"]) == 0)
    check("all warnings dispositioned", len(g["open_warnings"]) == 0)
    check("audit chain intact", g["audit_chain_ok"])
    check("SUBMISSION-READY after full HITL", g["submission_ready"] is True)


def main():
    print("Running SDTM accelerator verification...\n")
    test_mapping_and_validation()
    test_core_adapter()
    test_gate_blocks_before_signoff()
    test_unauthorized_signer_rejected()
    test_audit_tamper_detected()
    test_full_hitl_release()
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"\n{passed}/{len(CHECKS)} checks passed.")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
