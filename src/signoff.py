"""
signoff.py  --  Human-in-the-loop review, disposition, and e-signature gate.

Implements the deck's non-negotiables (page 6): "AI drafts; a qualified
statistician reviews and signs", "Validation gate before any client use", and a
"full audit trail". Nothing reaches submission-ready without a human.

Gate has three independently-audited conditions:
    C1  every ERROR finding is RESOLVED (corrected at source + re-verified,
        or justified false-positive) -- errors can never be waived.
    C2  every WARNING finding is DISPOSITIONED by a human (coded / confirmed /
        queried / false-positive) -- this is what delivers "zero escapes".
    C3  required e-signatures are present, from roles authorized in study.yaml,
        each bound to the SHA-256 fingerprint of the exact dataset signed.

All state lives in outputs/signoff/review_state.json; every action is written to
the tamper-evident audit trail (audit.py).
"""
from __future__ import annotations
import json
import os
import sys

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit import AuditTrail, dataset_fingerprint  # noqa: E402

REQUIRED_SIGNATURES = ["mapping_review", "validation_gate", "submission_release"]
ERROR_RESOLUTIONS = {"corrected", "false_positive"}
WARNING_DISPOSITIONS = {"coded", "confirmed_acceptable", "query_raised", "false_positive"}

_MON = dict(zip(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
                 "SEP", "OCT", "NOV", "DEC"], range(1, 13)))


def _dmon_key(x: str):
    """DD-MON-YYYY -> (y, m, d) sortable tuple; None if unparseable."""
    import re
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", str(x).strip())
    if not m:
        return None
    d, mo, y = m.groups()
    return (int(y), _MON.get(mo.upper(), 0), int(d))


def _dmon_gt(a: str, b: str) -> bool:
    ka, kb = _dmon_key(a), _dmon_key(b)
    return ka is not None and kb is not None and ka > kb


