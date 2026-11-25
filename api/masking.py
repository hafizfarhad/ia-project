"""PII masking. Raw CNIC / MSISDN never leave the API unless explicitly unmasked (and audited)."""
from __future__ import annotations


def mask_cnic(cnic: str | None) -> str | None:
    # 35202-1234567-3 -> 35202-*******-3
    if not cnic:
        return cnic
    parts = cnic.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}"
    return "*" * max(0, len(cnic) - 2) + cnic[-2:]


def mask_msisdn(msisdn: str | None) -> str | None:
    # 0300-1234567 -> 0300-***4567
    if not msisdn:
        return msisdn
    if "-" in msisdn:
        pre, rest = msisdn.split("-", 1)
        return f"{pre}-{'*' * max(0, len(rest) - 4)}{rest[-4:]}"
    return "*" * max(0, len(msisdn) - 4) + msisdn[-4:]


def mask_account(acc: dict) -> dict:
    out = dict(acc)
    out["cnic"] = mask_cnic(acc.get("cnic"))
    out["msisdn"] = mask_msisdn(acc.get("msisdn"))
    out["pii_masked"] = True
    return out
