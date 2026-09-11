from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from hosted_facilitator.hosted.schemas import FacilitatorEnvelope


class ProtocolValidationError(ValueError):
    """Raised when a hosted facilitator request is not settlement-safe."""


def validate_settlement_envelope(envelope: FacilitatorEnvelope) -> dict[str, Any]:
    accepted = envelope.payment_payload.get("accepted")
    if not isinstance(accepted, dict):
        raise ProtocolValidationError("paymentPayload.accepted is required")

    requirements = envelope.payment_requirements
    if not isinstance(requirements, dict):
        raise ProtocolValidationError("paymentRequirements must be an object")

    for field in ("scheme", "network", "asset", "amount", "payTo"):
        if field not in accepted:
            raise ProtocolValidationError(f"paymentPayload.accepted.{field} is required")
        if field not in requirements:
            raise ProtocolValidationError(f"paymentRequirements.{field} is required")
        if _canonical_value(field, accepted[field]) != _canonical_value(field, requirements[field]):
            raise ProtocolValidationError(
                f"paymentPayload.accepted.{field} does not match paymentRequirements.{field}"
            )

    if (
        "maxTimeoutSeconds" in accepted
        and "maxTimeoutSeconds" in requirements
        and str(accepted["maxTimeoutSeconds"]) != str(requirements["maxTimeoutSeconds"])
    ):
        raise ProtocolValidationError(
            "paymentPayload.accepted.maxTimeoutSeconds does not match "
            "paymentRequirements.maxTimeoutSeconds"
        )

    if envelope.payment_payload.get("x402Version") != envelope.x402_version:
        raise ProtocolValidationError("paymentPayload.x402Version does not match x402Version")

    accepted_extra = accepted.get("extra")
    requirements_extra = requirements.get("extra")
    if accepted_extra != requirements_extra:
        raise ProtocolValidationError(
            "paymentPayload.accepted.extra does not match paymentRequirements.extra"
        )

    return accepted


def _canonical_value(field: str, value: Any) -> str:
    if field in {"asset", "payTo"} and isinstance(value, str):
        return value.lower()
    if field == "amount":
        return _canonical_amount(value)
    return str(value)


def _canonical_amount(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProtocolValidationError(f"Invalid payment amount: {value!r}") from exc
    if amount != amount.to_integral_value():
        return format(amount.normalize(), "f")
    return str(int(amount))