class SignoffManager:
    def __init__(self, root: str):
        self.root = root
        self.cfg = yaml.safe_load(open(os.path.join(root, "config", "study.yaml")))
        self.signoff_dir = os.path.join(root, "outputs", "signoff")
        os.makedirs(self.signoff_dir, exist_ok=True)
        self.state_path = os.path.join(self.signoff_dir, "review_state.json")
        self.audit = AuditTrail(os.path.join(self.signoff_dir, "audit_trail.jsonl"))
        self.state = self._load_state()

    # ------------------------------------------------------------- state
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            return json.load(open(self.state_path))
        return {"dispositions": {}, "corrections": [], "signatures": {}}

    def _save(self):
        json.dump(self.state, open(self.state_path, "w"), indent=2)

    def _findings(self) -> pd.DataFrame:
        p = os.path.join(self.root, "outputs", "reports", "validation_findings.csv")
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return pd.DataFrame()
        return pd.read_csv(p, dtype=str).fillna("")

    # ------------------------------------------------------------- queue
    def review_queue(self) -> pd.DataFrame:
        df = self._findings()
        if df.empty:
            return df
        df = df[df["severity"].isin(["ERROR", "WARNING"])].copy()
        df["status"] = df["issue_id"].map(
            lambda i: self.state["dispositions"].get(i, {}).get("action", "OPEN"))
        return df

    # --------------------------------------------------- authorization
    def _role_can_sign(self, role: str, step: str) -> bool:
        for s in self.cfg["authorized_signers"]:
            if s["role"] == role and step in s["can_sign"]:
                return True
        return False

    # --------------------------------------------------- WARNING disposition
    def disposition_warning(self, issue_id: str, action: str,
                            reviewer: str, role: str, comment: str):
        if action not in WARNING_DISPOSITIONS:
            raise ValueError(f"Invalid warning disposition '{action}'.")
        self.state["dispositions"][issue_id] = {
            "severity": "WARNING", "action": action, "reviewer": reviewer,
            "role": role, "comment": comment, "resolves": True}
        self.audit.record(reviewer, role, "disposition_warning", issue_id,
                          {"action": action, "comment": comment})
        self._save()

    # --------------------------------------------------- ERROR resolution
    def resolve_error(self, issue_id: str, method: str, reviewer: str, role: str,
                      comment: str, correction: dict | None = None):
        """
        method='false_positive' -> justified rule override (recorded).
        method='corrected'      -> apply `correction` to the RAW EDC source (a
                                    data query fixing the value at origin) so the
                                    whole pipeline can be re-derived cleanly.
                                    `correction` = {raw_dataset, filter:{col:val},
                                    swap:[a,b]} or {raw_dataset, filter, set:{...}}.
                                    Caller must re-run mapping + validation after.
        """
        if method not in ERROR_RESOLUTIONS:
            raise ValueError(f"Invalid error resolution '{method}'.")
        applied = None
        if method == "corrected":
            if not correction:
                raise ValueError("A 'corrected' resolution requires a correction payload.")
            applied = self._apply_correction(correction, reviewer, role)
        self.state["dispositions"][issue_id] = {
            "severity": "ERROR", "action": method, "reviewer": reviewer,
            "role": role, "comment": comment, "resolves": True,
            "correction": applied}
        self.audit.record(reviewer, role, "resolve_error", issue_id,
                          {"method": method, "comment": comment, "correction": applied})
        self._save()

    def _apply_correction(self, corr: dict, reviewer: str, role: str) -> dict:
        # Correction is applied at the RAW EDC source (a resolved data query),
        # so mapping + QC reference re-derive consistently on the next run.
        path = os.path.join(self.root, "data", "raw_edc", corr["raw_dataset"])
        df = pd.read_csv(path, dtype=str).fillna("")
        mask = pd.Series(True, index=df.index)
        for col, val in corr.get("filter", {}).items():
            mask &= (df[col].astype(str) == str(val))
        changes = []
        if "swap" in corr:
            a, b = corr["swap"]
            # Idempotent: only swap rows where the dates are actually inverted,
            # so re-applying the same correction is a safe no-op.
            inv = mask & df.apply(
                lambda r: _dmon_gt(r[a], r[b]), axis=1)
            df.loc[inv, [a, b]] = df.loc[inv, [b, a]].values
            changes.append({"rows": int(inv.sum()), "swapped": [a, b]})
        for col, val in corr.get("set", {}).items():
            df.loc[mask, col] = val
            changes.append({"rows": int(mask.sum()), "set": {col: val}})
        df.to_csv(path, index=False)
        rec = {"raw_dataset": corr["raw_dataset"], "filter": corr.get("filter", {}),
               "changes": changes}
        self.state["corrections"].append({**rec, "reviewer": reviewer})
        self.audit.record(reviewer, role, "data_correction", corr["raw_dataset"], rec)
        return rec

    # --------------------------------------------------- e-signature
    def sign(self, step: str, reviewer: str, role: str, meaning: str,
             credential: str = "verified"):
        if step not in REQUIRED_SIGNATURES:
            raise ValueError(f"Unknown sign-off step '{step}'.")
        if not self._role_can_sign(role, step):
            raise PermissionError(
                f"Role '{role}' is not authorized to sign '{step}' (see study.yaml).")
        # bind signature to exact bytes of every domain dataset
        sdtm_dir = os.path.join(self.root, "outputs", "sdtm")
        hashes = {f: dataset_fingerprint(os.path.join(sdtm_dir, f))
                  for f in sorted(os.listdir(sdtm_dir)) if f.endswith(".csv")}
        sig = {"reviewer": reviewer, "role": role, "meaning": meaning,
               "credential": credential, "dataset_hashes": hashes}
        entry = self.audit.record(reviewer, role, "e_signature", step, sig)
        self.state["signatures"][step] = {**sig, "utc": entry["utc"],
                                          "entry_hash": entry["entry_hash"]}
        self._save()
        return entry

    # --------------------------------------------------- gate
    def gate_status(self) -> dict:
        df = self._findings()
        errors = df[df["severity"] == "ERROR"] if not df.empty else pd.DataFrame()
        warns = df[df["severity"] == "WARNING"] if not df.empty else pd.DataFrame()
        disp = self.state["dispositions"]

        open_errors = [r["issue_id"] for _, r in errors.iterrows()
                       if not disp.get(r["issue_id"], {}).get("resolves")]
        open_warns = [r["issue_id"] for _, r in warns.iterrows()
                      if not disp.get(r["issue_id"], {}).get("resolves")]
        missing_sigs = [s for s in REQUIRED_SIGNATURES
                        if s not in self.state["signatures"]]
        chain_ok, chain_msg = self.audit.verify_chain()

        ready = (not open_errors and not open_warns and not missing_sigs
                 and chain_ok)
        return {
            "submission_ready": ready,
            "open_errors": open_errors,
            "open_warnings": open_warns,
            "missing_signatures": missing_sigs,
            "audit_chain_ok": chain_ok,
            "audit_chain_msg": chain_msg,
            "signatures_applied": list(self.state["signatures"].keys()),
        }

    # --------------------------------------------------- release dossier
    def generate_dossier(self) -> str:
        gate = self.gate_status()
        val = json.load(open(os.path.join(self.root, "outputs", "reports",
                                          "validation_results.json")))
        dossier = {
            "study": self.cfg["study"],
            "gate_status": gate,
            "governance_metrics": {
                "domain_accuracy": val["domain_accuracy"],
                "qc_parity": val["qc_parity"],
                "metrics_pass": val["metrics_pass"],
            },
            "dispositions": self.state["dispositions"],
            "corrections": self.state["corrections"],
            "signatures": self.state["signatures"],
            "audit_entries": len(self.audit.entries()),
        }
        path = os.path.join(self.signoff_dir, "validation_dossier.json")
        json.dump(dossier, open(path, "w"), indent=2)
        self._write_dossier_html(dossier)
        return path

    def _write_dossier_html(self, d: dict):
        g = d["gate_status"]
        status = ("SUBMISSION-READY" if g["submission_ready"]
                  else "BLOCKED — human action required")
        color = "#137a3f" if g["submission_ready"] else "#b22"
        sigs = "".join(
            f"<tr><td>{s}</td><td>{v['reviewer']}</td><td>{v['role']}</td>"
            f"<td>{v['meaning']}</td><td>{v['utc']}</td></tr>"
            for s, v in d["signatures"].items()) or "<tr><td colspan=5>None</td></tr>"
        html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>Validation & Sign-off Dossier</title><style>
