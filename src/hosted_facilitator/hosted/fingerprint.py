from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any


def payment_fingerprint(
    *,
    payment_payload: dict[str, Any],
    payment_requirements: dict[str, Any],
    provider: str,
    x402_version: int = 2,
) -> str:
    authorization = _authorization(payment_payload)
    accepted = payment_payload.get("accepted")
    payable = accepted if isinstance(accepted, dict) else payment_requirements
    canonical = {
        "x402Version": x402_version,
        "provider": provider,
        "scheme": payable.get("scheme"),
        "network": payable.get("network"),
        "asset": _lower(payable.get("asset")),
        "amount": _numeric_string(payable.get("amount")),
        "payTo": _lower(payable.get("payTo")),
        "payer": _lower(authorization.get("from")),
        "authorizationTo": _lower(authorization.get("to")),
        "authorizationValue": _numeric_string(authorization.get("value")),
        "authorizationNonce": _lower_hex(authorization.get("nonce")),
        "validAfter": _numeric_string(authorization.get("validAfter")),
        "validBefore": _numeric_string(authorization.get("validBefore")),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authorization(payment_payload: dict[str, Any]) -> dict[str, Any]:
    payload = payment_payload.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("authorization"), dict):
        return payload["authorization"]
    if isinstance(payment_payload.get("authorization"), dict):
        return payment_payload["authorization"]
    return {}


def _lower(value: Any) -> Any:
    return _lower_hex(value)


def _lower_hex(value: Any) -> Any:
    return value.lower() if isinstance(value, str) and value.lower().startswith("0x") else value


def _numeric_string(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric fingerprint field: {value!r}") from exc
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")
