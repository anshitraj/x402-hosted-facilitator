from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from x402.schemas import PaymentPayload, PaymentRequirements

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
from hosted_facilitator.hosted.signers import HostedSignerGuard
from hosted_facilitator.hosted.storage import SettlementRecord, SettlementStatus

_ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class HostedExactProvider(HostedFacilitatorProvider):
    name = "exact_evm"

    def __init__(
        self,
        exact_facilitator: Any | Mapping[str, Any],
        *,
        receipt_checker: Callable[[str, SettlementRecord], Any] | None = None,
        signer_guard: HostedSignerGuard | None = None,
        networks: tuple[str, ...] = (),
        timeout_seconds: float = 30.0,
        max_concurrent_settlements: int = 1,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrent_settlements <= 0:
            raise ValueError("max_concurrent_settlements must be positive")
        self._receipt_checker = receipt_checker
        self._exact_facilitators = _facilitator_map(exact_facilitator, networks)
        self.supported_networks = tuple(self._exact_facilitators)
        self._signer_guard = signer_guard
        self._timeout_seconds = timeout_seconds
        self._max_concurrent_settlements = max_concurrent_settlements
        self._settlement_semaphore = asyncio.Semaphore(max_concurrent_settlements)

    def _facilitator_for_network(self, network: str | None) -> Any:
        if network and network in self._exact_facilitators:
            return self._exact_facilitators[network]
        if network is None and len(self._exact_facilitators) == 1:
            return next(iter(self._exact_facilitators.values()))
        raise LookupError(f"No exact EVM facilitator configured for {network or 'unknown'}")

    def matches(self, *, scheme: str | None, network: str | None, extra: dict) -> bool:
        if extra.get("name") == "GatewayWalletBatched":
            return False
        return scheme == "exact" and (
            not self.supported_networks or network in self.supported_networks
        )

    async def supported(self, context: RequestContext) -> list[dict]:
        kinds: list[dict] = []
        for network, facilitator in self._exact_facilitators.items():
            result = facilitator.get_supported()
            data = _dump(result)
            if isinstance(data, dict) and isinstance(data.get("kinds"), list):
                kinds.extend(data["kinds"])
            elif isinstance(data, list):
                kinds.extend(data)
            else:
                kinds.append({"x402Version": 2, "scheme": "exact", "network": network})
        return kinds

    def validate_envelope(self, envelope: FacilitatorEnvelope) -> None:
        PaymentPayload.model_validate(envelope.payment_payload)
        PaymentRequirements.model_validate(envelope.payment_requirements)

    async def preflight_settlement(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> None:
        requirements = PaymentRequirements.model_validate(envelope.payment_requirements)
        if self._signer_guard is not None:
            await self._signer_guard.preflight_settlement(
                balance_checker=self._balance_checker_for_network(requirements.network)
            )

    async def health_check(self) -> dict[str, Any]:
        unhealthy = 0
        first_unhealthy_network = None
        first_error_type = None
        first_signer_health = None
        for network in self.supported_networks:
            if self._signer_guard is None:
                continue
            health = await self._signer_guard.health_check(
                balance_checker=self._balance_checker_for_network(network)
            )
            if first_signer_health is None:
                first_signer_health = health
            if health.get("status") != "ok":
                unhealthy += 1
                if first_unhealthy_network is None:
                    first_unhealthy_network = network
                    first_error_type = health.get("errorType")
        result = {
            "status": "ok" if unhealthy == 0 else "unhealthy",
            "networkCount": len(self.supported_networks),
            "supportedNetworks": tuple(self.supported_networks),
            "unhealthyNetworkCount": unhealthy,
            "maxConcurrentSettlements": self._max_concurrent_settlements,
            "signerLockScope": "network" if self._signer_guard is not None else "none",
        }
        if first_unhealthy_network is not None:
            result["network"] = first_unhealthy_network
        if first_error_type is not None:
            result["errorType"] = first_error_type
        if first_signer_health is not None:
            for key in ("signerStatus", "balanceWei", "minBalanceWei", "environment"):
                if key in first_signer_health:
                    result[key] = first_signer_health[key]
        return result

    def _balance_checker_for_network(self, network: str | None) -> Callable[[], int]:
        return lambda: _lookup_exact_signer_balance_wei(self._facilitator_for_network(network))

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        payload = PaymentPayload.model_validate(envelope.payment_payload)
        requirements = PaymentRequirements.model_validate(envelope.payment_requirements)
        facilitator = self._facilitator_for_network(requirements.network)
        try:
            result = await self._await_provider_call(
                facilitator.verify(payload, requirements),
                operation="verify",
            )
        except TimeoutError as exc:
            raise HostedProviderUnavailableError("exact_evm_verify_timeout") from exc
        data = _dump(result)
        return VerifyOutcome(
            isValid=bool(data.get("isValid", data.get("is_valid", False))),
            payer=data.get("payer"),
            invalidReason=data.get("invalidReason") or data.get("invalid_reason"),
        )

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        payload = PaymentPayload.model_validate(envelope.payment_payload)
        requirements = PaymentRequirements.model_validate(envelope.payment_requirements)
        facilitator = self._facilitator_for_network(requirements.network)
        try:
            async with self._settlement_slot(network=requirements.network):
                result = await self._await_provider_call(
                    facilitator.settle(payload, requirements),
                    operation="settle",
                )
        except TimeoutError:
            return SettleOutcome(
                success=False,
                network=envelope.payment_requirements.get("network"),
                payer=None,
                errorReason="provider_timeout",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
            )
        data = _dump(result)
        success_value = data.get("success")
        if success_value is True:
            settlement_status = ProviderSettlementStatus.SETTLED
            error_reason = None
        elif success_value is False:
            error_reason = data.get("errorReason") or data.get("error_reason")
            settlement_status = _settlement_status_for_failure(error_reason)
        else:
            settlement_status = ProviderSettlementStatus.UNKNOWN
            error_reason = "malformed_provider_settlement_response"
        return SettleOutcome(
            success=settlement_status == ProviderSettlementStatus.SETTLED,
            transaction=data.get("transaction"),
            network=data.get("network") or envelope.payment_requirements.get("network"),
            payer=data.get("payer"),
            errorReason=error_reason,
            settlementStatus=settlement_status,
        )

    @asynccontextmanager
    async def _settlement_slot(self, *, network: str | None):
        async with self._settlement_semaphore:
            if self._signer_guard is None:
                yield
                return
            async with self._signer_guard.settlement_slot(network=network):
                yield

    async def reconcile_settlement(self, record: SettlementRecord) -> ReconciliationOutcome:
        exact_facilitator = self._facilitator_for_network(record.network)
        delegated = getattr(exact_facilitator, "reconcile_settlement", None)
        if delegated is not None:
            try:
                outcome = await self._await_provider_call(
                    delegated(record),
                    operation="reconcile",
                )
            except TimeoutError:
                return ReconciliationOutcome(
                    status=ReconciliationStatus.PENDING,
                    transaction=record.transaction,
                    payer=record.payer,
                    error_reason="exact_evm_reconciliation_timeout",
                )
            return await self._validate_reconciliation_outcome(
                _coerce_reconciliation_outcome(outcome),
                record,
            )

        transaction = _normalize_optional_tx_hash(record.transaction)
        return await self._reconcile_transaction(record, transaction)

    async def _await_provider_call(self, value: Any, *, operation: str) -> Any:
        del operation
        if not inspect.isawaitable(value):
            return value
        try:
            return await asyncio.wait_for(value, timeout=self._timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("exact_evm_provider_timeout") from exc

    async def _validate_reconciliation_outcome(
        self,
        outcome: ReconciliationOutcome,
        record: SettlementRecord,
    ) -> ReconciliationOutcome:
        if outcome.status != ReconciliationStatus.SETTLED:
            return outcome

        transaction = _normalize_optional_tx_hash(outcome.transaction or record.transaction)
        proof_outcome = await self._reconcile_transaction(record, transaction)
        if proof_outcome.status != ReconciliationStatus.SETTLED:
            return proof_outcome
        return ReconciliationOutcome(
            status=ReconciliationStatus.SETTLED,
            transaction=transaction,
            payer=outcome.payer or record.payer,
        )

    async def _reconcile_transaction(
        self,
        record: SettlementRecord,
        transaction: str | None,
    ) -> ReconciliationOutcome:
        if transaction is None:
            return ReconciliationOutcome(
                status=ReconciliationStatus.PENDING,
                error_reason="exact_evm_transaction_unavailable",
            )

        receipt = await self._lookup_receipt(transaction, record)
        if receipt is None:
            return ReconciliationOutcome(
                status=ReconciliationStatus.PENDING,
                transaction=transaction,
                payer=record.payer,
                error_reason="exact_evm_receipt_not_found",
            )

        if not _receipt_transaction_matches(receipt, transaction):
            return ReconciliationOutcome(
                status=ReconciliationStatus.PENDING,
                transaction=transaction,
                payer=record.payer,
                error_reason="exact_evm_receipt_transaction_mismatch",
            )

        status = _receipt_status(receipt)
        if status == 1:
            if not _receipt_contains_exact_transfer(receipt, record):
                return ReconciliationOutcome(
                    status=ReconciliationStatus.PENDING,
                    transaction=transaction,
                    payer=record.payer,
                    error_reason="exact_evm_receipt_proof_unavailable",
                )
            return ReconciliationOutcome(
                status=ReconciliationStatus.SETTLED,
                transaction=transaction,
                payer=record.payer,
            )
        if status == 0:
            if record.status == SettlementStatus.SUBMITTED:
                return ReconciliationOutcome(
                    status=ReconciliationStatus.UNKNOWN,
                    transaction=transaction,
                    payer=record.payer,
                    error_reason="exact_evm_transaction_failed",
                )
            return ReconciliationOutcome(
                status=ReconciliationStatus.FAILED_FINAL,
                transaction=transaction,
                payer=record.payer,
                error_reason="exact_evm_transaction_failed",
            )
        return ReconciliationOutcome(
            status=ReconciliationStatus.PENDING,
            transaction=transaction,
            payer=record.payer,
            error_reason="exact_evm_receipt_status_unknown",
        )

    async def _lookup_receipt(self, transaction: str, record: SettlementRecord) -> Any | None:
        if self._receipt_checker is not None:
            receipt = self._receipt_checker(transaction, record)
            if inspect.isawaitable(receipt):
                receipt = await self._await_provider_call(receipt, operation="receipt")
            return receipt

        exact_facilitator = self._facilitator_for_network(record.network)
        direct_lookup = getattr(exact_facilitator, "get_transaction_receipt", None)
        if direct_lookup is not None:
            try:
                return await self._call_receipt_lookup(direct_lookup, transaction)
            except Exception as exc:
                if _is_transaction_not_found(exc):
                    return None
                raise

        signer = _find_exact_signer(exact_facilitator, record)
        if signer is None:
            return None

        web3_client = getattr(signer, "_w3", None)
        eth_client = getattr(web3_client, "eth", None)
        get_receipt = getattr(eth_client, "get_transaction_receipt", None)
        if get_receipt is not None:
            try:
                return await self._call_receipt_lookup(get_receipt, transaction)
            except Exception as exc:
                if _is_transaction_not_found(exc):
                    return None
                raise

        return None

    async def _call_receipt_lookup(self, lookup: Callable[[str], Any], transaction: str) -> Any:
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(lookup, transaction),
                timeout=self._timeout_seconds,
            )
            if inspect.isawaitable(value):
                return await self._await_provider_call(value, operation="receipt")
            return value
        except asyncio.TimeoutError as exc:
            raise TimeoutError("exact_evm_receipt_lookup_timeout") from exc


def _dump(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump(by_alias=True, exclude_none=True)
    return result


def _facilitator_map(
    exact_facilitator: Any | Mapping[str, Any],
    networks: tuple[str, ...],
) -> dict[str, Any]:
    if isinstance(exact_facilitator, Mapping):
        mapped = {str(network): facilitator for network, facilitator in exact_facilitator.items()}
        if not mapped:
            raise ValueError("At least one exact EVM network facilitator is required")
        return mapped
    if not networks:
        networks = ("eip155:5042002",)
    return dict.fromkeys(networks, exact_facilitator)


def _coerce_reconciliation_outcome(outcome: Any) -> ReconciliationOutcome:
    if isinstance(outcome, ReconciliationOutcome):
        return outcome
    data = _dump(outcome)
    if isinstance(data, dict):
        return ReconciliationOutcome(
            status=ReconciliationStatus(data["status"]),
            transaction=data.get("transaction"),
            payer=data.get("payer"),
            error_reason=data.get("errorReason") or data.get("error_reason"),
        )
    raise TypeError("Exact EVM reconciliation hook returned an unsupported outcome")


def _normalize_optional_tx_hash(transaction: str | None) -> str | None:
    if transaction is None:
        return None
    value = transaction.strip()
    if not value:
        return None
    normalized = value if value.startswith("0x") else f"0x{value}"
    return normalized.lower()


def _receipt_status(receipt: Any) -> int | None:
    status = None
    if isinstance(receipt, dict):
        status = receipt.get("status")
    elif hasattr(receipt, "status"):
        status = receipt.status
    elif hasattr(receipt, "__getitem__"):
        try:
            status = receipt["status"]
        except Exception:
            status = None

    if status is True:
        return 1
    if status is False:
        return 0
    if isinstance(status, str) and status.lower().startswith("0x"):
        try:
            return int(status, 16)
        except ValueError:
            return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _receipt_transaction_matches(receipt: Any, transaction: str) -> bool:
    receipt_hash = _get_field(receipt, "transactionHash") or _get_field(receipt, "transaction_hash")
    if receipt_hash is None:
        return True
    return _normalize_optional_tx_hash(_hex_value(receipt_hash)) == transaction.lower()


def _receipt_contains_exact_transfer(receipt: Any, record: SettlementRecord) -> bool:
    requirements = record.raw_requirements
    asset = _normalize_address(requirements.get("asset"))
    payer = _normalize_address(record.payer)
    pay_to = _normalize_address(requirements.get("payTo"))
    try:
        amount = int(str(requirements.get("amount")))
    except (TypeError, ValueError):
        return False
    if asset is None or payer is None or pay_to is None:
        return False

    for log in _receipt_logs(receipt):
        if _normalize_address(_get_field(log, "address")) != asset:
            continue
        topics = _get_field(log, "topics")
        if not isinstance(topics, (list, tuple)) or len(topics) < 3:
            continue
        if _hex_value(topics[0]).lower() != _ERC20_TRANSFER_TOPIC:
            continue
        if _topic_address(topics[1]) != payer:
            continue
        if _topic_address(topics[2]) != pay_to:
            continue
        if _log_data_int(_get_field(log, "data")) != amount:
            continue
        return True
    return False


def _receipt_logs(receipt: Any) -> list[Any]:
    logs = _get_field(receipt, "logs")
    return logs if isinstance(logs, list) else []


def _get_field(target: Any, name: str) -> Any:
    if isinstance(target, dict):
        return target.get(name)
    if hasattr(target, name):
        return getattr(target, name)
    if hasattr(target, "__getitem__"):
        try:
            return target[name]
        except Exception:
            return None
    return None


def _normalize_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    if not normalized.startswith("0x") or len(normalized) != 42:
        return None
    return normalized


def _topic_address(value: Any) -> str | None:
    raw = _hex_value(value).lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        return None
    return f"0x{raw[-40:]}"


def _log_data_int(value: Any) -> int | None:
    raw = _hex_value(value)
    try:
        if raw.startswith("0x"):
            return int(raw, 16)
        return int(raw)
    except (TypeError, ValueError):
        return None


def _hex_value(value: Any) -> str:
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    if hasattr(value, "hex"):
        hex_value = value.hex()
        return hex_value if str(hex_value).startswith("0x") else f"0x{hex_value}"
    return str(value)


def _find_exact_signer(exact_facilitator: Any, record: SettlementRecord) -> Any | None:
    if hasattr(exact_facilitator, "_signer"):
        return exact_facilitator._signer

    find_facilitator = getattr(exact_facilitator, "_find_facilitator", None)
    if find_facilitator is not None and record.network:
        scheme = find_facilitator(record.scheme or "exact", record.network)
        signer = getattr(scheme, "_signer", None)
        if signer is not None:
            return signer

    for scheme_data in getattr(exact_facilitator, "_schemes", ()):
        scheme = getattr(scheme_data, "facilitator", None)
        if getattr(scheme, "scheme", None) != (record.scheme or "exact"):
            continue
        networks = getattr(scheme_data, "networks", ())
        if record.network and record.network not in networks:
            continue
        signer = getattr(scheme, "_signer", None)
        if signer is not None:
            return signer
    return None


def _find_any_exact_signer(exact_facilitator: Any) -> Any | None:
    if hasattr(exact_facilitator, "_signer"):
        return exact_facilitator._signer

    for scheme_data in getattr(exact_facilitator, "_schemes", ()):
        scheme = getattr(scheme_data, "facilitator", None)
        signer = getattr(scheme, "_signer", None)
        if signer is not None:
            return signer
    return None


def _lookup_exact_signer_balance_wei(exact_facilitator: Any) -> int:
    signer = _find_any_exact_signer(exact_facilitator)
    if signer is None:
        raise RuntimeError("exact signer unavailable")

    account = getattr(signer, "_account", None)
    address = getattr(account, "address", None)
    if not address:
        raise RuntimeError("exact signer address unavailable")

    web3_client = getattr(signer, "_w3", None)
    eth_client = getattr(web3_client, "eth", None)
    get_balance = getattr(eth_client, "get_balance", None)
    if get_balance is None:
        raise RuntimeError("exact signer balance checker unavailable")
    return int(get_balance(address))


def _is_transaction_not_found(exc: Exception) -> bool:
    class_name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return "transactionnotfound" in class_name or (
        "transaction" in message and "not found" in message
    )


def _settlement_status_for_failure(error_reason: str | None) -> ProviderSettlementStatus:
    if not error_reason:
        return ProviderSettlementStatus.UNKNOWN
    normalized = error_reason.lower()
    ambiguous_markers = (
        "timeout",
        "timed out",
        "rpc",
        "network",
        "transport",
        "connection",
        "unavailable",
        "unknown",
        "provider",
        "node",
        "rate limit",
        "malformed",
    )
    if any(marker in normalized for marker in ambiguous_markers):
        return ProviderSettlementStatus.UNKNOWN
    return ProviderSettlementStatus.FAILED_FINAL
