#!/usr/bin/env python3
"""
run_pipeline.py  --  End-to-end orchestrator for the VGS SDTM Delivery
Accelerator, implementing the deck's one-pipeline flow:

    Spec / CRF  ->  Mapping (sdtm.oak)  ->  Validation (P21/CORE-style)
                ->  Human sign-off (HITL + Part 11)  ->  Submission-ready

Modes
-----
default            : map -> validate -> define.xml -> report the gate, then STOP
                     at the human review step (prints the review queue). This is
                     the honest state: AI has drafted; a human must now act.
--simulate-signoff : additionally play a SCRIPTED qualified reviewer through the
                     full HITL flow (resolve errors via data query, disposition
                     every warning, apply three e-signatures) to demonstrate an
                     end-to-end release. Every simulated action is written to the
                     audit trail and clearly attributed to a demo reviewer.
--engine {auto,R,python}
                   : mapping engine. auto uses real sdtm.oak (R) if installed.

The default mode never auto-signs: HITL is a hard gate, not a convenience.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import oak_wrapper          # noqa: E402
import validate as V        # noqa: E402
import define_xml           # noqa: E402
from signoff import SignoffManager  # noqa: E402


def _rule(title):
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def regen_edc():
    subprocess.run([sys.executable, os.path.join(HERE, "generate_edc.py")],
                   check=True, cwd=ROOT)


def reset_signoff():
    """Fresh data => fresh review state (stale dispositions would be meaningless).
    In production the audit trail is append-only and never reset; this exists
    only to keep the local demo repeatable."""
    sd = os.path.join(ROOT, "outputs", "signoff")
    os.makedirs(sd, exist_ok=True)
    for f in ("review_state.json", "audit_trail.jsonl"):
        open(os.path.join(sd, f), "w").close()
    open(os.path.join(sd, "review_state.json"), "w").write(
        '{"dispositions": {}, "corrections": [], "signatures": {}}')


def map_and_validate(engine: str, core_mode: str = "off",
                     core_report: str | None = None):
    manifest = oak_wrapper.run_mapping(ROOT, engine)
    results = V.validate(ROOT, core_mode, core_report)
    return manifest, results


def print_gate(sm: SignoffManager):
    g = sm.gate_status()
    print(json.dumps(g, indent=2))
    return g


def simulate_reviewer(engine: str, core_mode: str = "off",
                      core_report: str | None = None):
    """Scripted HITL: a qualified reviewer resolves everything, then signs."""
    sm = SignoffManager(ROOT)

    # ---- 1. Resolve ERRORs by raising & resolving a data query at source ----
    _rule("HITL 1/3 · Resolve ERROR findings (data query at source)")
    # rule -> (raw dataset, [start,end] raw date columns) for date-transposition
    DATE_FIX = {"AE": ("ae_raw.csv", ["AESTDAT", "AEENDAT"]),
                "CM": ("cm_raw.csv", ["CMSTDAT", "CMENDAT"])}
    q = sm.review_queue()
    for _, r in q[q["severity"] == "ERROR"].iterrows():
        if r["rule_id"] == "VGS0050" and r["domain"] in DATE_FIX:
            subj = r["usubjid"].split("-")[-1]
            raw_ds, cols = DATE_FIX[r["domain"]]
            sm.resolve_error(
                r["issue_id"], method="corrected",
                reviewer="A. Rao", role="Data Mgmt & Solutions",
                comment=f"Query DQ-{subj}: {r['domain']} start/end dates transposed "
                        "in EDC; corrected at source and re-verified.",
                correction={"raw_dataset": raw_ds,
                            "filter": {"SUBJID": subj}, "swap": cols})
            print(f"  resolved {r['issue_id']} ({r['domain']} {r['usubjid']}) via source correction")

    # re-derive the whole pipeline from corrected source
    map_and_validate(engine, core_mode, core_report)
    sm = SignoffManager(ROOT)  # reload against fresh findings

    # ---- 2. Disposition every WARNING (MedDRA coding / clinical review) ----
    _rule("HITL 2/3 · Disposition WARNING findings")
    q = sm.review_queue()
    CODING = {"VGS0060": "MedDRA", "VGS0061": "WHODrug"}
    for _, r in q[q["severity"] == "WARNING"].iterrows():
        if r["rule_id"] in CODING:      # uncoded term -> coder assigns dictionary term
            sm.disposition_warning(
                r["issue_id"], action="coded",
                reviewer="Dr. L. Chen", role="CDISC / Biostat SME",
                comment=f"{CODING[r['rule_id']]} term assigned for '{r['value']}' by coder.")
        elif r["rule_id"] in ("VGS0070", "VGS0071"):  # implausible/critical -> query
            sm.disposition_warning(
                r["issue_id"], action="query_raised",
                reviewer="Dr. L. Chen", role="CDISC / Biostat SME",
                comment=f"Value {r['value']} flagged; site query raised pending "
                        "source verification.")
        else:
            sm.disposition_warning(
                r["issue_id"], action="confirmed_acceptable",
                reviewer="Dr. L. Chen", role="CDISC / Biostat SME",
                comment="Reviewed and confirmed acceptable.")
    print(f"  dispositioned {len(q[q['severity']=='WARNING'])} warnings")

    # ---- 3. Apply the three required e-signatures ----
    _rule("HITL 3/3 · Electronic signatures (21 CFR Part 11)")
    sm.sign("mapping_review", "Dr. L. Chen", "CDISC / Biostat SME",
            meaning="Mapping reviewed and approved against the CRF/spec.")
    sm.sign("validation_gate", "M. Okafor", "Validation / QA Engineer",
            meaning="Conformance validation reviewed; gate criteria met.")
    sm.sign("submission_release", "Dr. P. Ellsworth", "Qualified Statistician",
            meaning="Datasets approved for submission-grade release.")
    for s in ("mapping_review", "validation_gate", "submission_release"):
        v = sm.state["signatures"][s]
        print(f"  signed {s:20s} by {v['reviewer']} ({v['role']})")

    return sm


def main():
    ap = argparse.ArgumentParser(description="VGS SDTM Delivery Accelerator")
    ap.add_argument("--engine", default="auto", choices=["auto", "R", "python"])
    ap.add_argument("--simulate-signoff", action="store_true",
                    help="Play a scripted qualified reviewer through the HITL gate.")
    ap.add_argument("--keep-data", action="store_true",
                    help="Do not regenerate the raw EDC source before running.")
    ap.add_argument("--core-mode", default="off", choices=["off", "auto", "report"],
                    help="Integrate real CDISC CORE: 'auto' runs the CORE CLI if "
                         "installed; 'report' ingests an existing CORE JSON report.")
    ap.add_argument("--core-report", default=None,
                    help="Path to a CORE JSON report (used with --core-mode report).")
    args = ap.parse_args()

    _rule("STEP 0 · Generate CDISC-conforming raw EDC source (50 subjects)")
    if not args.keep_data:
        regen_edc()
        reset_signoff()
    else:
        print("  keeping existing raw EDC source")

    _rule("STEP 1-2 · Map raw -> SDTM (sdtm.oak) and validate")
    manifest, results = map_and_validate(args.engine, args.core_mode, args.core_report)
    print(f"  engine used: {manifest['engine_used']}")
    for d, info in manifest["domains"].items():
        print(f"  {d}: {info['records']} records")
    print(f"  findings: {results['severity_counts']}  by source: {results['findings_by_source']}")
    print(f"  CDISC CORE: mode={results['core']['mode']} ran={results['core']['ran']} "
          f"({results['core']['reason']})")
    print(f"  domain_accuracy: {results['domain_accuracy']}")
    print(f"  qc_parity: {results['qc_parity']}  metrics_pass: {results['metrics_pass']}")

    _rule("STEP 3 · Submission metadata (define.xml)")
    print("  wrote", os.path.relpath(define_xml.generate(ROOT), ROOT))

    if not args.simulate_signoff:
        _rule("STEP 4 · HUMAN-IN-THE-LOOP GATE (action required)")
        sm = SignoffManager(ROOT)
        sm.audit.record("system", "pipeline", "validation_run_complete",
                        "outputs/reports/validation_report.xlsx",
                        {"severity_counts": results["severity_counts"]})
        g = print_gate(sm)
        print("\n  >> Pipeline halted at the human gate. AI has drafted the SDTM;")
        print("  >> a qualified reviewer must resolve findings and sign before release.")
        print("  >> Re-run with --simulate-signoff to see a scripted reviewer complete it.")
        return 0 if not g["submission_ready"] else 0

    sm = simulate_reviewer(args.engine, args.core_mode, args.core_report)

    _rule("STEP 5 · Final gate + release dossier")
    g = print_gate(sm)
    dossier = sm.generate_dossier()
    print("  dossier:", os.path.relpath(dossier, ROOT))
    ok, msg = sm.audit.verify_chain()
    print(f"  audit chain: {msg}")
    print(f"\n  SUBMISSION-READY: {g['submission_ready']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
