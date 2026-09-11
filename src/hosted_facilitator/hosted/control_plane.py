from __future__ import annotations

import hashlib
import re
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from inspect import isawaitable
from threading import RLock
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from hosted_facilitator.hosted.auth import OperationsPermission, Principal
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.observability import component_health
from hosted_facilitator.hosted.storage import SettlementStatus
from hosted_facilitator.hosted.telemetry import HostedTelemetryProtocol


class ControlPlaneTargetType(StrEnum):
    GLOBAL = "global"
    PROVIDER = "provider"
    NETWORK = "network"
    SELLER = "seller"
    SETTLEMENT = "settlement"


@dataclass(frozen=True)
class ControlPlaneAuditEvent:
    event_id: int
    action: str
    target_type: ControlPlaneTargetType
    target: str
    before: bool
    after: bool
    reason: str
    actor: str
    correlation_id: str
    created_at: datetime


@dataclass
class ControlPlanePauseState:
    global_paused: bool = False
    providers: set[str] = field(default_factory=set)
    networks: set[str] = field(default_factory=set)


_SECRET_SHAPED_REASON_RE = re.compile(
    r"(omck_[A-Za-z0-9_-]{8,}|omcr_[A-Za-z0-9_-]{8,}|0x[0-9a-fA-F]{64}|circle[_-]?api[_-]?key)",
    re.IGNORECASE,
)
_RECONCILIATION_QUEUE_STATUSES = (
    SettlementStatus.UNKNOWN,
    SettlementStatus.SUBMITTED,
    SettlementStatus.SETTLE_IN_PROGRESS,
    SettlementStatus.MANUAL_REVIEW,
)
_ACTIVE_RECONCILIATION_STATUSES = (
    SettlementStatus.SUBMITTED,
    SettlementStatus.SETTLE_IN_PROGRESS,
)
_OPERATOR_ACTIVE_CLAIM_STALE_SECONDS = 300


class ControlPlanePauseRequest(BaseModel):
    target_type: ControlPlaneTargetType = Field(alias="targetType")
    target: str | None = None
    paused: bool
    reason: str = Field(min_length=3, max_length=240)


class ControlPlaneSellerCreateRequest(BaseModel):
    seller_ref: str = Field(alias="sellerRef", min_length=3, max_length=64)
    tenant_ref: str = Field(alias="tenantRef", default="alpha", min_length=3, max_length=64)
    name: str | None = Field(default=None, max_length=120)
    environment: str = Field(default="testnet", max_length=24)
    enabled_networks: list[str] = Field(
        default_factory=lambda: ["eip155:5042002"],
        alias="enabledNetworks",
        min_length=1,
        max_length=12,
    )
    allowed_assets: list[str] = Field(
        default_factory=lambda: ["0x3600000000000000000000000000000000000000"],
        alias="allowedAssets",
        min_length=1,
        max_length=24,
    )
    allowed_pay_to: list[str] = Field(alias="allowedPayTo", min_length=1, max_length=24)


class ControlPlaneSellerApiKeyIssueRequest(BaseModel):
    payment_profile_id: str = Field(default="default", alias="paymentProfileId", max_length=64)


class ControlPlaneSellerApiKeyRevokeRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=240)


class ControlPlaneReconciliationActionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=240)
    lease_seconds: int = Field(default=300, alias="leaseSeconds", ge=30, le=3600)


