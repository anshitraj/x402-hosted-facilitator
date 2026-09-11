from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.providers.base import (
    HostedFacilitatorProvider,
    HostedProviderUnavailableError,
)
from hosted_facilitator.hosted.reconciliation import ReconciliationOutcome, ReconciliationStatus
from hosted_facilitator.hosted.schemas import (
    FacilitatorEnvelope,
    ProviderSettlementStatus,
    SettleOutcome,
    VerifyOutcome,
)
from hosted_facilitator.hosted.storage import SettlementRecord
from hosted_facilitator.hosted.validation import ProtocolValidationError
from hosted_facilitator.nanopayments.client import NanopaymentClient
from hosted_facilitator.nanopayments.exceptions import (
    GatewayAPIError,
    GatewayConnectionError,
    GatewayTimeoutError,
    NanopaymentError,
    SettlementError,
    VerificationError,
)
from hosted_facilitator.nanopayments.types import (
    PaymentPayload,
    PaymentRequirements,
    PaymentRequirementsKind,
)

HOSTED_CIRCLE_GATEWAY_ENABLED_ENV = "OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED"
HOSTED_CIRCLE_GATEWAY_ENVIRONMENT_ENV = "OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENVIRONMENT"
HOSTED_CIRCLE_GATEWAY_BASE_URL_ENV = "OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL"
HOSTED_CIRCLE_GATEWAY_NETWORKS_ENV = "OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS"
HOSTED_CIRCLE_GATEWAY_TIMEOUT_ENV = "OMNICLAW_HOSTED_CIRCLE_GATEWAY_TIMEOUT_SECONDS"
_GATEWAY_BATCHED_NAME = "GatewayWalletBatched"
_EIP155_NETWORK_RE = re.compile(r"^eip155:\d{1,20}$")
_CIRCLE_ERROR_REASON_ALLOWLIST = {
    "address_mismatch",
    "amount_mismatch",
    "authorization_expired",
    "authorization_not_yet_valid",
    "authorization_validity_too_short",
    "insufficient_balance",
    "invalid_payload",
    "invalid_signature",
    "network_mismatch",
    "nonce_already_used",
    "self_transfer",
    "settle_exact_evm_transaction_confirmation_timed_out",
    "settle_exact_failed_onchain",
    "settle_exact_node_failure",
    "settle_exact_svm_block_height_exceeded",
    "settle_exact_svm_transaction_confirmation_timed_out",
    "unexpected_error",
    "unsupported_asset",
    "unsupported_domain",
    "unsupported_network",
    "unsupported_scheme",
    "wallet_not_found",
}
_AMBIGUOUS_SETTLEMENT_REASONS = {
    "settle_exact_evm_transaction_confirmation_timed_out",
    "settle_exact_node_failure",
    "settle_exact_svm_block_height_exceeded",
    "settle_exact_svm_transaction_confirmation_timed_out",
    "unexpected_error",
}


