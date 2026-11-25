"""Canonical JSON + SHA-256, used for alert evidence and the audit hash chain."""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal


def _default(o):
    if isinstance(o, datetime):
        return o.astimezone(UTC).isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def normalise(obj):
    """Round-trip through JSON so the object looks exactly as it will after a JSONB read-back."""
    return json.loads(json.dumps(obj, default=_default))


def canonical(obj) -> str:
    return json.dumps(obj, default=_default, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def evidence_hash(evidence) -> str:
    return sha256(canonical(evidence))
