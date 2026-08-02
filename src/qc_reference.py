"""
qc_reference.py  --  Independent (double-programmed) SDTM reference mapping.

In pharma biometrics, the QC standard is INDEPENDENT double programming: a second
programmer maps the raw data without reusing the production code, and the two
outputs are compared cell-by-cell. Agreement = "QC parity" (deck target >= 99%).

This module is that second, deliberately independent implementation. It uses
plain pandas transforms and does NOT import py_oak_engine or read the YAML specs,
so a systematic error in the production engine cannot silently propagate here.
The validator compares production output against this reference to compute parity.
"""
from __future__ import annotations
import os
import re
import pandas as pd

_MON = dict(zip(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
                 "SEP", "OCT", "NOV", "DEC"], range(1, 13)))


def _iso(x):
    if not isinstance(x, str):
        return ""
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", x.strip())
    if not m:
        return ""
    d, mo, y = m.groups()
    return f"{int(y):04d}-{_MON[mo.upper()]:02d}-{int(d):02d}"


_SEX = {"MALE": "M", "FEMALE": "F", "M": "M", "F": "F", "U": "U", "1": "M", "2": "F"}


def build(root: str) -> dict:
    raw = os.path.join(root, "data", "raw_edc")
    qc_dir = os.path.join(root, "outputs", "sdtm", "qc")
    os.makedirs(qc_dir, exist_ok=True)
    out = {}

    # ---- DM ----
    dm = pd.read_csv(os.path.join(raw, "dm_raw.csv"), dtype=str).fillna("")
    dm_qc = pd.DataFrame({
        "USUBJID": dm["STUDYID"] + "-" + dm["SUBJID"],
        "SEX": dm["SEX"].str.upper().map(_SEX).fillna(""),
        "RACE": dm["RACE"].str.upper().replace("", "UNKNOWN"),
        "BRTHDTC": dm["BRTHDAT"].map(_iso),
        "RFSTDTC": dm["FRSTDSDAT"].map(_iso),
    })
    out["DM"] = dm_qc
    dm_qc.to_csv(os.path.join(qc_dir, "dm_qc.csv"), index=False)

    # ---- AE ----
    ae = pd.read_csv(os.path.join(raw, "ae_raw.csv"), dtype=str).fillna("")
    ae_qc = pd.DataFrame({
        "USUBJID": ae["STUDYID"] + "-" + ae["SUBJID"],
        "AETERM": ae["AETERM"],
        "AESTDTC": ae["AESTDAT"].map(_iso),
        "AEENDTC": ae["AEENDAT"].map(_iso),
        "AESEV": ae["AESEV"].str.upper(),
    })
    out["AE"] = ae_qc
    ae_qc.to_csv(os.path.join(qc_dir, "ae_qc.csv"), index=False)

    # ---- VS ----  independent wide->tall via melt
    vs = pd.read_csv(os.path.join(raw, "vs_raw.csv"), dtype=str).fillna("")
    meas = ["SYSBP", "DIABP", "PULSE", "TEMP", "HEIGHT", "WEIGHT"]
    vs["USUBJID"] = vs["STUDYID"] + "-" + vs["SUBJID"]
    tall = vs.melt(id_vars=["USUBJID", "VISITNUM", "VSDAT"], value_vars=meas,
                   var_name="VSTESTCD", value_name="VSORRES")
    tall = tall[tall["VSORRES"] != ""].copy()
    tall["VSDTC"] = tall["VSDAT"].map(_iso)
    out["VS"] = tall[["USUBJID", "VISITNUM", "VSTESTCD", "VSORRES", "VSDTC"]]
    out["VS"].to_csv(os.path.join(qc_dir, "vs_qc.csv"), index=False)

    # ---- LB ----  independent wide->tall via melt
    lb = pd.read_csv(os.path.join(raw, "lb_raw.csv"), dtype=str).fillna("")
    lbmeas = ["WBC", "HGB", "PLAT", "ALT", "AST", "CREAT", "GLUC"]
    lb["USUBJID"] = lb["STUDYID"] + "-" + lb["SUBJID"]
    lbtall = lb.melt(id_vars=["USUBJID", "VISITNUM", "LBDAT"], value_vars=lbmeas,
                     var_name="LBTESTCD", value_name="LBORRES")
    lbtall = lbtall[lbtall["LBORRES"] != ""].copy()
    lbtall["LBDTC"] = lbtall["LBDAT"].map(_iso)
    out["LB"] = lbtall[["USUBJID", "VISITNUM", "LBTESTCD", "LBORRES", "LBDTC"]]
    out["LB"].to_csv(os.path.join(qc_dir, "lb_qc.csv"), index=False)

    # ---- CM ----
    cm = pd.read_csv(os.path.join(raw, "cm_raw.csv"), dtype=str).fillna("")
    cm_qc = pd.DataFrame({
        "USUBJID": cm["STUDYID"] + "-" + cm["SUBJID"],
        "CMTRT": cm["CMTRT"],
        "CMSTDTC": cm["CMSTDAT"].map(_iso),
        "CMENDTC": cm["CMENDAT"].map(_iso),
        "CMDOSE": cm["CMDOSE"],
    })
    out["CM"] = cm_qc
    cm_qc.to_csv(os.path.join(qc_dir, "cm_qc.csv"), index=False)

    return out


if __name__ == "__main__":
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    res = build(r)
    for k, v in res.items():
        print(f"QC reference {k}: {len(v)} rows")