class HostedCircleGatewayProvider(HostedFacilitatorProvider):
    name = "circle_gateway"

    def __init__(
        self,
        client: Any,
        *,
        networks: tuple[str, ...] = (),
    ):
        self._client = client
        self.supported_networks = tuple(networks)

    def matches(self, *, scheme: str | None, network: str | None, extra: dict) -> bool:
        return (
            scheme == "exact"
            and extra.get("name") == _GATEWAY_BATCHED_NAME
            and (not self.supported_networks or network in self.supported_networks)
        )

    async def supported(self, context: RequestContext) -> list[dict]:
        del context
        kinds = await self._client.get_supported()
        result = []
        for kind in kinds:
            data = kind.to_dict() if hasattr(kind, "to_dict") else dict(kind)
            if not _is_gateway_kind(data):
                continue
            network = data.get("network")
            if self.supported_networks and network not in self.supported_networks:
                continue
            if not _has_gateway_metadata(data):
                continue
            result.append(data)
        return result

    async def health_check(self) -> dict[str, Any]:
        try:
            kinds = await self._client.get_supported()
        except GatewayAPIError as exc:
            return {
                "status": "unhealthy",
                "errorType": type(exc).__name__,
                "provider": self.name,
            }
        supported_networks = []
        for kind in kinds:
            data = kind.to_dict() if hasattr(kind, "to_dict") else dict(kind)
            if not _is_gateway_kind(data):
                continue
            network = data.get("network")
            if self.supported_networks and network not in self.supported_networks:
                continue
            if not _has_gateway_metadata(data):
                continue
            supported_networks.append(str(network))
        missing_networks = sorted(set(self.supported_networks) - set(supported_networks))
        if self.supported_networks and missing_networks:
            return {
                "status": "unhealthy",
                "errorType": "GatewayNetworkUnavailable",
                "provider": self.name,
                "networkCount": len(supported_networks),
                "supportedNetworks": tuple(sorted(supported_networks)),
                "missingNetworkCount": len(missing_networks),
            }
        if not supported_networks:
            return {
                "status": "unhealthy",
                "errorType": "GatewayKindsUnavailable",
                "provider": self.name,
                "networkCount": 0,
                "supportedNetworks": (),
            }
        return {
            "status": "ok",
            "provider": self.name,
            "networkCount": len(supported_networks),
            "supportedNetworks": tuple(sorted(supported_networks)),
        }

    def validate_envelope(self, envelope: FacilitatorEnvelope) -> None:
        requirement = _gateway_requirement_kind(envelope.payment_requirements)
        if self.supported_networks and requirement.network not in self.supported_networks:
            raise ProtocolValidationError("Circle Gateway network is not enabled")
        payload = _gateway_payment_payload(envelope)
        if payload.accepted is None:
            raise ProtocolValidationError("paymentPayload.accepted is required")

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        del context
        payload, requirements = _circle_payment_objects(envelope)
        try:
            result = await self._client.verify(payload, requirements)
        except (GatewayTimeoutError, GatewayConnectionError) as exc:
            raise HostedProviderUnavailableError("circle_gateway_verify_unavailable") from exc
        except GatewayAPIError as exc:
            if exc.status_code in {400, 402}:
                return VerifyOutcome(
                    isValid=False,
                    invalidReason="circle_gateway_verification_rejected",
                )
            raise HostedProviderUnavailableError("circle_gateway_verify_error") from exc
        return VerifyOutcome(
            isValid=getattr(result, "is_valid", None) is True,
            payer=getattr(result, "payer", None),
            invalidReason=getattr(result, "invalid_reason", None),
        )

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        del context
        payload, requirements = _circle_payment_objects(envelope)
        network = envelope.payment_requirements.get("network")
        try:
            result = await self._client.settle(payload, requirements)
        except (GatewayTimeoutError, GatewayConnectionError):
            return SettleOutcome(
                success=False,
                network=network,
                errorReason="circle_gateway_outcome_unknown",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
            )
        except VerificationError as exc:
            return SettleOutcome(
                success=False,
                network=network,
                payer=getattr(exc, "payer", None),
                errorReason=_circle_error_reason(exc),
                settlementStatus=ProviderSettlementStatus.FAILED_FINAL,
            )
        except SettlementError as exc:
            settlement_status = (
                ProviderSettlementStatus.UNKNOWN
                if _is_ambiguous_settlement_error(exc)
                else ProviderSettlementStatus.FAILED_FINAL
            )
            return SettleOutcome(
                success=False,
                transaction=getattr(exc, "transaction", None),
                network=network,
                payer=getattr(exc, "payer", None),
                errorReason=_circle_error_reason(exc),
                settlementStatus=settlement_status,
            )
        except GatewayAPIError:
            return SettleOutcome(
                success=False,
                network=network,
                errorReason="circle_gateway_outcome_unknown",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
            )
        data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        success = data.get("success") is True
        transaction = data.get("transaction")
        if success and not _safe_nonempty_string(transaction):
            return SettleOutcome(
                success=False,
                network=network,
                payer=data.get("payer"),
                errorReason="circle_gateway_outcome_unknown",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
            )
        return SettleOutcome(
            success=success,
            transaction=transaction,
            network=network,
            payer=data.get("payer"),
            errorReason=data.get("errorReason") or data.get("error_reason"),
            settlementStatus=(
                ProviderSettlementStatus.SETTLED
                if success
                else ProviderSettlementStatus.FAILED_FINAL
            ),
        )

    async def reconcile_settlement(self, record: SettlementRecord) -> ReconciliationOutcome:
        if record.status.value == "settled":
            return ReconciliationOutcome(
                status=ReconciliationStatus.SETTLED,
                transaction=record.transaction,
                payer=record.payer,
            )
        if not _safe_nonempty_string(record.transaction):
            return ReconciliationOutcome(
                status=ReconciliationStatus.UNKNOWN,
                transaction=record.transaction,
                payer=record.payer,
                error_reason="circle_gateway_missing_transfer_id",
            )
        try:
            transfer = await self._client.get_x402_transfer(record.transaction)
        except (GatewayTimeoutError, GatewayConnectionError, GatewayAPIError):
            return ReconciliationOutcome(
                status=ReconciliationStatus.PENDING,
                transaction=record.transaction,
                payer=record.payer,
                error_reason="circle_gateway_reconciliation_unavailable",
            )
        status = _circle_transfer_status(transfer)
        payer = _circle_transfer_payer(transfer) or record.payer
        transaction = _circle_transfer_id(transfer) or record.transaction
        if status in {"confirmed", "completed"}:
            return ReconciliationOutcome(
                status=ReconciliationStatus.SETTLED,
                transaction=transaction,
                payer=payer,
            )
        if status in {"received", "batched"}:
            return ReconciliationOutcome(
                status=ReconciliationStatus.PENDING,
                transaction=transaction,
                payer=payer,
                error_reason="circle_gateway_transfer_pending",
            )
        if status == "failed":
            return ReconciliationOutcome(
                status=ReconciliationStatus.FAILED_FINAL,
                transaction=transaction,
                payer=payer,
                error_reason="circle_gateway_transfer_failed",
            )
        return ReconciliationOutcome(
            status=ReconciliationStatus.UNKNOWN,
            transaction=transaction,
            payer=payer,
            error_reason="circle_gateway_transfer_status_unknown",
        )


