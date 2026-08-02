"""
oak_wrapper.py  --  Python wrapper around the SDTM mapping step.

Responsibility: given the study specs + raw EDC + CT, produce SDTM datasets.
It selects the execution engine transparently:

    engine = "R"       -> shell out to Rscript R/map_sdtm_oak.R  (real sdtm.oak)
    engine = "python"  -> py_oak_engine (identical spec, local fallback)
    engine = "auto"    -> use R if Rscript + sdtm.oak are available, else python

Both engines emit the same columns, so downstream validation / sign-off is
engine-agnostic. Output: outputs/sdtm/<domain>.csv and .xpt (SAS transport v5),
plus a mapping manifest recording engine, versions, and unmatched-CT log.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import py_oak_engine as oak  # noqa: E402


def _r_available() -> bool:
    if not shutil.which("Rscript"):
        return False
    try:
        out = subprocess.run(
            ["Rscript", "-e", 'cat(requireNamespace("sdtm.oak", quietly=TRUE))'],
            capture_output=True, text=True, timeout=60)
        return out.stdout.strip().upper() == "TRUE"
    except Exception:
        return False


def _xpt_name(df: pd.DataFrame) -> pd.DataFrame:
    """SAS XPT v5 needs <=8 char, uppercase var names; already SDTM-compliant."""
    return df.rename(columns={c: c[:8].upper() for c in df.columns})


def run_mapping(root: str, engine: str = "auto") -> dict:
    cfg = yaml.safe_load(open(os.path.join(root, "config", "study.yaml")))
    domains = cfg["study"]["domains"]
    out_dir = os.path.join(root, "outputs", "sdtm")
    os.makedirs(out_dir, exist_ok=True)
    ct_path = os.path.join(root, "ct", "controlled_terminology.csv")
    raw_dir = os.path.join(root, "data", "raw_edc")

    chosen = engine
    if engine == "auto":
        chosen = "R" if _r_available() else "python"

    manifest = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "engine_requested": engine,
        "engine_used": chosen,
        "sdtm_version": cfg["study"]["sdtm_version"],
        "ct_version": cfg["study"]["ct_version"],
        "domains": {},
        "unmatched_ct": [],
    }

    if chosen == "R":
        r_script = os.path.join(root, "R", "map_sdtm_oak.R")
        proc = subprocess.run(["Rscript", r_script, root, out_dir],
                              capture_output=True, text=True)
        manifest["r_stdout"] = proc.stdout.strip()
        manifest["r_stderr"] = proc.stderr.strip()
        if proc.returncode != 0:
            raise RuntimeError(f"sdtm.oak R mapping failed:\n{proc.stderr}")
        for d in domains:
            df = pd.read_csv(os.path.join(out_dir, f"{d.lower()}.csv"), dtype=str)
            _write_xpt(df, out_dir, d)
            manifest["domains"][d] = {"records": len(df),
                                      "variables": list(df.columns)}
    else:
        for d in domains:
            spec = yaml.safe_load(open(os.path.join(root, "specs", f"{d.lower()}.yaml")))
            df, unmatched = oak.build_domain(spec, raw_dir, ct_path)
            df.to_csv(os.path.join(out_dir, f"{d.lower()}.csv"), index=False)
            _write_xpt(df, out_dir, d)
            manifest["domains"][d] = {"records": len(df),
                                      "variables": list(df.columns)}
            manifest["unmatched_ct"].extend(unmatched)

    with open(os.path.join(out_dir, "mapping_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _write_xpt(df: pd.DataFrame, out_dir: str, domain: str):
    """Write SAS transport (XPT v5) -- the FDA submission transport format."""
    try:
        import pyreadstat
        x = _xpt_name(df.copy())
        pyreadstat.write_xport(
            x, os.path.join(out_dir, f"{domain.lower()}.xpt"),
            table_name=domain.upper(), file_label=f"{domain.upper()} domain")
    except Exception as e:  # xpt is a bonus; never fail the run on it
        print(f"  [warn] XPT write skipped for {domain}: {e}")


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    eng = sys.argv[1] if len(sys.argv) > 1 else "auto"
    m = run_mapping(root, eng)
    print(json.dumps({k: v for k, v in m.items() if k != "unmatched_ct"}, indent=2))
    print("unmatched CT values:", len(m["unmatched_ct"]))
