from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FacilitatorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    x402_version: int = Field(alias="x402Version")
    payment_payload: dict[str, Any] = Field(alias="paymentPayload")
    payment_requirements: dict[str, Any] = Field(alias="paymentRequirements")


class VerifyOutcome(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    is_valid: bool = Field(alias="isValid")
    payer: str | None = None
    invalid_reason: str | None = Field(default=None, alias="invalidReason")
    trace_id: str | None = Field(default=None, alias="traceId")
    provider: str | None = None


class ProviderSettlementStatus(StrEnum):
    SETTLED = "settled"
    SUBMITTED = "submitted"
    FAILED_FINAL = "failed_final"
    UNKNOWN = "unknown"


class SettleOutcome(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    transaction: str | None = None
    network: str | None = None
    payer: str | None = None
    error_reason: str | None = Field(default=None, alias="errorReason")
    settlement_status: ProviderSettlementStatus | None = Field(
        default=None,
        alias="settlementStatus",
    )
    trace_id: str | None = Field(default=None, alias="traceId")
    provider: str | None = None
    duplicate: bool | None = None


class SupportedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kinds: list[dict[str, Any]]
    extensions: list[str] = Field(default_factory=list)
    signers: dict[str, list[str]] = Field(default_factory=dict)
    trace_id: str | None = Field(default=None, alias="traceId")
