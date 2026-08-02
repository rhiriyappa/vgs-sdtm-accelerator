"""
audit.py  --  Tamper-evident 21 CFR Part 11 audit trail.

Append-only JSONL log where each entry carries a SHA-256 hash chained to the
previous entry (blockchain-style). Any retroactive edit to a past entry breaks
the chain, which verify_chain() detects. Every entry records the Part 11
essentials: WHO (actor + role), WHEN (UTC, tz-aware), WHAT (action + target),
and full context. This is the audit backbone behind the deck's "full audit trail
& model versioning" and "zero validation escapes" commitments.
"""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone

GENESIS = "0" * 64


def _hash(payload: dict, prev_hash: str) -> str:
    blob = json.dumps(payload, sort_keys=True) + prev_hash
    return hashlib.sha256(blob.encode()).hexdigest()


class AuditTrail:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _last_hash(self) -> str:
        if not os.path.exists(self.path):
            return GENESIS
        last = GENESIS
        with open(self.path) as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["entry_hash"]
        return last

    def _seq(self) -> int:
        if not os.path.exists(self.path):
            return 0
        with open(self.path) as f:
            return sum(1 for line in f if line.strip())

    def record(self, actor: str, role: str, action: str, target: str,
               details: dict | None = None) -> dict:
        prev = self._last_hash()
        payload = {
            "seq": self._seq() + 1,
            "utc": datetime.now(timezone.utc).isoformat(),
            "actor": actor, "role": role, "action": action,
            "target": target, "details": details or {},
            "prev_hash": prev,
        }
        payload["entry_hash"] = _hash(payload, prev)
        with open(self.path, "a") as f:
            f.write(json.dumps(payload) + "\n")
        return payload

    def entries(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def verify_chain(self) -> tuple[bool, str]:
        prev = GENESIS
        for e in self.entries():
            recomputed = _hash({k: e[k] for k in
                                ("seq", "utc", "actor", "role", "action",
                                 "target", "details", "prev_hash")}, prev)
            if e["prev_hash"] != prev:
                return False, f"seq {e['seq']}: prev_hash mismatch (tampering)."
            if e["entry_hash"] != recomputed:
                return False, f"seq {e['seq']}: entry_hash mismatch (tampering)."
            prev = e["entry_hash"]
        return True, "Audit chain intact."


def dataset_fingerprint(path: str) -> str:
    """SHA-256 of a dataset file -- binds a signature to exact bytes signed."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
