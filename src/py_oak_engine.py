"""
py_oak_engine.py  --  Pure-Python implementation of the sdtm.oak mapping
primitives, so the accelerator runs locally with zero R dependency.

Each function mirrors a real sdtm.oak (R) verb:

    assign_no_ct       -> assign()            copy raw -> target
    hardcode_no_ct     -> hardcode()          constant value
    assign_ct          -> assign_ct()         copy + map to controlled terminology
    (derive helpers)   -> derive_seq / USUBJID / ISO-8601 date

The engine is spec-driven: it consumes the same YAML mapping spec that the R
program consumes, so switching engines never changes the mapping logic -- only
the executor. That equivalence is what makes the Python path a valid local
stand-in for the validated R path.
"""
from __future__ import annotations
import os
import re
import pandas as pd

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


# --------------------------------------------------------------------------- CT
def load_ct(ct_path: str) -> dict:
    """Return {codelist: {UPPER(synonym|decode|value): submission_value}}."""
    df = pd.read_csv(ct_path, dtype=str).fillna("")
    ct: dict[str, dict[str, str]] = {}
    for _, r in df.iterrows():
        cl = ct.setdefault(r["codelist"], {})
        sub = r["submission_value"]
        cl[sub.upper()] = sub
        if r["decode"]:
            cl[r["decode"].upper()] = sub
        for syn in filter(None, r["synonyms"].split("|")):
            cl[syn.upper()] = sub
    return ct


def ct_decode_map(ct_path: str) -> dict:
    """Return {codelist: {submission_value: decode}} for *TEST label lookups."""
    df = pd.read_csv(ct_path, dtype=str).fillna("")
    out: dict[str, dict[str, str]] = {}
    for _, r in df.iterrows():
        out.setdefault(r["codelist"], {})[r["submission_value"]] = r["decode"]
    return out


# ----------------------------------------------------------------- oak verbs
def iso_date(raw: str) -> str:
    """DD-MON-YYYY -> ISO-8601 YYYY-MM-DD. Non-parseable -> '' (partial-safe)."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", raw.strip())
    if not m:
        return ""
    d, mon, y = m.groups()
    mm = MONTHS.get(mon.upper())
    if not mm:
        return ""
    return f"{int(y):04d}-{mm:02d}-{int(d):02d}"


def assign_ct(value: str, codelist: str, ct: dict, default: str | None = None):
    """Map a raw value to its CT submission value. Returns (value, matched?)."""
    v = "" if value is None else str(value).strip()
    if v == "":
        return (default, False) if default is not None else ("", False)
    hit = ct.get(codelist, {}).get(v.upper())
    if hit is not None:
        return hit, True
    # Unmapped: keep raw so nothing is silently dropped; flag as unmatched.
    return (default if default is not None else v), False


# --------------------------------------------------------------- domain build
def _usubjid(studyid: str, subjid: str) -> str:
    return f"{studyid}-{subjid}"


def build_domain(spec: dict, raw_dir: str, ct_path: str):
    """
    Execute a mapping spec against a raw EDC dataset.
    Returns (sdtm_dataframe, unmatched_ct_log[list of dict]).
    """
    ct = load_ct(ct_path)
    labels = ct_decode_map(ct_path)
    raw = pd.read_csv(os.path.join(raw_dir, spec["raw_dataset"]), dtype=str).fillna("")
    unmatched: list[dict] = []

    if "pivot" in spec:
        rows = _pivot_source(spec, raw)
    else:
        rows = raw.to_dict("records")

    out_records = []
    for src in rows:
        rec = {}
        for v in spec["variables"]:
            var, method = v["var"], v["method"]
            if method == "hardcode":
                rec[var] = v["value"]
            elif method == "assign":
                rec[var] = src.get(v["from"], "")
            elif method == "copy_numeric":
                rec[var] = _num(src.get(v["from"], ""))
            elif method == "derive_usubjid":
                a, b = v["from"]
                rec[var] = _usubjid(src.get(a, ""), src.get(b, ""))
            elif method == "iso_date":
                rec[var] = iso_date(src.get(v["from"], ""))
            elif method == "assign_ct":
                val, ok = assign_ct(src.get(v["from"], ""), v["codelist"], ct,
                                    v.get("default"))
                rec[var] = val
                if not ok and str(src.get(v["from"], "")).strip() != "":
                    unmatched.append({
                        "domain": spec["domain"], "variable": var,
                        "codelist": v["codelist"],
                        "raw_value": src.get(v["from"], ""),
                        "usubjid": rec.get("USUBJID", ""),
                    })
            elif method == "derive_seq":
                pass  # assigned after full build (needs ordering)
            elif method == "pivot_key":
                rec[var] = src["__code"]
            elif method == "pivot_label":
                rec[var] = src["__label"]
            elif method == "pivot_value":
                rec[var] = src["__value"]
            elif method == "pivot_value_numeric":
                rec[var] = _num(src["__value"])
            elif method == "pivot_unit":
                rec[var] = src["__unit"]
            elif method == "pivot_nrlo":
                rec[var] = src.get("__low", "")
            elif method == "pivot_nrhi":
                rec[var] = src.get("__high", "")
            elif method == "nrind":
                rec[var] = _nrind(src["__value"], src.get("__low", ""),
                                  src.get("__high", ""))
            else:
                raise ValueError(f"Unknown method '{method}' for {var}")
        out_records.append(rec)

    df = pd.DataFrame(out_records)

    # derive_seq: number records within `by` group, ordered by key_vars
    seq_spec = next((v for v in spec["variables"] if v["method"] == "derive_seq"), None)
    if seq_spec is not None and not df.empty:
        by = seq_spec["by"]
        sort_keys = [k for k in spec.get("key_vars", []) if k in df.columns]
        df = df.sort_values([by] + sort_keys, kind="stable").reset_index(drop=True)
        df[seq_spec["var"]] = df.groupby(by).cumcount() + 1

    # order columns as declared in spec
    cols = [v["var"] for v in spec["variables"]]
    df = df[[c for c in cols if c in df.columns]]
    return df, unmatched


def _pivot_source(spec: dict, raw: pd.DataFrame):
    """Wide -> tall expansion for Findings domains (VS, LB). Measures use generic
    keys: column, code, label, unit, and optional low/high reference bounds."""
    pv = spec["pivot"]
    rows = []
    for _, r in raw.iterrows():
        for meas in pv["measures"]:
            val = str(r.get(meas["column"], "")).strip()
            if val == "":
                continue  # skip not-collected cells
            base = {k: r.get(k, "") for k in pv["id_vars"]}
            base.update({
                "__code": meas["code"],
                "__label": meas["label"],
                "__value": val,
                "__unit": meas.get("unit", ""),
                "__low": meas.get("low", ""),
                "__high": meas.get("high", ""),
            })
            rows.append(base)
    return rows


def _nrind(value, low, high) -> str:
    """Reference-range indicator: NORMAL / HIGH / LOW (empty if not computable)."""
    v, lo, hi = _num(value), _num(low), _num(high)
    if v == "" or lo == "" or hi == "":
        return ""
    if v < lo:
        return "LOW"
    if v > hi:
        return "HIGH"
    return "NORMAL"


def _num(x):
    try:
        f = float(str(x).strip())
        return int(f) if f.is_integer() else f
    except (ValueError, AttributeError):
        return ""