class InMemoryControlPlaneState:
    durable = False
    hosted_safe = False
    writes_enabled = True

    def __init__(self):
        self._pause = ControlPlanePauseState()
        self._audit: list[ControlPlaneAuditEvent] = []
        self._next_event_id = 1
        self._lock = RLock()

    def pause_state(self) -> ControlPlanePauseState:
        with self._lock:
            return ControlPlanePauseState(
                global_paused=self._pause.global_paused,
                providers=set(self._pause.providers),
                networks=set(self._pause.networks),
            )

    def audit_tail(self, limit: int = 25) -> list[ControlPlaneAuditEvent]:
        with self._lock:
            return list(reversed(self._audit[-max(0, min(limit, 100)) :]))

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "count": len(self._audit)}

    def set_pause(
        self,
        *,
        target_type: ControlPlaneTargetType,
        target: str | None,
        paused: bool,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        normalized_target = _normalize_pause_target(target_type=target_type, target=target)
        with self._lock:
            before = self._is_paused_locked(target_type=target_type, target=normalized_target)
            if target_type == ControlPlaneTargetType.GLOBAL:
                self._pause.global_paused = paused
            elif target_type == ControlPlaneTargetType.PROVIDER:
                _set_membership(self._pause.providers, normalized_target, paused)
            elif target_type == ControlPlaneTargetType.NETWORK:
                _set_membership(self._pause.networks, normalized_target, paused)
            after = self._is_paused_locked(target_type=target_type, target=normalized_target)
            event = ControlPlaneAuditEvent(
                event_id=self._next_event_id,
                action="pause_set",
                target_type=target_type,
                target=normalized_target,
                before=before,
                after=after,
                reason=_safe_reason(reason),
                actor=_safe_actor(actor),
                correlation_id=_safe_correlation_id(correlation_id),
                created_at=datetime.now(timezone.utc),
            )
            self._next_event_id += 1
            self._audit.append(event)
            return event

    def record_seller_key_event(
        self,
        *,
        action: str,
        seller_account_id: str,
        key_prefix: str,
        reason: str | None = None,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        safe_action = _safe_seller_key_action(action)
        audit_reason = _seller_key_audit_reason(
            key_prefix=key_prefix,
            reason=reason if safe_action == "seller_api_key_revoke" else None,
        )
        with self._lock:
            event = ControlPlaneAuditEvent(
                event_id=self._next_event_id,
                action=safe_action,
                target_type=ControlPlaneTargetType.SELLER,
                target=_safe_seller_ref(seller_account_id),
                before=safe_action == "seller_api_key_revoke",
                after=safe_action == "seller_api_key_issue",
                reason=audit_reason,
                actor=_safe_actor(actor),
                correlation_id=_safe_correlation_id(correlation_id),
                created_at=datetime.now(timezone.utc),
            )
            self._next_event_id += 1
            self._audit.append(event)
            return event

    def record_seller_create(
        self,
        *,
        seller_account_id: str,
        key_prefix: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        with self._lock:
            event = ControlPlaneAuditEvent(
                event_id=self._next_event_id,
                action="seller_create",
                target_type=ControlPlaneTargetType.SELLER,
                target=_safe_seller_ref(seller_account_id),
                before=False,
                after=True,
                reason=f"api_key_prefix:{_safe_key_prefix(key_prefix)}",
                actor=_safe_actor(actor),
                correlation_id=_safe_correlation_id(correlation_id),
                created_at=datetime.now(timezone.utc),
            )
            self._next_event_id += 1
            self._audit.append(event)
            return event

    def record_reconciliation_event(
        self,
        *,
        action: str,
        record_id: int,
        before: bool,
        after: bool,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        safe_action = _safe_reconciliation_action(action)
        with self._lock:
            event = ControlPlaneAuditEvent(
                event_id=self._next_event_id,
                action=safe_action,
                target_type=ControlPlaneTargetType.SETTLEMENT,
                target=str(_safe_record_id(str(record_id))),
                before=bool(before),
                after=bool(after),
                reason=_safe_reason(reason),
                actor=_safe_actor(actor),
                correlation_id=_safe_correlation_id(correlation_id),
                created_at=datetime.now(timezone.utc),
            )
            self._next_event_id += 1
            self._audit.append(event)
            return event

    def record_external_audit_event(self, event: ControlPlaneAuditEvent) -> ControlPlaneAuditEvent:
        with self._lock:
            mirrored = ControlPlaneAuditEvent(
                event_id=self._next_event_id,
                action=event.action,
                target_type=event.target_type,
                target=event.target,
                before=event.before,
                after=event.after,
                reason=event.reason,
                actor=event.actor,
                correlation_id=event.correlation_id,
                created_at=event.created_at,
            )
            self._next_event_id += 1
            self._audit.append(mirrored)
            return mirrored

    def allows(self, *, provider: str, network: str | None) -> bool:
        with self._lock:
            if self._pause.global_paused:
                return False
            if _safe_provider_name(provider) in self._pause.providers:
                return False
            safe_network = _safe_network_or_none(network)
            return not (safe_network and safe_network in self._pause.networks)

    def _is_paused_locked(
        self,
        *,
        target_type: ControlPlaneTargetType,
        target: str,
    ) -> bool:
        if target_type == ControlPlaneTargetType.GLOBAL:
            return self._pause.global_paused
        if target_type == ControlPlaneTargetType.PROVIDER:
            return target in self._pause.providers
        if target_type == ControlPlaneTargetType.NETWORK:
            return target in self._pause.networks
        return False


class DisabledControlPlaneState:
    durable = True
    hosted_safe = True
    writes_enabled = False

    def pause_state(self) -> ControlPlanePauseState:
        return ControlPlanePauseState()

    def audit_tail(self, limit: int = 25) -> list[ControlPlaneAuditEvent]:
        return []

    def health_check(self) -> dict[str, Any]:
        return {"status": "ok"}

    def allows(self, *, provider: str, network: str | None) -> bool:
        return True

    def set_pause(self, **_kwargs) -> ControlPlaneAuditEvent:
        raise RuntimeError("control-plane writes are disabled")

    def record_seller_create(self, **_kwargs) -> ControlPlaneAuditEvent:
        raise RuntimeError("control-plane writes are disabled")

    def record_seller_key_event(self, **_kwargs) -> ControlPlaneAuditEvent:
        raise RuntimeError("control-plane writes are disabled")

    def record_reconciliation_event(self, **_kwargs) -> ControlPlaneAuditEvent:
        raise RuntimeError("control-plane writes are disabled")


def register_control_plane_routes(
    app: FastAPI,
    *,
    engine: HostedFacilitatorEngine,
    store_factory: Callable[[], Any] | None,
    rate_limiter: Any,
    telemetry: HostedTelemetryProtocol,
    control_state: Any,
    seller_account_resolver: Any,
    authorize_request: Callable[[Request, OperationsPermission], Awaitable[Principal] | Principal],
) -> None:
    async def control_plane_overview(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.overview",
            {"omniclaw.endpoint": "ops_overview", "omniclaw.correlation_id": correlation_id},
        ):
            try:
                await _authorize_control_plane_request(request, OperationsPermission.OPS_OVERVIEW)
                overview = await build_control_plane_overview(
                    engine=engine,
                    store_factory=store_factory,
                    rate_limiter=rate_limiter,
                    telemetry=telemetry,
                    control_state=control_state,
                )
                status_code = 200 if overview["status"] == "ok" else 503
                if status_code != 200:
                    return JSONResponse(status_code=status_code, content=overview)
                return overview
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_overview",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def set_pause(request: Request, payload: ControlPlanePauseRequest):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.pause",
            {
                "omniclaw.endpoint": "ops_pauses",
                "omniclaw.correlation_id": correlation_id,
                "action": "pause_set",
            },
        ):
            try:
                principal = await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_PAUSE,
                )
                if not getattr(control_state, "writes_enabled", False):
                    raise HTTPException(status_code=503, detail="Control-plane writes are disabled")
                _validate_pause_request(engine, payload)
                event = control_state.set_pause(
                    target_type=payload.target_type,
                    target=payload.target,
                    paused=payload.paused,
                    reason=payload.reason,
                    actor=_actor_from_principal_or_request(principal, request),
                    correlation_id=correlation_id,
                )
                status_code = 200
                _record_control_plane_change(telemetry, action="pause_set", status="ok")
                _log_control_plane_change(
                    telemetry,
                    status="ok",
                    correlation_id=event.correlation_id,
                    action=event.action,
                )
                return {
                    "event": _audit_event_json(event),
                    "pauseState": _pause_state_json(control_state),
                }
            except HTTPException as exc:
                status_code = exc.status_code
                _record_control_plane_change(telemetry, action="pause_set", status="denied")
                _log_control_plane_change(
                    telemetry,
                    status="denied",
                    correlation_id=correlation_id,
                    action="pause_set",
                )
                raise
            except ValueError as exc:
                status_code = 400
                _record_control_plane_change(telemetry, action="pause_set", status="denied")
                _log_control_plane_change(
                    telemetry,
                    status="denied",
                    correlation_id=correlation_id,
                    action="pause_set",
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_pauses",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def create_seller_account(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.sellers.create",
            {
                "omniclaw.endpoint": "ops_sellers",
                "omniclaw.correlation_id": correlation_id,
                "action": "seller_create",
            },
        ):
            try:
                principal = await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SELLERS,
                )
                try:
                    payload = ControlPlaneSellerCreateRequest.model_validate(await request.json())
                except ValidationError as exc:
                    raise ValueError("Invalid seller onboarding payload") from exc
                except Exception as exc:
                    raise ValueError("Invalid seller onboarding payload") from exc
                if not getattr(control_state, "writes_enabled", False):
                    raise HTTPException(status_code=503, detail="Control-plane writes are disabled")
                if not _seller_account_admin_available(seller_account_resolver):
                    raise HTTPException(
                        status_code=503, detail="Seller administration is unavailable"
                    )
                seller_account_id = _safe_seller_ref(payload.seller_ref)
                existing = _get_existing_seller_account(seller_account_resolver, seller_account_id)
                if existing is not None:
                    raise HTTPException(status_code=409, detail="Seller account already exists")
                seller_account_kwargs = {
                    "seller_account_id": seller_account_id,
                    "tenant_id": _safe_seller_ref(payload.tenant_ref),
                    "name": _safe_seller_name(payload.name, seller_account_id),
                    "environment": _safe_seller_environment(payload.environment),
                    "enabled_networks": tuple(
                        _safe_network(network) for network in payload.enabled_networks
                    ),
                    "enabled_schemes": ("exact",),
                    "enabled_providers": ("exact_evm", "circle_gateway"),
                    "allowed_assets": tuple(
                        _safe_evm_address(value) for value in payload.allowed_assets
                    ),
                    "allowed_pay_to": tuple(
                        _safe_evm_address(value) for value in payload.allowed_pay_to
                    ),
                }
                actor = _actor_from_principal_or_request(principal, request)
                create_with_key = getattr(
                    seller_account_resolver, "create_seller_account_with_api_key", None
                )
                if not callable(create_with_key):
                    raise HTTPException(
                        status_code=503,
                        detail="Atomic seller administration is unavailable",
                    )
                result = create_with_key(
                    **seller_account_kwargs,
                    audit_actor=actor,
                    audit_correlation_id=correlation_id,
                )
                if len(result) != 3:
                    raise RuntimeError("Atomic seller audit is unavailable")
                seller_account, issued, audit_event = result
                _mirror_external_audit_event(control_state, audit_event)
                status_code = 201
                _record_control_plane_change(telemetry, action="seller_create", status="ok")
                _log_control_plane_change(
                    telemetry,
                    status="ok",
                    correlation_id=correlation_id,
                    action="seller_create",
                )
                return JSONResponse(
                    status_code=201,
                    headers={"Cache-Control": "no-store"},
                    content={
                        "seller": _seller_json(seller_account),
                        "apiKey": issued.key,
                        "apiKeyPrefix": issued.key_prefix,
                        "facilitatorPath": "/v2/x402",
                        "createdBy": _actor_from_principal_or_request(principal, request),
                        "auditEvent": _audit_event_json(audit_event) if audit_event else None,
                    },
                )
            except HTTPException as exc:
                status_code = exc.status_code
                _record_control_plane_change(telemetry, action="seller_create", status="denied")
                _log_control_plane_change(
                    telemetry,
                    status="denied",
                    correlation_id=correlation_id,
                    action="seller_create",
                )
                raise
            except ValueError as exc:
                status_code = 409 if "already exists" in str(exc).lower() else 400
                detail = str(exc)
                if "already exists" in detail:
                    detail = "Seller account already exists"
                _record_control_plane_change(telemetry, action="seller_create", status="denied")
                _log_control_plane_change(
                    telemetry,
                    status="denied",
                    correlation_id=correlation_id,
                    action="seller_create",
                )
                raise HTTPException(status_code=status_code, detail=detail) from exc
            except Exception as exc:
                status_code = 503
                _record_control_plane_change(telemetry, action="seller_create", status="failed")
                _log_control_plane_change(
                    telemetry,
                    status="failed",
                    correlation_id=correlation_id,
                    action="seller_create",
                )
                raise HTTPException(status_code=503, detail="Seller creation failed") from exc
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_sellers",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def list_seller_accounts(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.sellers.list",
            {
                "omniclaw.endpoint": "ops_sellers",
                "omniclaw.correlation_id": correlation_id,
            },
        ):
            try:
                await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SELLERS,
                )
                sellers = _list_seller_accounts(seller_account_resolver)
                status_code = 200
                return {"sellers": sellers}
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_sellers",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def get_seller_account(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        seller_account_id = ""
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.sellers.detail",
            {
                "omniclaw.endpoint": "ops_sellers",
                "omniclaw.correlation_id": correlation_id,
            },
        ):
            store = None
            close_store = False
            try:
                seller_account_id = _safe_seller_ref(
                    str(request.path_params.get("seller_ref") or "")
                )
                await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SELLERS,
                )
                await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SETTLEMENTS,
                )
                store = store_factory() if store_factory is not None else engine.settlement_store
                close_store = store_factory is not None
                detail = _seller_admin_detail(
                    seller_account_resolver,
                    seller_account_id,
                    settlement_store=store,
                )
                if detail is None:
                    raise HTTPException(status_code=404, detail="Seller account not found")
                status_code = 200
                return detail
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            except ValueError as exc:
                status_code = 400
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                status_code = 503
                raise HTTPException(status_code=503, detail="Seller detail unavailable") from exc
            finally:
                if close_store and store is not None:
                    _close_store(store)
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_sellers",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def get_settlement_record(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        record_id = 0
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.settlements.detail",
            {
                "omniclaw.endpoint": "ops_settlements",
                "omniclaw.correlation_id": correlation_id,
                "settlement.record_id": record_id,
            },
        ) as span:
            store = None
            close_store = False
            try:
                record_id = _safe_record_id(str(request.path_params.get("record_id") or ""))
                with suppress(Exception):
                    telemetry.set_attributes(span, {"settlement.record_id": record_id})
                await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SETTLEMENTS,
                )
                store = store_factory() if store_factory is not None else engine.settlement_store
                close_store = store_factory is not None
                detail = _settlement_detail(store, record_id)
                if detail is None:
                    raise HTTPException(status_code=404, detail="Settlement record not found")
                status_code = 200
                return JSONResponse(
                    content=detail,
                    headers={"Cache-Control": "no-store"},
                )
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            except ValueError as exc:
                status_code = 400
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                status_code = 503
                raise HTTPException(
                    status_code=503, detail="Settlement detail unavailable"
                ) from exc
            finally:
                if close_store and store is not None:
                    _close_store(store)
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_settlements",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def get_reconciliation_queue(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.reconciliation.queue",
            {
                "omniclaw.endpoint": "ops_reconciliation",
                "omniclaw.correlation_id": correlation_id,
            },
        ):
            store = None
            close_store = False
            try:
                await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SETTLEMENTS,
                )
                filters = _reconciliation_queue_filters(request)
                store = store_factory() if store_factory is not None else engine.settlement_store
                close_store = store_factory is not None
                status_code = 200
                return JSONResponse(
                    content=_reconciliation_queue(store, filters),
                    headers={"Cache-Control": "no-store"},
                )
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            except ValueError as exc:
                status_code = 400
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                status_code = 503
                raise HTTPException(
                    status_code=503, detail="Reconciliation queue unavailable"
                ) from exc
            finally:
                if close_store and store is not None:
                    _close_store(store)
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_reconciliation",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def claim_reconciliation_record(request: Request):
        return await _run_reconciliation_action(request, "reconciliation_claim")

    async def release_reconciliation_record(request: Request):
        return await _run_reconciliation_action(request, "reconciliation_release")

    async def mark_reconciliation_manual_review(request: Request):
        return await _run_reconciliation_action(request, "reconciliation_manual_review")

    async def _run_reconciliation_action(request: Request, action: str):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        record_id = 0
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.reconciliation.action",
            {
                "omniclaw.endpoint": "ops_reconciliation",
                "omniclaw.correlation_id": correlation_id,
                "action": action,
            },
        ) as span:
            store = None
            close_store = False
            try:
                record_id = _safe_record_id(str(request.path_params.get("record_id") or ""))
                with suppress(Exception):
                    telemetry.set_attributes(
                        span,
                        {"settlement.record_id": record_id, "action": action},
                    )
                principal = await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_MANUAL_REVIEW,
                )
                if not getattr(control_state, "writes_enabled", False):
                    raise HTTPException(status_code=503, detail="Control-plane writes are disabled")
                try:
                    payload = ControlPlaneReconciliationActionRequest.model_validate(
                        await request.json()
                    )
                except Exception as exc:
                    raise ValueError("Invalid reconciliation action payload") from exc
                store = store_factory() if store_factory is not None else engine.settlement_store
                close_store = store_factory is not None
                actor = _actor_from_principal_or_request(principal, request)
                owner = _lease_owner_from_principal_or_request(principal, request)
                audit_event = _apply_reconciliation_action(
                    store=store,
                    control_state=control_state,
                    action=action,
                    record_id=record_id,
                    actor=actor,
                    owner=owner,
                    correlation_id=correlation_id,
                    reason=payload.reason,
                    lease_seconds=payload.lease_seconds,
                )
                detail = _settlement_detail(store, record_id)
                if detail is None:
                    raise HTTPException(status_code=404, detail="Settlement record not found")
                status_code = 200
                _record_control_plane_change(telemetry, action=action, status="ok")
                return JSONResponse(
                    content={
                        **detail,
                        "auditEvent": _audit_event_json(audit_event) if audit_event else None,
                    },
                    headers={"Cache-Control": "no-store"},
                )
            except HTTPException as exc:
                status_code = exc.status_code
                _record_control_plane_change(telemetry, action=action, status="denied")
                raise
            except ValueError as exc:
                status_code = 400
                _record_control_plane_change(telemetry, action=action, status="denied")
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except KeyError as exc:
                status_code = 404
                _record_control_plane_change(telemetry, action=action, status="denied")
                raise HTTPException(status_code=404, detail="Settlement record not found") from exc
            except RuntimeError as exc:
                detail = str(exc)
                if "unavailable" in detail or "does not support" in detail:
                    status_code = 503
                    _record_control_plane_change(telemetry, action=action, status="failed")
                    raise HTTPException(status_code=503, detail=detail) from exc
                status_code = 409
                _record_control_plane_change(telemetry, action=action, status="denied")
                raise HTTPException(status_code=409, detail=detail) from exc
            except Exception as exc:
                status_code = 503
                _record_control_plane_change(telemetry, action=action, status="failed")
                raise HTTPException(status_code=503, detail="Reconciliation action failed") from exc
            finally:
                if close_store and store is not None:
                    _close_store(store)
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_reconciliation",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def issue_seller_api_key(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        seller_account_id = ""
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.sellers.api_keys.issue",
            {
                "omniclaw.endpoint": "ops_sellers",
                "omniclaw.correlation_id": correlation_id,
                "action": "seller_api_key_issue",
            },
        ):
            try:
                seller_account_id = _safe_seller_ref(
                    str(request.path_params.get("seller_ref") or "")
                )
                principal = await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SELLERS,
                )
                if not getattr(control_state, "writes_enabled", False):
                    raise HTTPException(status_code=503, detail="Control-plane writes are disabled")
                try:
                    payload = ControlPlaneSellerApiKeyIssueRequest.model_validate(
                        await request.json()
                    )
                except Exception as exc:
                    raise ValueError("Invalid seller API-key payload") from exc
                actor = _actor_from_principal_or_request(principal, request)
                issue_with_audit = getattr(
                    seller_account_resolver, "issue_api_key_with_audit", None
                )
                if not callable(issue_with_audit):
                    raise HTTPException(
                        status_code=503, detail="Atomic seller API-key issue is unavailable"
                    )
                issued, audit_event = issue_with_audit(
                    seller_account_id=seller_account_id,
                    payment_profile_id=_request_profile_id(payload.payment_profile_id),
                    actor=actor,
                    correlation_id=correlation_id,
                )
                _mirror_external_audit_event(control_state, audit_event)
                status_code = 201
                _record_control_plane_change(telemetry, action="seller_api_key_issue", status="ok")
                return JSONResponse(
                    status_code=201,
                    headers={"Cache-Control": "no-store"},
                    content={
                        "apiKey": issued.key,
                        "key": _issued_key_json(issued),
                        "auditEvent": _audit_event_json(audit_event) if audit_event else None,
                    },
                )
            except HTTPException as exc:
                status_code = exc.status_code
                _record_control_plane_change(
                    telemetry, action="seller_api_key_issue", status="denied"
                )
                raise
            except (KeyError, ValueError) as exc:
                status_code = 400
                _record_control_plane_change(
                    telemetry, action="seller_api_key_issue", status="denied"
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                status_code = 503
                _record_control_plane_change(
                    telemetry, action="seller_api_key_issue", status="failed"
                )
                raise HTTPException(status_code=503, detail="Seller API-key issue failed") from exc
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_sellers",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    async def revoke_seller_api_key(request: Request):
        started = time.perf_counter()
        status_code = 500
        correlation_id = _correlation_id_from_request(request)
        seller_account_id = ""
        key_id = ""
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.control_plane.sellers.api_keys.revoke",
            {
                "omniclaw.endpoint": "ops_sellers",
                "omniclaw.correlation_id": correlation_id,
                "action": "seller_api_key_revoke",
            },
        ):
            try:
                seller_account_id = _safe_seller_ref(
                    str(request.path_params.get("seller_ref") or "")
                )
                key_id = _safe_key_id(str(request.path_params.get("key_id") or ""))
                principal = await _authorize_control_plane_request(
                    request,
                    OperationsPermission.OPS_SELLERS,
                )
                if not getattr(control_state, "writes_enabled", False):
                    raise HTTPException(status_code=503, detail="Control-plane writes are disabled")
                try:
                    payload = ControlPlaneSellerApiKeyRevokeRequest.model_validate(
                        await request.json()
                    )
                except Exception as exc:
                    raise ValueError("Invalid seller API-key revoke payload") from exc
                actor = _actor_from_principal_or_request(principal, request)
                revoke_with_audit = getattr(
                    seller_account_resolver, "revoke_api_key_with_audit", None
                )
                if not callable(revoke_with_audit):
                    raise HTTPException(
                        status_code=503, detail="Atomic seller API-key revoke is unavailable"
                    )
                key_summary, audit_event = revoke_with_audit(
                    seller_account_id=seller_account_id,
                    key_id=key_id,
                    reason=_safe_revoke_reason_text(payload.reason),
                    actor=actor,
                    correlation_id=correlation_id,
                )
                _mirror_external_audit_event(control_state, audit_event)
                status_code = 200
                _record_control_plane_change(telemetry, action="seller_api_key_revoke", status="ok")
                return {
                    "key": key_summary,
                    "auditEvent": _audit_event_json(audit_event) if audit_event else None,
                }
            except HTTPException as exc:
                status_code = exc.status_code
                _record_control_plane_change(
                    telemetry, action="seller_api_key_revoke", status="denied"
                )
                raise
            except KeyError as exc:
                status_code = 404
                _record_control_plane_change(
                    telemetry, action="seller_api_key_revoke", status="denied"
                )
                raise HTTPException(status_code=404, detail="Seller API key not found") from exc
            except ValueError as exc:
                status_code = 400
                _record_control_plane_change(
                    telemetry, action="seller_api_key_revoke", status="denied"
                )
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception as exc:
                status_code = 503
                _record_control_plane_change(
                    telemetry, action="seller_api_key_revoke", status="failed"
                )
                raise HTTPException(status_code=503, detail="Seller API-key revoke failed") from exc
            finally:
                _record_control_plane_read(
                    telemetry,
                    endpoint="ops_sellers",
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                )

    app.add_api_route(
        "/ops/api/overview",
        control_plane_overview,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/pauses",
        set_pause,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/sellers",
        list_seller_accounts,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/sellers",
        create_seller_account,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/sellers/{seller_ref}",
        get_seller_account,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/settlements/{record_id}",
        get_settlement_record,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/reconciliation",
        get_reconciliation_queue,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/reconciliation/{record_id}/claim",
        claim_reconciliation_record,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/reconciliation/{record_id}/release",
        release_reconciliation_record,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/reconciliation/{record_id}/manual-review",
        mark_reconciliation_manual_review,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/sellers/{seller_ref}/api-keys",
        issue_seller_api_key,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/ops/api/sellers/{seller_ref}/api-keys/{key_id}/revoke",
        revoke_seller_api_key,
        methods=["POST"],
        include_in_schema=False,
    )

    async def _authorize_control_plane_request(
        request: Request,
        permission: OperationsPermission,
    ) -> Principal:
        result = authorize_request(request, permission)
        if isawaitable(result):
            result = await result
        return result