def hosted_circle_gateway_enabled_from_env() -> bool:
    raw = os.getenv(HOSTED_CIRCLE_GATEWAY_ENABLED_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def create_hosted_circle_gateway_provider_from_env() -> HostedCircleGatewayProvider | None:
    if not hosted_circle_gateway_enabled_from_env():
        return None
    client = NanopaymentClient(
        environment=os.getenv(HOSTED_CIRCLE_GATEWAY_ENVIRONMENT_ENV, "testnet").strip()
        or "testnet",
        base_url=os.getenv(HOSTED_CIRCLE_GATEWAY_BASE_URL_ENV, "").strip() or None,
        timeout=_circle_timeout_from_env(),
    )
    return HostedCircleGatewayProvider(
        client,
        networks=_circle_networks_from_env(),
    )


def _circle_payment_objects(
    envelope: FacilitatorEnvelope,
) -> tuple[PaymentPayload, PaymentRequirements]:
    requirement = _gateway_requirement_kind(envelope.payment_requirements)
    payload = _gateway_payment_payload(envelope)
    if payload.accepted is None:
        payload = replace(payload, accepted=requirement)
    requirements = PaymentRequirements(
        x402_version=envelope.x402_version,
        accepts=(requirement,),
    )
    return payload, requirements


def _gateway_payment_payload(envelope: FacilitatorEnvelope) -> PaymentPayload:
    try:
        return PaymentPayload.from_dict(envelope.payment_payload)
    except Exception as exc:
        raise ProtocolValidationError("Invalid Circle Gateway paymentPayload") from exc


def _gateway_requirement_kind(requirements: dict[str, Any]) -> PaymentRequirementsKind:
    if not _is_gateway_kind(requirements):
        raise ProtocolValidationError(
            "Circle Gateway payments require paymentRequirements.extra.name="
            f"{_GATEWAY_BATCHED_NAME}"
        )
    try:
        return PaymentRequirementsKind.from_dict(requirements)
    except Exception as exc:
        raise ProtocolValidationError("Invalid Circle Gateway paymentRequirements") from exc


def _is_gateway_kind(data: dict[str, Any]) -> bool:
    extra = data.get("extra")
    return isinstance(extra, dict) and extra.get("name") == _GATEWAY_BATCHED_NAME


def _has_gateway_metadata(data: dict[str, Any]) -> bool:
    extra = data.get("extra")
    return (
        isinstance(extra, dict)
        and _safe_nonempty_string(data.get("network"))
        and _safe_nonempty_string(extra.get("verifyingContract"))
        and _has_gateway_asset(extra)
    )


def _has_gateway_asset(extra: dict[str, Any]) -> bool:
    assets = extra.get("assets")
    if isinstance(assets, list | tuple):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if asset.get("symbol") == "USDC" and _safe_nonempty_string(asset.get("address")):
                return True
    asset = extra.get("asset")
    return isinstance(asset, dict) and _safe_nonempty_string(asset.get("address"))


def _circle_error_reason(exc: NanopaymentError) -> str:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return _prefixed_circle_reason(reason)
    details = getattr(exc, "details", {})
    if isinstance(details, dict):
        detail_reason = details.get("reason")
        if isinstance(detail_reason, str) and detail_reason:
            return _prefixed_circle_reason(detail_reason)
    return "circle_gateway_settlement_failed"


def _prefixed_circle_reason(reason: str) -> str:
    normalized = reason.strip().lower()
    if normalized in _CIRCLE_ERROR_REASON_ALLOWLIST:
        return f"circle_gateway_{normalized}"
    return "circle_gateway_settlement_failed"


def _is_ambiguous_settlement_error(exc: SettlementError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, str) and reason.strip().lower() in _AMBIGUOUS_SETTLEMENT_REASONS


def _safe_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _circle_transfer_status(transfer: dict[str, Any]) -> str:
    status = transfer.get("status") or transfer.get("state")
    if isinstance(status, str):
        return status.strip().lower()
    return ""


def _circle_transfer_id(transfer: dict[str, Any]) -> str | None:
    for key in ("id", "transferId", "transaction"):
        value = transfer.get(key)
        if _safe_nonempty_string(value):
            return value
    return None


def _circle_transfer_payer(transfer: dict[str, Any]) -> str | None:
    for key in ("fromAddress", "payer"):
        value = transfer.get(key)
        if _safe_nonempty_string(value):
            return value
    return None


def _circle_networks_from_env() -> tuple[str, ...]:
    raw = os.getenv(HOSTED_CIRCLE_GATEWAY_NETWORKS_ENV, "").strip()
    if not raw:
        return ()
    networks = tuple(part.strip() for part in raw.split(",") if part.strip())
    for network in networks:
        if not _EIP155_NETWORK_RE.fullmatch(network):
            raise RuntimeError(f"{HOSTED_CIRCLE_GATEWAY_NETWORKS_ENV} contains invalid network")
    return networks


def _circle_timeout_from_env() -> float:
    raw = os.getenv(HOSTED_CIRCLE_GATEWAY_TIMEOUT_ENV, "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{HOSTED_CIRCLE_GATEWAY_TIMEOUT_ENV} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{HOSTED_CIRCLE_GATEWAY_TIMEOUT_ENV} must be positive")
    return value
