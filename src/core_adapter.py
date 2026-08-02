"""
core_adapter.py  --  Integration seam for the real CDISC CORE engine.

The strategy deck is explicit: "Don't rebuild validation, leverage it ... CDISC
CORE is open source. You integrate them as the compliance gate rather than
reinventing them." This adapter is that integration.

Design decision (learned the hard way): the `cdisc-rules-engine` package pins
old pandas/numpy and conflicts with a modern analysis stack, so CORE is invoked
OUT OF PROCESS via its CLI in its own environment -- never imported here. The
adapter:

  1. detects whether the `core` CLI + a downloaded rules cache are available;
  2. if so, runs CORE over the mapped XPT datasets + define.xml and parses its
     JSON report;
  3. normalizes CORE findings into the same finding model the built-in engine
     uses, tagged source="CDISC CORE", so they flow through one report and one
     human-sign-off gate;
  4. if CORE is not configured, returns cleanly unavailable and the pipeline
     continues on the built-in engine (same graceful-fallback pattern as R vs
     Python mapping).

Because CORE's rules cache requires a CDISC Library API key + network to build,
the adapter also supports ingesting an ALREADY-PRODUCED CORE JSON report
(`--core-report path.json`), which is the most robust way to wire real CORE
findings into the gate in a validated/offline environment.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess

# CORE execution status / severity -> our model
_SEV_MAP = {
    "error": "ERROR", "reject": "ERROR", "failed": "ERROR",
    "warning": "WARNING", "warn": "WARNING", "notice": "NOTICE",
    "info": "NOTICE",
}


def detect_core() -> dict:
    """Return {available, cmd, cache, reason}."""
    cmd = os.environ.get("CORE_CMD", "core")
    exe = shutil.which(cmd) or (cmd if os.path.exists(cmd) else None)
    if not exe:
        return {"available": False, "cmd": cmd, "cache": None,
                "reason": f"CORE CLI '{cmd}' not found on PATH "
                          "(install cdisc-rules-engine in its own env)."}
    cache = os.environ.get("CORE_CACHE",
                           os.path.expanduser("~/.core/cache"))
    if not os.path.isdir(cache):
        return {"available": False, "cmd": exe, "cache": cache,
                "reason": f"CORE rules cache not found at {cache} "
                          "(run `core update-cache` with a CDISC Library API key)."}
    return {"available": True, "cmd": exe, "cache": cache, "reason": "ready"}


def run_core(root: str, standard: str = "sdtmig", version: str = "3-4") -> dict:
    """
    Run CORE over the mapped XPT datasets + define.xml.
    Returns {ran, report_path, reason}. Never raises on CORE failure.
    """
    det = detect_core()
    reports = os.path.join(root, "outputs", "reports")
    os.makedirs(reports, exist_ok=True)
    out_prefix = os.path.join(reports, "core_report")
    if not det["available"]:
        return {"ran": False, "report_path": None, "reason": det["reason"]}

    sdtm_dir = os.path.join(root, "outputs", "sdtm")
    define = os.path.join(root, "outputs", "signoff", "define.xml")
    cmd = [det["cmd"], "validate",
           "-s", standard, "-v", version,
           "-dp", sdtm_dir,
           "-of", "JSON", "-o", out_prefix]
    if os.path.exists(define):
        cmd += ["-dxp", define]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        rp = out_prefix + ".json"
        if proc.returncode == 0 and os.path.exists(rp):
            return {"ran": True, "report_path": rp, "reason": "ok"}
        return {"ran": False, "report_path": None,
                "reason": f"CORE exited {proc.returncode}: {proc.stderr[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ran": False, "report_path": None, "reason": str(e)}


def normalize_core_report(report: dict | list) -> list[dict]:
    """
    Convert a CORE JSON report into unified findings. CORE report layouts vary by
    version, so this walks tolerantly for records that carry a rule id + message.
    Returns [{rule_id, severity, domain, variable, usubjid, value, message}].
    """
    findings: list[dict] = []

    def sev(x) -> str:
        return _SEV_MAP.get(str(x).strip().lower(), "WARNING")

    def emit(rule_id, severity, domain, variable, usubjid, message):
        findings.append({
            "rule_id": f"CORE:{rule_id}" if rule_id else "CORE",
            "severity": sev(severity), "domain": (domain or "").upper(),
            "variable": variable or "", "usubjid": usubjid or "",
            "value": "", "message": (message or "").strip(),
        })

    # Common shape: {"results":[{"id"/"core_id","domains","variables","severity",
    #   "message","errors":[{"row","value","USUBJID",...}]}, ...]}
    results = []
    if isinstance(report, dict):
        for key in ("results", "issues", "conformance_details", "data"):
            if isinstance(report.get(key), list):
                results = report[key]
                break
    elif isinstance(report, list):
        results = report

    for item in results:
        if not isinstance(item, dict):
            continue
        rid = (item.get("core_id") or item.get("id") or item.get("rule_id")
               or item.get("ruleId") or "")
        severity = (item.get("severity") or item.get("executability")
                    or item.get("status") or "warning")
        message = (item.get("message") or item.get("description")
                   or item.get("rule") or "")
        domains = item.get("domains") or item.get("domain") or ""
        if isinstance(domains, list):
            domains = domains[0] if domains else ""
        variables = item.get("variables") or item.get("variable") or ""
        if isinstance(variables, list):
            variables = ", ".join(variables)
        errs = item.get("errors") or item.get("records") or []
        if isinstance(errs, list) and errs:
            for e in errs:
                usub = (e.get("USUBJID") or e.get("usubjid")
                        or e.get("uSubjId") or "") if isinstance(e, dict) else ""
                emit(rid, severity, domains, variables, usub,
                     (e.get("value") and f"{message} (value={e['value']})")
                     if isinstance(e, dict) else message)
        else:
            emit(rid, severity, domains, variables, "", message)
    return findings


def get_core_findings(root: str, mode: str = "off",
                      core_report: str | None = None) -> dict:
    """
    mode: 'off'    -> skip CORE (default)
          'auto'   -> run CORE CLI if available, else unavailable
          'report' -> parse an existing CORE JSON report (path in core_report)
    Returns {source, ran, reason, findings}.
    """
    if mode == "off":
        return {"source": "CDISC CORE", "ran": False,
                "reason": "CORE integration disabled (mode=off).", "findings": []}
    if mode == "report" or core_report:
        path = core_report
        if not path or not os.path.exists(path):
            return {"source": "CDISC CORE", "ran": False,
                    "reason": f"CORE report not found: {path}", "findings": []}
        report = json.load(open(path))
        return {"source": "CDISC CORE", "ran": True, "reason": f"parsed {path}",
                "findings": normalize_core_report(report)}
    # mode == 'auto'
    res = run_core(root)
    if not res["ran"]:
        return {"source": "CDISC CORE", "ran": False, "reason": res["reason"],
                "findings": []}
    report = json.load(open(res["report_path"]))
    return {"source": "CDISC CORE", "ran": True,
            "reason": res["reason"], "findings": normalize_core_report(report)}


if __name__ == "__main__":
    import sys
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(detect_core(), indent=2))
    if len(sys.argv) > 1:
        print(json.dumps(get_core_findings(r, "report", sys.argv[1]), indent=2))