async def build_control_plane_overview(
    *,
    engine: HostedFacilitatorEngine,
    store_factory: Callable[[], Any] | None,
    rate_limiter: Any,
    telemetry: HostedTelemetryProtocol,
    control_state: Any,
) -> dict[str, Any]:
    store = None
    close_store = False
    try:
        store = store_factory() if store_factory is not None else engine.settlement_store
        close_store = store_factory is not None
        store_health = await component_health("settlement_store", store)
        settlement = _settlement_summary(store)
    except Exception as exc:
        store_health = {
            "name": "settlement_store",
            "status": "unhealthy",
            "errorType": type(exc).__name__,
        }
        settlement = _empty_settlement_summary()
    finally:
        if close_store and store is not None:
            _close_store(store)

    rate_limiter_health = await component_health("rate_limiter", rate_limiter)
    telemetry_health = await component_health("telemetry", telemetry)
    control_plane_health = await component_health("control_plane", control_state)
    provider_names = tuple(
        sorted(_safe_provider_name(name) for name in engine.router.provider_names())
    )
    provider_checks = []
    for provider_name in provider_names:
        provider = engine.router.provider_by_name(provider_name)
        if provider is not None:
            provider_checks.append(
                _safe_provider_health(await component_health(provider_name, provider))
            )
    supported_networks = _supported_networks_from_provider_checks(provider_checks)
    providers = {
        "name": "providers",
        "status": (
            "ok"
            if provider_names and all(item["status"] != "unhealthy" for item in provider_checks)
            else "unhealthy"
        ),
        "count": len(provider_names),
        "names": provider_names,
        "networks": supported_networks,
        "items": provider_checks,
    }
    components = {
        "settlementStore": store_health,
        "rateLimiter": rate_limiter_health,
        "telemetry": telemetry_health,
        "controlPlane": control_plane_health,
        "providers": {
            "name": "providers",
            "status": providers["status"],
            "count": providers["count"],
            "networks": supported_networks,
            "items": provider_checks,
        },
    }
    status = "ok" if all(item["status"] == "ok" for item in components.values()) else "unhealthy"
    return {
        "service": "omniclaw_hosted_facilitator",
        "status": status,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "components": components,
        "providers": providers,
        "settlement": settlement,
        "pauseState": _pause_state_json(control_state),
        "auditTail": [_audit_event_json(event) for event in _audit_tail(control_state)],
    }