body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem;color:#12263a}}
h1{{color:#0b2e4f}} .badge{{display:inline-block;padding:.5rem 1rem;border-radius:8px;
color:#fff;background:{color};font-weight:700}}
table{{border-collapse:collapse;width:100%;margin-top:1rem;font-size:14px}}
th,td{{border:1px solid #e3e8ee;padding:6px 8px;text-align:left}}
th{{background:#0b2e4f;color:#fff}}</style></head><body>
<h1>VGS SDTM — Validation &amp; Sign-off Dossier</h1>
<p>Study {d['study']['studyid']} · {d['study']['sdtm_version']}</p>
<p class=badge>{status}</p>
<h2>Governance metrics</h2>
<pre>{json.dumps(d['governance_metrics'], indent=2)}</pre>
<h2>Electronic signatures (21 CFR Part 11)</h2>
<table><tr><th>Step</th><th>Reviewer</th><th>Role</th><th>Meaning</th><th>UTC</th></tr>
{sigs}</table>
<h2>Gate</h2><pre>{json.dumps(g, indent=2)}</pre>
<p>Audit trail entries: {d['audit_entries']} (tamper-evident, chain
{'intact' if g['audit_chain_ok'] else 'BROKEN'}).</p>
</body></html>"""
        open(os.path.join(self.signoff_dir, "validation_dossier.html"), "w").write(html)
