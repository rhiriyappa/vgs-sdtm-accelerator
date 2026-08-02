"""
define_xml.py  --  Minimal define.xml (CDISC Define-XML 2.1) generator.

define.xml is the machine-readable dataset metadata that accompanies every FDA
SDTM submission. This produces a compact but well-formed stub describing each
domain, its variables, key variables, and controlled-terminology references,
derived directly from the mapping specs -- enough to demonstrate the submission
metadata deliverable in the pipeline.
"""
from __future__ import annotations
import os
from xml.sax.saxutils import escape
import yaml


def generate(root: str) -> str:
    cfg = yaml.safe_load(open(os.path.join(root, "config", "study.yaml")))
    domains = cfg["study"]["domains"]
    itemgroups, itemdefs = [], []

    for d in domains:
        spec = yaml.safe_load(open(os.path.join(root, "specs", f"{d.lower()}.yaml")))
        keys = spec.get("key_vars", [])
        varrefs = []
        for i, v in enumerate(spec["variables"], start=1):
            oid = f"IT.{d}.{v['var']}"
            key_seq = f' KeySequence="{keys.index(v["var"]) + 1}"' if v["var"] in keys else ""
            varrefs.append(
                f'      <ItemRef ItemOID="{oid}" OrderNumber="{i}" '
                f'Mandatory="Yes"{key_seq}/>')
            cl = f' Codelist="{v.get("codelist")}"' if v.get("codelist") else ""
            itemdefs.append(
                f'  <ItemDef OID="{oid}" Name="{v["var"]}" DataType="text"'
                f'{cl}><Description><TranslatedText xml:lang="en">'
                f'{escape(v["method"])}</TranslatedText></Description></ItemDef>')
        itemgroups.append(
            f'  <ItemGroupDef OID="IG.{d}" Name="{d}" Repeating='
            f'"{"No" if spec["structure"].startswith("one_record_per_subject") else "Yes"}" '
            f'Purpose="Tabulation" Structure="{escape(spec["structure"])}">\n'
            + "\n".join(varrefs) + f'\n  </ItemGroupDef>')

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ODM xmlns="http://www.cdisc.org/ns/odm/v1.3"
     xmlns:def="http://www.cdisc.org/ns/def/v2.1"
     FileType="Snapshot" ODMVersion="1.3.2"
     SourceSystem="VGS SDTM Delivery Accelerator">
 <Study OID="{cfg['study']['studyid']}">
  <GlobalVariables>
   <StudyName>{cfg['study']['studyid']}</StudyName>
   <StudyDescription>Demo SDTM submission metadata</StudyDescription>
   <ProtocolName>{cfg['study']['studyid']}</ProtocolName>
  </GlobalVariables>
  <MetaDataVersion OID="MDV.1" Name="{cfg['study']['sdtm_version']}"
     def:DefineVersion="2.1.0" def:StandardName="SDTMIG"
     def:StandardVersion="3.4">
{chr(10).join(itemgroups)}
{chr(10).join(itemdefs)}
  </MetaDataVersion>
 </Study>
</ODM>
"""
    out = os.path.join(root, "outputs", "signoff", "define.xml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w").write(xml)
    return out


if __name__ == "__main__":
    r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("define.xml written:", generate(r))