def _settlement_summary(store: Any) -> dict[str, Any]:
    count = _safe_int_call(store, "count")
    attempt_count = _safe_int_call(store, "attempt_count")
    status_counts = _safe_status_counts(store)
    manual_review = status_counts.get(SettlementStatus.MANUAL_REVIEW.value, 0)
    unknown = status_counts.get(SettlementStatus.UNKNOWN.value, 0)
    submitted = status_counts.get(SettlementStatus.SUBMITTED.value, 0)
    return {
        "records": count,
        "attempts": attempt_count,
        "manualReviewBacklog": manual_review,
        "unknown": unknown,
        "submitted": submitted,
        "statusCounts": status_counts,
        "byProviderNetwork": _safe_provider_network_status_counts(store),
        "oldestAgeSeconds": _safe_oldest_age_seconds(store),
        "recentRecords": _safe_recent_settlement_records(store),
    }


def _empty_settlement_summary() -> dict[str, Any]:
    return {
        "records": 0,
        "attempts": 0,
        "manualReviewBacklog": 0,
        "unknown": 0,
        "submitted": 0,
        "statusCounts": {},
        "byProviderNetwork": [],
        "oldestAgeSeconds": {},
        "recentRecords": [],
    }


def _safe_provider_health(health: dict[str, Any]) -> dict[str, Any]:
    safe = dict(health)
    values = safe.get("supportedNetworks")
    networks: list[str] = []
    if isinstance(values, (list, tuple)):
        for value in values:
            network = _safe_network_or_none(str(value))
            if network:
                networks.append(network)
    if networks:
        safe["supportedNetworks"] = tuple(sorted(set(networks)))
    else:
        safe.pop("supportedNetworks", None)
    network = _safe_network_or_none(str(safe.get("network") or ""))
    if network:
        safe["network"] = network
    else:
        safe.pop("network", None)
    if "provider" in safe:
        safe["provider"] = _safe_provider_name(str(safe["provider"]))
    return safe


def _safe_provider_network_status_counts(store: Any) -> list[dict[str, Any]]:
    method = getattr(store, "settlement_provider_network_status_counts", None)
    if method is None:
        return []
    result = method()
    if not isinstance(result, list):
        return []
    rows: list[dict[str, Any]] = []
    allowed = {status.value for status in SettlementStatus}
    for item in result:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in allowed:
            continue
        network = _safe_network_or_none(str(item.get("network") or ""))
        rows.append(
            {
                "provider": _safe_provider_name(str(item.get("provider") or "")),
                "network": network or "",
                "status": status,
                "count": max(0, int(item.get("count") or 0)),
            }
        )
    return rows[:250]


def _safe_oldest_age_seconds(store: Any) -> dict[str, int]:
    method = getattr(store, "settlement_oldest_age_seconds", None)
    if method is None:
        return {}
    result = method()
    if not isinstance(result, dict):
        return {}
    allowed = {status.value for status in SettlementStatus}
    ages: dict[str, int] = {}
    for status, age in result.items():
        safe_status = str(status)
        if safe_status not in allowed:
            continue
        ages[safe_status] = max(0, int(age or 0))
    return ages


def _safe_recent_settlement_records(store: Any) -> list[dict[str, Any]]:
    method = getattr(store, "list_recent_records", None)
    if method is None:
        return []
    try:
        records = method(limit=25)
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    return [_settlement_record_json(record) for record in records[:25]]


def _settlement_detail(store: Any, record_id: int) -> dict[str, Any] | None:
    get_record = getattr(store, "get_record_by_id", None)
    if not callable(get_record):
        return None
    record = get_record(record_id)
    if record is None:
        return None
    list_attempts = getattr(store, "list_attempts_for_record", None)
    attempts = list_attempts(record_id, limit=50) if callable(list_attempts) else []
    return {
        "record": _settlement_record_detail_json(record),
        "attempts": [
            _settlement_attempt_json(
                attempt,
                network=_safe_network_or_none(str(getattr(record, "network", "") or "")) or "",
                provider=_safe_provider_name(str(getattr(record, "provider", "") or "")),
            )
            for attempt in attempts
        ],
    }


def _reconciliation_queue_filters(request: Request) -> dict[str, Any]:
    raw_status_values: list[str] = []
    for value in request.query_params.getlist("status"):
        raw_status_values.extend(part.strip() for part in value.split(",") if part.strip())
    if not raw_status_values or raw_status_values == ["all"]:
        statuses = _RECONCILIATION_QUEUE_STATUSES
    else:
        statuses = tuple(_parse_reconciliation_status(value) for value in raw_status_values)
    min_age_seconds = _safe_query_int(
        request.query_params.get("minAgeSeconds"),
        name="minAgeSeconds",
        default=0,
        minimum=0,
        maximum=604800,
    )
    limit = _safe_query_int(
        request.query_params.get("limit"),
        name="limit",
        default=50,
        minimum=1,
        maximum=100,
    )
    seller_ref = request.query_params.get("sellerRef")
    provider = request.query_params.get("provider")
    network = request.query_params.get("network")
    return {
        "statuses": statuses,
        "minAgeSeconds": min_age_seconds,
        "staleBefore": (
            datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds)
            if min_age_seconds > 0
            else None
        ),
        "sellerRef": _safe_seller_ref(seller_ref) if seller_ref else None,
        "provider": _safe_provider_name(provider) if provider else None,
        "network": _safe_network(network) if network else None,
        "limit": limit,
    }


def _reconciliation_queue(store: Any, filters: dict[str, Any]) -> dict[str, Any]:
    list_queue = getattr(store, "list_reconciliation_queue", None)
    if not callable(list_queue):
        raise RuntimeError("settlement store does not expose reconciliation queue")
    count_queue = getattr(store, "reconciliation_queue_status_counts", None)
    records = list_queue(
        statuses=filters["statuses"],
        stale_before=filters["staleBefore"],
        seller_account_id=filters["sellerRef"],
        provider=filters["provider"],
        network=filters["network"],
        limit=filters["limit"],
    )
    if not isinstance(records, list):
        records = []
    items = [_reconciliation_queue_record_json(record) for record in records[: filters["limit"]]]
    counts = (
        _safe_reconciliation_queue_counts(count_queue, filters)
        if callable(count_queue)
        else _status_counts_from_queue_items(items)
    )
    truncated = sum(counts.values()) > len(items)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "filters": {
            "statuses": tuple(status.value for status in filters["statuses"]),
            "minAgeSeconds": filters["minAgeSeconds"],
            "sellerRef": filters["sellerRef"] or "",
            "provider": filters["provider"] or "",
            "network": filters["network"] or "",
            "limit": filters["limit"],
        },
        "counts": counts,
        "items": items,
        "truncated": truncated,
    }


def _apply_reconciliation_action(
    *,
    store: Any,
    control_state: Any,
    action: str,
    record_id: int,
    actor: str,
    owner: str,
    correlation_id: str,
    reason: str,
    lease_seconds: int,
) -> ControlPlaneAuditEvent:
    safe_action = _safe_reconciliation_action(action)
    lease_until = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    active_stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=_OPERATOR_ACTIVE_CLAIM_STALE_SECONDS
    )
    atomic_action = getattr(store, "apply_reconciliation_control_action", None)
    if callable(atomic_action):
        _updated_record, audit_event = atomic_action(
            action=safe_action,
            record_id=record_id,
            owner=_safe_operator_lease_owner(owner),
            lease_until=lease_until,
            active_stale_before=active_stale_before,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        )
        return audit_event
    record = _get_settlement_record_or_error(store, record_id)
    before_active = bool(getattr(record, "reconciliation_owner", None))
    if safe_action == "reconciliation_claim":
        if (
            record.status in _ACTIVE_RECONCILIATION_STATUSES
            and getattr(record, "updated_at", datetime.now(timezone.utc)) > active_stale_before
        ):
            raise RuntimeError("Active settlement record is not stale enough to claim")
        claim_record = getattr(store, "claim_reconciliation_record", None)
        if not callable(claim_record):
            raise RuntimeError("Reconciliation claim is unavailable")
        claimed = claim_record(
            record_id=record_id,
            owner=_safe_operator_lease_owner(owner),
            lease_until=lease_until,
            eligible_statuses=_RECONCILIATION_QUEUE_STATUSES,
            stale_before=None,
        )
        if claimed is None:
            raise RuntimeError("Settlement record is not claimable")
        return _record_reconciliation_control_event(
            control_state=control_state,
            action=safe_action,
            record_id=record_id,
            before=before_active,
            after=True,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        )
    if safe_action == "reconciliation_release":
        release_record = getattr(store, "release_reconciliation_record", None)
        if not callable(release_record):
            raise RuntimeError("Reconciliation release is unavailable")
        released = release_record(record_id=record_id, owner=_safe_operator_lease_owner(owner))
        if released is None:
            raise RuntimeError("Settlement record is not leased by this operator")
        return _record_reconciliation_control_event(
            control_state=control_state,
            action=safe_action,
            record_id=record_id,
            before=before_active,
            after=False,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        )
    if safe_action == "reconciliation_manual_review":
        if record.status != SettlementStatus.UNKNOWN:
            raise RuntimeError("Only unknown settlement records can move to manual review")
        if str(getattr(record, "reconciliation_owner", "") or "") != _safe_operator_lease_owner(
            owner
        ):
            raise RuntimeError("Settlement record must be claimed by this operator")
        mark_manual_review = getattr(store, "mark_manual_review", None)
        if not callable(mark_manual_review):
            raise RuntimeError("Manual review transition is unavailable")
        mark_manual_review(
            record,
            error_reason="manual_review_operator_requested",
            owner=_safe_operator_lease_owner(owner),
        )
        return _record_reconciliation_control_event(
            control_state=control_state,
            action=safe_action,
            record_id=record_id,
            before=before_active,
            after=True,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        )
    raise RuntimeError("Unsupported reconciliation action")


def _get_settlement_record_or_error(store: Any, record_id: int) -> Any:
    get_record = getattr(store, "get_record_by_id", None)
    if not callable(get_record):
        raise RuntimeError("Settlement detail is unavailable")
    record = get_record(record_id)
    if record is None:
        raise KeyError(record_id)
    return record


def _record_reconciliation_control_event(
    *,
    control_state: Any,
    action: str,
    record_id: int,
    before: bool,
    after: bool,
    reason: str,
    actor: str,
    correlation_id: str,
) -> ControlPlaneAuditEvent:
    record_event = getattr(control_state, "record_reconciliation_event", None)
    if not callable(record_event):
        raise RuntimeError("Reconciliation audit is unavailable")
    return record_event(
        action=action,
        record_id=record_id,
        before=before,
        after=after,
        reason=reason,
        actor=actor,
        correlation_id=correlation_id,
    )


def _safe_reconciliation_queue_counts(count_queue: Any, filters: dict[str, Any]) -> dict[str, int]:
    result = count_queue(
        statuses=filters["statuses"],
        stale_before=filters["staleBefore"],
        seller_account_id=filters["sellerRef"],
        provider=filters["provider"],
        network=filters["network"],
    )
    if not isinstance(result, dict):
        return {}
    allowed = {status.value for status in _RECONCILIATION_QUEUE_STATUSES}
    counts: dict[str, int] = {}
    for status, count in result.items():
        safe_status = str(status)
        if safe_status not in allowed:
            continue
        counts[safe_status] = max(0, int(count or 0))
    return counts


def _status_counts_from_queue_items(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _reconciliation_queue_record_json(record: Any) -> dict[str, Any]:
    item = _settlement_record_json(record)
    updated_at = getattr(record, "updated_at", None)
    age_seconds = 0
    if isinstance(updated_at, datetime):
        age_seconds = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
    item.update(
        {
            "ageSeconds": age_seconds,
            "reconciliation": {
                "owner": _safe_optional_id(str(getattr(record, "reconciliation_owner", "") or "")),
                "leaseUntil": _safe_datetime_iso(
                    getattr(record, "reconciliation_lease_until", None)
                ),
                "attempts": max(0, int(getattr(record, "reconciliation_attempts", 0) or 0)),
                "duplicateClaims": max(0, int(getattr(record, "duplicate_claims", 0) or 0)),
                "lastDuplicateAt": _safe_datetime_iso(getattr(record, "last_duplicate_at", None)),
            },
        }
    )
    return item


def _settlement_record_json(record: Any) -> dict[str, Any]:
    raw_requirements = getattr(record, "raw_requirements", {}) or {}
    if not isinstance(raw_requirements, dict):
        raw_requirements = {}
    amount_atomic = _safe_amount_atomic(raw_requirements.get("amount"))
    return {
        "recordId": int(getattr(record, "record_id", 0) or 0),
        "sellerRef": _safe_seller_ref(str(getattr(record, "seller_account_id", "") or "")),
        "paymentProfileId": _safe_profile_id(str(getattr(record, "payment_profile_id", "") or "")),
        "provider": _safe_provider_name(str(getattr(record, "provider", "") or "")),
        "scheme": _safe_scheme(str(getattr(record, "scheme", "") or "")),
        "network": _safe_network_or_none(str(getattr(record, "network", "") or "")) or "",
        "status": _safe_settlement_status(str(getattr(record, "status", "") or "")),
        "traceId": _safe_optional_id(str(getattr(record, "trace_id", "") or "")),
        "transaction": _redacted_transaction(str(getattr(record, "transaction", "") or "")),
        "payer": _redacted_evm_address(str(getattr(record, "payer", "") or "")),
        "errorReason": _safe_error_reason(str(getattr(record, "error_reason", "") or "")),
        "amountAtomic": amount_atomic,
        "amountUsdc": _format_usdc_amount(amount_atomic),
        "asset": _redacted_evm_address(str(raw_requirements.get("asset") or "")),
        "payTo": _redacted_evm_address(str(raw_requirements.get("payTo") or "")),
        "rail": _safe_provider_name(str((raw_requirements.get("extra") or {}).get("name") or "")),
        "createdAt": _safe_datetime_iso(getattr(record, "created_at", None)),
        "updatedAt": _safe_datetime_iso(getattr(record, "updated_at", None)),
        "reconciliationAttempts": max(0, int(getattr(record, "reconciliation_attempts", 0) or 0)),
    }


def _settlement_record_detail_json(record: Any) -> dict[str, Any]:
    summary = _settlement_record_json(record)
    raw_requirements = getattr(record, "raw_requirements", {}) or {}
    if not isinstance(raw_requirements, dict):
        raw_requirements = {}
    transaction = _safe_transaction(str(getattr(record, "transaction", "") or ""))
    payer = _safe_evm_address_or_empty(str(getattr(record, "payer", "") or ""))
    pay_to = _safe_evm_address_or_empty(str(raw_requirements.get("payTo") or ""))
    asset = _safe_evm_address_or_empty(str(raw_requirements.get("asset") or ""))
    summary.update(
        {
            "transaction": transaction,
            "transactionKind": _transaction_kind(summary["provider"], transaction),
            "explorerUrl": _explorer_url(summary["network"], transaction),
            "payer": payer,
            "asset": asset,
            "payTo": pay_to,
            "fingerprint": _safe_optional_id(str(getattr(record, "fingerprint", "") or "")),
            "rawRequirements": _safe_requirements_for_ops(raw_requirements),
            "reconciliation": {
                "owner": _safe_optional_id(str(getattr(record, "reconciliation_owner", "") or "")),
                "leaseUntil": _safe_datetime_iso(
                    getattr(record, "reconciliation_lease_until", None)
                ),
                "attempts": max(0, int(getattr(record, "reconciliation_attempts", 0) or 0)),
                "duplicateClaims": max(0, int(getattr(record, "duplicate_claims", 0) or 0)),
                "lastDuplicateAt": _safe_datetime_iso(getattr(record, "last_duplicate_at", None)),
            },
        }
    )
    return summary


def _settlement_attempt_json(attempt: Any, *, network: str, provider: str = "") -> dict[str, Any]:
    transaction = _safe_transaction(str(getattr(attempt, "transaction", "") or ""))
    return {
        "attemptId": max(0, int(getattr(attempt, "attempt_id", 0) or 0)),
        "settlementRecordId": max(0, int(getattr(attempt, "settlement_record_id", 0) or 0)),
        "traceId": _safe_optional_id(str(getattr(attempt, "trace_id", "") or "")),
        "status": _safe_status(str(getattr(attempt, "status", "") or "")),
        "startedAt": _safe_datetime_iso(getattr(attempt, "started_at", None)),
        "finishedAt": _safe_datetime_iso(getattr(attempt, "finished_at", None)),
        "transaction": transaction,
        "transactionKind": _transaction_kind(provider, transaction),
        "explorerUrl": _explorer_url(network, transaction),
        "errorReason": _safe_error_reason(str(getattr(attempt, "error_reason", "") or "")),
    }


def _safe_requirements_for_ops(requirements: dict[str, Any]) -> dict[str, Any]:
    raw_extra = requirements.get("extra") if isinstance(requirements.get("extra"), dict) else {}
    safe: dict[str, Any] = {
        "scheme": _safe_scheme(str(requirements.get("scheme") or "")),
        "network": _safe_network_or_none(str(requirements.get("network") or "")) or "",
        "asset": _safe_evm_address_or_empty(str(requirements.get("asset") or "")),
        "amount": _safe_amount_atomic(requirements.get("amount")),
        "payTo": _safe_evm_address_or_empty(str(requirements.get("payTo") or "")),
        "rail": _safe_provider_name(str(raw_extra.get("name") or "")),
    }
    resource_hash = str(requirements.get("resourceHash") or "")
    if resource_hash and all(ch in "0123456789abcdef" for ch in resource_hash.lower()):
        safe["resourceHash"] = resource_hash.lower()[:64]
    return safe


def _transaction_kind(provider: str, transaction: str) -> str:
    if not transaction:
        return ""
    if _is_evm_transaction_hash(transaction):
        return "evm_tx"
    if provider == "circle_gateway":
        return "gateway_transfer"
    if provider == "exact_evm":
        return "reference"
    return "reference"


def _explorer_url(network: str, transaction: str) -> str:
    if not _is_evm_transaction_hash(transaction):
        return ""
    if network == "eip155:5042002":
        return f"https://testnet.arcscan.app/tx/{transaction}"
    return ""


def _is_evm_transaction_hash(value: str) -> bool:
    return (
        len(value) == 66
        and value.startswith("0x")
        and all(ch in "0123456789abcdefABCDEF" for ch in value[2:])
    )


def _safe_profile_id(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", "."})[:64]
    return safe or "default"


def _request_profile_id(value: str) -> str:
    candidate = value.strip() or "default"
    safe = _safe_profile_id(candidate)
    if safe != candidate:
        raise ValueError("Invalid payment profile id")
    return safe


def _safe_scheme(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-"})[:24]
    return safe or "unknown"


def _safe_settlement_status(value: str) -> str:
    allowed = {status.value for status in SettlementStatus}
    return value if value in allowed else "unknown"


def _parse_reconciliation_status(value: str) -> SettlementStatus:
    try:
        status = SettlementStatus(value)
    except ValueError as exc:
        raise ValueError(f"unsupported settlement status: {value}") from exc
    if status not in _RECONCILIATION_QUEUE_STATUSES:
        raise ValueError(f"unsupported reconciliation status: {value}")
    return status


def _safe_query_int(
    value: str | None,
    *,
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value in {None, ""}:
        return default
    try:
        parsed = int(str(value), 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _safe_amount_atomic(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text if text.isdigit() else ""


def _format_usdc_amount(amount_atomic: str) -> str:
    if not amount_atomic:
        return ""
    try:
        amount = Decimal(amount_atomic) / Decimal("1000000")
    except (InvalidOperation, ValueError):
        return ""
    formatted = f"{amount.normalize():f}"
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def _safe_transaction(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", "."})[:128]
    return safe


def _redacted_transaction(value: str) -> str:
    safe = _safe_transaction(value)
    if not safe:
        return ""
    if len(safe) <= 12:
        return "redacted"
    return f"{safe[:6]}...{safe[-4:]}"


def _safe_optional_id(value: str) -> str:
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", "."})[:96]
    return safe


def _safe_error_reason(value: str) -> str:
    if _SECRET_SHAPED_REASON_RE.search(value):
        return "redacted"
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", ".", ":"})[:160]
    return safe


def _safe_datetime_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return ""


def _supported_networks_from_provider_checks(
    provider_checks: list[dict[str, Any]],
) -> tuple[str, ...]:
    networks: set[str] = set()
    for check in provider_checks:
        values = check.get("supportedNetworks")
        if isinstance(values, (list, tuple)):
            for value in values:
                safe = _safe_network_or_none(str(value))
                if safe:
                    networks.add(safe)
        safe = _safe_network_or_none(str(check.get("network") or ""))
        if safe:
            networks.add(safe)
    return tuple(sorted(networks))


def _safe_int_call(target: Any, name: str) -> int:
    method = getattr(target, name, None)
    if method is None:
        return 0
    value = method()
    if not isinstance(value, int):
        return 0
    return max(value, 0)


def _safe_status_counts(store: Any) -> dict[str, int]:
    method = getattr(store, "settlement_status_counts", None)
    if method is None:
        return {}
    result = method()
    if not isinstance(result, dict):
        return {}
    allowed = {status.value for status in SettlementStatus}
    counts: dict[str, int] = {}
    for key, value in result.items():
        status = str(key)
        if status not in allowed or not isinstance(value, int):
            continue
        counts[status] = max(value, 0)
    return counts


def _record_control_plane_read(
    telemetry: HostedTelemetryProtocol,
    *,
    endpoint: str,
    status_code: int,
    duration_seconds: float,
    correlation_id: str | None = None,
) -> None:
    attributes = {"endpoint": endpoint, "status": str(status_code)}
    try:
        telemetry.record_counter("omniclaw.hosted.http.server.requests", attributes)
        telemetry.record_histogram(
            "omniclaw.hosted.http.server.duration",
            duration_seconds,
            attributes,
        )
        telemetry.log_event(
            "info",
            "control_plane_request",
            {
                "endpoint": endpoint,
                "status": str(status_code),
                "http.status_code": status_code,
                "duration_ms": int(duration_seconds * 1000),
                "omniclaw.correlation_id": correlation_id,
            },
        )
    except Exception:
        return


def _record_control_plane_change(
    telemetry: HostedTelemetryProtocol,
    *,
    action: str,
    status: str,
) -> None:
    try:
        telemetry.record_counter(
            "omniclaw.hosted.control_plane.changes",
            {"action": action, "status": status},
        )
    except Exception:
        return


def _log_control_plane_change(
    telemetry: HostedTelemetryProtocol,
    *,
    status: str,
    correlation_id: str,
    action: str,
) -> None:
    try:
        telemetry.log_event(
            "info",
            "control_plane_change",
            {
                "action": action,
                "status": status,
                "omniclaw.correlation_id": correlation_id,
            },
        )
    except Exception:
        return


@contextmanager
def _telemetry_span(
    telemetry: HostedTelemetryProtocol,
    name: str,
    attributes: dict[str, Any] | None = None,
):
    manager = None
    try:
        manager = telemetry.span(name, attributes)
        span = manager.__enter__()
    except Exception:
        yield None
        return
    try:
        yield span
    except BaseException:
        with suppress(Exception):
            telemetry.record_exception(span, sys.exc_info()[1])
            manager.__exit__(*sys.exc_info())
        raise
    else:
        with suppress(Exception):
            manager.__exit__(None, None, None)


def _pause_state_json(control_state: Any) -> dict[str, Any]:
    state = control_state.pause_state()
    return {
        "globalPaused": bool(state.global_paused),
        "providers": tuple(sorted(_safe_provider_name(provider) for provider in state.providers)),
        "networks": tuple(sorted(_safe_network(network) for network in state.networks)),
        "writesEnabled": bool(getattr(control_state, "writes_enabled", False)),
    }


def _seller_json(seller_account: Any) -> dict[str, Any]:
    profiles = tuple(getattr(seller_account, "payment_profiles", ()) or ())
    if not profiles:
        default_profile = getattr(seller_account, "default_payment_profile", None)
        profiles = (default_profile(),) if callable(default_profile) else ()
    return {
        "sellerRef": str(seller_account.seller_account_id),
        "tenantRef": str(seller_account.tenant_id),
        "name": str(seller_account.name),
        "environment": str(seller_account.environment),
        "status": str(seller_account.status),
        "enabledNetworks": tuple(str(value) for value in seller_account.enabled_networks),
        "enabledSchemes": tuple(str(value) for value in seller_account.enabled_schemes),
        "enabledProviders": tuple(str(value) for value in seller_account.enabled_providers),
        "allowedAssets": tuple(str(value) for value in seller_account.allowed_assets),
        "allowedPayTo": tuple(str(value) for value in seller_account.allowed_pay_to),
        "paymentProfiles": tuple(_payment_profile_json(profile) for profile in profiles),
    }


def _seller_admin_detail(
    seller_account_resolver: Any,
    seller_account_id: str,
    *,
    settlement_store: Any | None = None,
) -> dict[str, Any] | None:
    seller = _get_existing_seller_account(seller_account_resolver, seller_account_id)
    if seller is None:
        return None
    return {
        "seller": _seller_json(seller),
        "apiKeys": _seller_api_key_summaries(seller_account_resolver, seller_account_id),
        "settlement": _seller_settlement_summary(settlement_store, seller_account_id),
    }


def _seller_settlement_summary(store: Any | None, seller_account_id: str) -> dict[str, Any]:
    if store is None:
        return _empty_seller_settlement_summary()
    records = _seller_recent_settlement_records(store, seller_account_id)
    counts = _safe_status_counts_for_seller(store, seller_account_id)
    total_amount_atomic = _safe_amount_atomic_for_seller(store, seller_account_id, records)
    last_settlement_at = _safe_last_successful_settlement_at_for_seller(
        store, seller_account_id, records
    )
    last_updated_at = str(records[0].get("updatedAt") or "") if records else ""
    return {
        "records": max(0, sum(counts.values())),
        "attempts": _safe_attempt_count_for_seller(store, seller_account_id, records),
        "manualReviewBacklog": counts.get(SettlementStatus.MANUAL_REVIEW.value, 0),
        "unknown": counts.get(SettlementStatus.UNKNOWN.value, 0),
        "submitted": counts.get(SettlementStatus.SUBMITTED.value, 0),
        "settled": counts.get(SettlementStatus.SETTLED.value, 0),
        "failed": counts.get(SettlementStatus.SETTLE_FAILED.value, 0),
        "statusCounts": counts,
        "totalAmountAtomic": str(total_amount_atomic),
        "totalAmountUsdc": _format_usdc_amount(str(total_amount_atomic)),
        "lastSettlementAt": last_settlement_at,
        "lastUpdatedAt": last_updated_at,
        "reconciliationRisk": _seller_reconciliation_risk_summary(store, seller_account_id),
        "recentRecords": records,
    }


def _empty_seller_settlement_summary() -> dict[str, Any]:
    return {
        "records": 0,
        "attempts": 0,
        "manualReviewBacklog": 0,
        "unknown": 0,
        "submitted": 0,
        "settled": 0,
        "failed": 0,
        "statusCounts": {},
        "totalAmountAtomic": "0",
        "totalAmountUsdc": "0",
        "lastSettlementAt": "",
        "lastUpdatedAt": "",
        "reconciliationRisk": {
            "backlog": 0,
            "active": 0,
            "staleActive": 0,
            "manualReview": 0,
            "unknown": 0,
            "oldestAgeSeconds": 0,
        },
        "recentRecords": [],
    }


def _seller_reconciliation_risk_summary(store: Any, seller_account_id: str) -> dict[str, int]:
    filters = {
        "statuses": _RECONCILIATION_QUEUE_STATUSES,
        "staleBefore": None,
        "sellerRef": seller_account_id,
        "provider": None,
        "network": None,
    }
    count_queue = getattr(store, "reconciliation_queue_status_counts", None)
    counts = (
        _safe_reconciliation_queue_counts(count_queue, filters)
        if callable(count_queue)
        else _seller_reconciliation_counts_from_queue_or_recent(store, seller_account_id)
    )
    stale_filters = {
        **filters,
        "statuses": _ACTIVE_RECONCILIATION_STATUSES,
        "staleBefore": datetime.now(timezone.utc)
        - timedelta(seconds=_OPERATOR_ACTIVE_CLAIM_STALE_SECONDS),
    }
    stale_counts = (
        _safe_reconciliation_queue_counts(count_queue, stale_filters)
        if callable(count_queue)
        else {}
    )
    active = sum(counts.get(status.value, 0) for status in _ACTIVE_RECONCILIATION_STATUSES)
    stale_active = sum(
        stale_counts.get(status.value, 0) for status in _ACTIVE_RECONCILIATION_STATUSES
    )
    oldest_age_seconds = _seller_oldest_reconciliation_age_seconds(store, seller_account_id)
    return {
        "backlog": sum(counts.values()),
        "active": max(0, active),
        "staleActive": max(0, stale_active),
        "manualReview": counts.get(SettlementStatus.MANUAL_REVIEW.value, 0),
        "unknown": counts.get(SettlementStatus.UNKNOWN.value, 0),
        "oldestAgeSeconds": oldest_age_seconds,
    }


def _seller_oldest_reconciliation_age_seconds(store: Any, seller_account_id: str) -> int:
    list_queue = getattr(store, "list_reconciliation_queue", None)
    if not callable(list_queue):
        return 0
    records = list_queue(
        statuses=_RECONCILIATION_QUEUE_STATUSES,
        stale_before=None,
        seller_account_id=seller_account_id,
        provider=None,
        network=None,
        limit=1,
    )
    if not isinstance(records, list) or not records:
        return 0
    updated_at = getattr(records[0], "updated_at", None)
    if not isinstance(updated_at, datetime):
        return 0
    return max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))


def _seller_reconciliation_counts_from_queue_or_recent(
    store: Any,
    seller_account_id: str,
) -> dict[str, int]:
    list_queue = getattr(store, "list_reconciliation_queue", None)
    if callable(list_queue):
        records = list_queue(
            statuses=_RECONCILIATION_QUEUE_STATUSES,
            stale_before=None,
            seller_account_id=seller_account_id,
            provider=None,
            network=None,
            limit=100,
        )
        if isinstance(records, list):
            return _status_counts_from_queue_items(
                [_settlement_record_json(record) for record in records]
            )
    allowed = {status.value for status in _RECONCILIATION_QUEUE_STATUSES}
    return _status_counts_from_queue_items(
        [
            record
            for record in _seller_recent_settlement_records(store, seller_account_id)
            if str(record.get("status") or "") in allowed
        ]
    )


def _seller_recent_settlement_records(store: Any, seller_account_id: str) -> list[dict[str, Any]]:
    method = getattr(store, "list_recent_records_for_seller", None)
    if callable(method):
        records = method(seller_account_id, limit=10)
        if isinstance(records, list):
            return [_settlement_record_json(record) for record in records[:10]]
    return [
        record
        for record in _safe_recent_settlement_records(store)
        if record.get("sellerRef") == seller_account_id
    ][:10]


def _safe_status_counts_for_seller(store: Any, seller_account_id: str) -> dict[str, int]:
    method = getattr(store, "settlement_status_counts_for_seller", None)
    if callable(method):
        result = method(seller_account_id)
        if isinstance(result, dict):
            allowed = {status.value for status in SettlementStatus}
            return {
                str(status): max(0, int(count))
                for status, count in result.items()
                if str(status) in allowed
            }
    counts: dict[str, int] = {}
    for record in _seller_recent_settlement_records(store, seller_account_id):
        status = str(record.get("status") or "")
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts


def _safe_attempt_count_for_seller(
    store: Any, seller_account_id: str, records: list[dict[str, Any]]
) -> int:
    method = getattr(store, "settlement_attempt_count_for_seller", None)
    if callable(method):
        return max(0, int(method(seller_account_id)))
    list_attempts = getattr(store, "list_attempts_for_record", None)
    if not callable(list_attempts):
        return 0
    attempts = 0
    for record in records:
        try:
            record_id = int(record.get("recordId") or 0)
            attempts += len(list_attempts(record_id, limit=100))
        except Exception:
            continue
    return attempts


def _safe_amount_atomic_for_seller(
    store: Any, seller_account_id: str, records: list[dict[str, Any]]
) -> int:
    method = getattr(store, "settlement_amount_atomic_for_seller", None)
    if callable(method):
        return max(0, int(method(seller_account_id)))
    return _sum_settlement_amount_atomic(records)


def _safe_last_successful_settlement_at_for_seller(
    store: Any, seller_account_id: str, records: list[dict[str, Any]]
) -> str:
    method = getattr(store, "last_successful_settlement_at_for_seller", None)
    if callable(method):
        return _safe_datetime_iso(method(seller_account_id))
    for record in records:
        if record.get("status") in {
            SettlementStatus.SETTLED.value,
            SettlementStatus.RECONCILED.value,
        }:
            return str(record.get("updatedAt") or "")
    return ""


def _sum_settlement_amount_atomic(records: list[dict[str, Any]]) -> int:
    total = 0
    for record in records:
        if record.get("status") not in {
            SettlementStatus.SETTLED.value,
            SettlementStatus.RECONCILED.value,
        }:
            continue
        if str(record.get("asset") or "").lower() not in {
            "0x3600...0000",
            "0x3600000000000000000000000000000000000000",
        }:
            continue
        amount = str(record.get("amountAtomic") or "")
        if amount.isdigit():
            total += int(amount)
    return total


def _list_seller_accounts(seller_account_resolver: Any) -> list[dict[str, Any]]:
    list_sellers = getattr(seller_account_resolver, "list_seller_accounts", None)
    if callable(list_sellers):
        result = list_sellers(limit=250)
        if isinstance(result, list):
            return [_safe_seller_summary(item) for item in result if isinstance(item, dict)]
    seller_account = _get_existing_seller_account(seller_account_resolver, "default")
    if seller_account is None:
        return []
    return [_safe_seller_summary(_seller_json(seller_account))]


def _safe_seller_summary(item: dict[str, Any]) -> dict[str, Any]:
    seller_ref = _safe_seller_ref(str(item.get("sellerRef") or item.get("seller_ref") or "default"))
    return {
        "sellerRef": seller_ref,
        "tenantRef": _safe_profile_id(
            str(item.get("tenantRef") or item.get("tenant_ref") or "alpha")
        ),
        "name": _safe_seller_name(str(item.get("name") or seller_ref), seller_ref),
        "environment": _safe_seller_environment(str(item.get("environment") or "testnet")),
        "status": _safe_status(str(item.get("status") or "active")),
        "paymentProfileCount": max(0, int(item.get("paymentProfileCount") or 0)),
        "activeApiKeyCount": max(0, int(item.get("activeApiKeyCount") or 0)),
        "createdAt": _safe_iso_string(str(item.get("createdAt") or "")),
        "updatedAt": _safe_iso_string(str(item.get("updatedAt") or "")),
    }


def _seller_api_key_summaries(
    seller_account_resolver: Any,
    seller_account_id: str,
) -> list[dict[str, Any]]:
    list_keys = getattr(seller_account_resolver, "list_seller_api_keys", None)
    if not callable(list_keys):
        return []
    result = list_keys(seller_account_id)
    if not isinstance(result, list):
        return []
    return [_safe_api_key_summary(item) for item in result if isinstance(item, dict)]


def _issued_key_json(issued: Any) -> dict[str, Any]:
    return {
        "keyId": _safe_key_id(str(issued.key_id)),
        "sellerRef": _safe_seller_ref(str(issued.seller_account_id)),
        "paymentProfileId": _safe_profile_id(str(issued.payment_profile_id)),
        "keyPrefix": _safe_key_prefix(str(issued.key_prefix)),
        "status": "active",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "revokedAt": None,
    }


def _safe_api_key_summary(item: dict[str, Any]) -> dict[str, Any]:
    revoked_at = item.get("revokedAt")
    return {
        "keyId": _safe_key_id(str(item.get("keyId") or "")),
        "sellerRef": _safe_seller_ref(str(item.get("sellerRef") or "default")),
        "paymentProfileId": _safe_profile_id(str(item.get("paymentProfileId") or "default")),
        "keyPrefix": _safe_key_prefix(str(item.get("keyPrefix") or "")),
        "status": _safe_status(str(item.get("status") or "disabled")),
        "createdAt": _safe_iso_string(str(item.get("createdAt") or "")),
        "revokedAt": _safe_iso_string(str(revoked_at)) if revoked_at else None,
    }


def _payment_profile_json(profile: Any) -> dict[str, Any]:
    return {
        "paymentProfileId": str(profile.payment_profile_id),
        "sellerRef": str(profile.seller_account_id),
        "name": str(profile.name),
        "status": str(profile.status),
        "enabledNetworks": tuple(str(value) for value in profile.enabled_networks),
        "enabledSchemes": tuple(str(value) for value in profile.enabled_schemes),
        "enabledProviders": tuple(str(value) for value in profile.enabled_providers),
        "allowedAssets": tuple(str(value) for value in profile.allowed_assets),
        "allowedPayTo": tuple(str(value) for value in profile.allowed_pay_to),
    }


def _audit_tail(control_state: Any) -> list[ControlPlaneAuditEvent]:
    audit_tail = getattr(control_state, "audit_tail", None)
    if audit_tail is None:
        return []
    events = audit_tail(limit=25)
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, ControlPlaneAuditEvent)]


def _audit_event_json(event: ControlPlaneAuditEvent) -> dict[str, Any]:
    action = "seller_create" if event.action == "seller_create" else event.action
    target_type = (
        "seller" if event.target_type == ControlPlaneTargetType.SELLER else event.target_type.value
    )
    return {
        "eventId": event.event_id,
        "action": action,
        "targetType": target_type,
        "target": event.target,
        "before": event.before,
        "after": event.after,
        "reason": event.reason,
        "actor": event.actor,
        "correlationId": event.correlation_id,
        "createdAt": event.created_at.isoformat(),
    }


def _normalize_pause_target(
    *,
    target_type: ControlPlaneTargetType,
    target: str | None,
) -> str:
    if target_type == ControlPlaneTargetType.GLOBAL:
        return "global"
    if target_type == ControlPlaneTargetType.PROVIDER:
        if not target:
            raise ValueError("provider pause requires target")
        return _safe_provider_name(target)
    if target_type == ControlPlaneTargetType.NETWORK:
        if not target:
            raise ValueError("network pause requires target")
        return _safe_network(target)
    raise ValueError("unsupported pause target type")


def _validate_pause_request(
    engine: HostedFacilitatorEngine,
    payload: ControlPlanePauseRequest,
) -> None:
    if payload.target_type == ControlPlaneTargetType.PROVIDER:
        target = _safe_provider_name(payload.target or "")
        if target not in engine.router.provider_names():
            raise ValueError("unknown provider target")
    if payload.target_type == ControlPlaneTargetType.NETWORK:
        _safe_network(payload.target or "")


def _set_membership(values: set[str], value: str, enabled: bool) -> None:
    if enabled:
        values.add(value)
    else:
        values.discard(value)


def _actor_from_request(request: Request) -> str:
    principal = getattr(request.state, "omniclaw_principal", None)
    if isinstance(principal, Principal):
        return _safe_actor(principal.actor)
    value = request.headers.get("x-omniclaw-operator", "unknown-operator")
    return _safe_actor(value)


def _actor_from_principal_or_request(principal: Principal, request: Request) -> str:
    if principal.issuer == "local" and principal.subject == "local-operator":
        return _actor_from_request(request)
    return _safe_actor(principal.actor)


def _lease_owner_from_principal_or_request(principal: Principal, request: Request) -> str:
    if principal.issuer == "local" and principal.subject == "local-operator":
        value = request.headers.get("x-omniclaw-operator", "unknown-operator")
        return _safe_operator_lease_owner(value, issuer="local")
    return _safe_operator_lease_owner(principal.subject, issuer=principal.issuer)


def _correlation_id_from_request(request: Request) -> str:
    existing = getattr(request.state, "omniclaw_correlation_id", None)
    if existing:
        return existing
    correlation_id = _safe_correlation_id(request.headers.get("x-request-id") or str(uuid4()))
    request.state.omniclaw_correlation_id = correlation_id
    return correlation_id


def _safe_actor(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-", ".", "@"})[:96]
    return safe or "unknown"


def _safe_operator_lease_owner(value: str, *, issuer: str = "") -> str:
    subject = str(value).strip() or "unknown"
    if not issuer and re.fullmatch(r"[A-Za-z0-9_.@-]{1,48}\.[0-9a-f]{24}", subject):
        return subject
    safe_subject = _safe_actor(subject)[:48]
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()[:24]
    return f"{safe_subject}.{digest}"


def _safe_correlation_id(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-", "."})[:96]
    return safe or str(uuid4())


def _safe_reason(value: str) -> str:
    return "operator_supplied" if str(value).strip() else "unspecified"


def _safe_audit_reason(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-", ":", "."})[:160]
    return safe or "unspecified"


def _seller_key_audit_reason(*, key_prefix: str, reason: str | None = None) -> str:
    value = f"api_key_prefix:{_safe_key_prefix(key_prefix)}"
    if reason is None:
        return value
    safe_reason = "_".join(str(reason).strip().split())
    return _safe_audit_reason(f"{value}:reason:{safe_reason}")


def _safe_reconciliation_action(action: str) -> str:
    safe = str(action).strip()
    if safe in {
        "reconciliation_claim",
        "reconciliation_release",
        "reconciliation_manual_review",
    }:
        return safe
    raise ValueError("unsupported reconciliation action")


def _safe_revoke_reason_text(value: str) -> str:
    reason = str(value).strip()
    if _SECRET_SHAPED_REASON_RE.search(reason):
        raise ValueError("seller API-key revoke reason must not contain secrets")
    return reason


def _safe_key_prefix(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-"})[:32]
    return safe or "unknown"


def _safe_provider_name(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-", "."})[:64]
    return safe or "unknown"


def _safe_seller_key_action(value: str) -> str:
    action = str(value)
    if action in {"seller_api_key_issue", "seller_api_key_revoke"}:
        return action
    raise ValueError("unsupported seller API-key audit action")


def _safe_key_id(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-"})[:80]
    if not safe:
        raise ValueError("seller API key id is required")
    return safe


def _safe_record_id(value: str) -> int:
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError("settlement record id is required")
    record_id = int(text)
    if record_id <= 0:
        raise ValueError("settlement record id is required")
    return record_id


def _safe_status(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"_", "-"})[:32]
    return safe or "unknown"


def _safe_iso_string(value: str) -> str:
    safe = "".join(ch for ch in str(value) if ch.isalnum() or ch in {"-", ":", ".", "+", "Z"})[:40]
    return safe


def _safe_network(value: str) -> str:
    text = str(value)
    if text.startswith("eip155:") and text[7:].isdigit() and len(text[7:]) <= 20:
        return text
    raise ValueError("unsupported control-plane network target")


def _safe_seller_ref(value: str) -> str:
    text = str(value).strip()
    if not 3 <= len(text) <= 64:
        raise ValueError("seller reference must be 3 to 64 characters")
    if not all(ch.isalnum() or ch in {"_", "-", "."} for ch in text):
        raise ValueError(
            "seller reference may only contain letters, numbers, underscore, hyphen, and dot"
        )
    if not text[0].isalnum():
        raise ValueError("seller reference must start with a letter or number")
    return text


def _safe_seller_name(value: str | None, seller_account_id: str) -> str:
    if value is None or not str(value).strip():
        return seller_account_id
    text = str(value).strip()
    if len(text) > 120:
        raise ValueError("seller name is too long")
    return "".join(ch for ch in text if ch.isprintable()) or seller_account_id


def _safe_seller_environment(value: str) -> str:
    environment = str(value).strip().lower()
    if environment not in {"testnet", "local"}:
        raise ValueError("only testnet seller creation is enabled in alpha")
    return environment


def _safe_evm_address(value: str) -> str:
    text = str(value).strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise ValueError("expected EVM address")
    if not all(ch in "0123456789abcdef" for ch in text[2:]):
        raise ValueError("expected EVM address")
    return text


def _safe_evm_address_or_empty(value: str) -> str:
    try:
        return _safe_evm_address(value)
    except ValueError:
        return ""


def _redacted_evm_address(value: str) -> str:
    try:
        safe = _safe_evm_address(value)
    except ValueError:
        return ""
    return f"{safe[:6]}...{safe[-4:]}"


def _mirror_external_audit_event(control_state: Any, event: ControlPlaneAuditEvent | None) -> None:
    if event is None or getattr(control_state, "durable", False):
        return
    record = getattr(control_state, "record_external_audit_event", None)
    if callable(record):
        record(event)


def _seller_account_admin_available(seller_account_resolver: Any) -> bool:
    return callable(getattr(seller_account_resolver, "get_seller_account", None))


def _get_existing_seller_account(
    seller_account_resolver: Any, seller_account_id: str
) -> Any | None:
    get_seller_account = getattr(seller_account_resolver, "get_seller_account", None)
    if not callable(get_seller_account):
        return None
    return get_seller_account(seller_account_id)


def _safe_network_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _safe_network(value)
    except ValueError:
        return None


def _close_store(store: Any) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()
