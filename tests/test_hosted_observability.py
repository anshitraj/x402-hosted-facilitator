from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

import hosted_facilitator.hosted.telemetry as hosted_telemetry
from hosted_facilitator.hosted.app import create_hosted_facilitator_app
from hosted_facilitator.hosted.auth import AuthorizationDeniedError, OperationsPermission, Principal
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import (
    ControlPlaneAuditEvent,
    ControlPlaneTargetType,
    InMemoryControlPlaneState,
)
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.limits import InMemoryFixedWindowRateLimiter, RateLimitPolicy
from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, SettleOutcome, VerifyOutcome
from hosted_facilitator.hosted.storage import (
    InMemorySettlementStore,
    SettlementAttemptStatus,
    SettlementStatus,
)
from hosted_facilitator.hosted.telemetry import (
    HostedJsonLogFormatter,
    HostedTelemetry,
    HostedTelemetryConfig,
)
from hosted_facilitator.hosted.tenancy import (
    SellerAccountConfig,
    StaticSellerAccountResolver,
    issue_seller_api_key,
    seller_key_hash,
    seller_key_prefix,
)

AUTH = {"Authorization": "Bearer secret-key"}
OPS_AUTH = {"Authorization": "Bearer oidc-token"}


def _seller_audit_event(
    *, action: str, seller_account_id: str, key_prefix: str, reason: str | None = None
) -> ControlPlaneAuditEvent:
    audit_reason = f"api_key_prefix:{key_prefix}"
    if action == "seller_api_key_revoke" and reason:
        audit_reason = f"{audit_reason}:reason:{'_'.join(reason.strip().split())}"
    return ControlPlaneAuditEvent(
        event_id=1,
        action=action,
        target_type=ControlPlaneTargetType.SELLER,
        target=seller_account_id,
        before=action == "seller_api_key_revoke",
        after=action in {"seller_create", "seller_api_key_issue"},
        reason=audit_reason,
        actor="ops@example.com",
        correlation_id="test-correlation",
        created_at=datetime.now(timezone.utc),
    )


class _AllowAllOperationsAuthorizer:
    hosted_safe = True

    async def authorize(self, authorization_header, permission, obj=None):
        del authorization_header, permission, obj
        return Principal(
            subject="ops-user", issuer="https://idp.example.test", email="ops@example.test"
        )


class _RequireOidcOperationsAuthorizer:
    hosted_safe = True

    async def authorize(self, authorization_header, permission, obj=None):
        del permission, obj
        if authorization_header != OPS_AUTH["Authorization"]:
            from hosted_facilitator.hosted.auth import AuthenticationError

            raise AuthenticationError("unauthorized")
        return Principal(
            subject="ops-user", issuer="https://idp.example.test", email="ops@example.test"
        )


class _SellerOnlyOperationsAuthorizer:
    hosted_safe = True

    async def authorize(self, authorization_header, permission, obj=None):
        del authorization_header, obj
        if permission != OperationsPermission.OPS_SELLERS:
            raise AuthorizationDeniedError("settlement permission required")
        return Principal(
            subject="ops-user", issuer="https://idp.example.test", email="ops@example.test"
        )


class _RecordingSpan:
    def __init__(self, name: str, attributes: Mapping[str, Any] | None = None):
        self.name = name
        self.attributes = dict(attributes or {})
        self.exceptions: list[str] = []

    def __repr__(self) -> str:
        return f"_RecordingSpan(name={self.name!r}, attributes={self.attributes!r})"


class _RecordingTelemetry:
    hosted_safe = True

    def __init__(self, health: dict[str, Any] | None = None):
        self.health = health or {"status": "ok"}
        self.spans: list[_RecordingSpan] = []
        self.counters: list[tuple[str, dict[str, Any]]] = []
        self.histograms: list[tuple[str, float, dict[str, Any]]] = []
        self.logs: list[tuple[str, str, dict[str, Any]]] = []

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, Any] | None = None):
        span = _RecordingSpan(name, attributes)
        self.spans.append(span)
        yield span

    def set_attributes(self, span: Any, attributes: Mapping[str, Any] | None) -> None:
        if isinstance(span, _RecordingSpan) and attributes:
            span.attributes.update(attributes)

    def record_exception(self, span: Any, exc: Exception) -> None:
        if isinstance(span, _RecordingSpan):
            span.exceptions.append(type(exc).__name__)

    def record_counter(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        self.counters.append((name, dict(attributes or {})))

    def record_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.histograms.append((name, value, dict(attributes or {})))

    def log_event(
        self,
        level: str,
        event: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.logs.append((level, event, dict(attributes or {})))

    async def health_check(self) -> dict[str, Any]:
        return self.health


class _ExplodingTelemetry(_RecordingTelemetry):
    def span(self, name: str, attributes: Mapping[str, Any] | None = None):
        raise RuntimeError("telemetry span failure")

    def set_attributes(self, span: Any, attributes: Mapping[str, Any] | None) -> None:
        raise RuntimeError("telemetry attribute failure")

    def record_counter(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        raise RuntimeError("telemetry counter failure")

    def record_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        raise RuntimeError("telemetry histogram failure")

    def log_event(
        self,
        level: str,
        event: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        raise RuntimeError("telemetry log failure")


class _Provider(HostedFacilitatorProvider):
    name = "exact_evm"

    def __init__(self):
        self.supported_calls = 0
        self.verify_calls = 0
        self.settle_calls = 0

    async def supported(self, context: RequestContext) -> list[dict]:
        self.supported_calls += 1
        return [
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }
        ]

    def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "provider": self.name,
            "supportedNetworks": ("eip155:5042002",),
        }

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        self.verify_calls += 1
        return VerifyOutcome(isValid=True, payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        self.settle_calls += 1
        return SettleOutcome(
            success=True,
            transaction="0x" + "22" * 32,
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


class _FailingHealthStore(InMemorySettlementStore):
    def health_check(self) -> bool:
        raise RuntimeError("postgres://secret-user:secret-pass@db.internal/omniclaw")


class _HostedSafeStore(InMemorySettlementStore):
    hosted_safe = True


class _HostedSafeRateLimiter(InMemoryFixedWindowRateLimiter):
    hosted_safe = True


class _HostedSafeNonDurableControlState(InMemoryControlPlaneState):
    hosted_safe = True
    durable = False
    writes_enabled = True


class _HostedSafeDurableControlState(_HostedSafeNonDurableControlState):
    durable = True


class _UnhealthyControlPlaneState(InMemoryControlPlaneState):
    def health_check(self) -> dict[str, Any]:
        raise RuntimeError("postgres://control-secret@db.internal/omniclaw")


class _FailingSellerCreateAuditState(InMemoryControlPlaneState):
    def record_seller_create(self, **_kwargs):
        raise RuntimeError("forced audit failure")


class _UnhealthyProjectResolver(StaticSellerAccountResolver):
    def health_check(self) -> dict[str, Any]:
        raise RuntimeError("postgres://seller-account-secret@db.internal/omniclaw")


class _HostedSafeProjectResolver(StaticSellerAccountResolver):
    hosted_safe = True


class _ProjectAdminResolver(StaticSellerAccountResolver):
    hosted_safe = True

    def __init__(self):
        super().__init__(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        )
        self._key_summaries = {
            "key_seeded": {
                "keyId": "key_seeded",
                "sellerRef": "default",
                "paymentProfileId": "default",
                "keyPrefix": "secret-key",
                "status": "active",
                "createdAt": "2026-01-01T00:00:00+00:00",
                "revokedAt": None,
            }
        }

    def get_seller_account(self, seller_account_id: str) -> SellerAccountConfig | None:
        return self._seller_accounts.get(seller_account_id)

    def list_seller_accounts(self, *, limit: int = 100):
        return [
            {
                "sellerRef": seller.seller_account_id,
                "tenantRef": seller.tenant_id,
                "name": seller.name,
                "environment": seller.environment,
                "status": seller.status,
                "paymentProfileCount": len(seller.payment_profiles) or 1,
                "activeApiKeyCount": sum(
                    1
                    for key in self._key_summaries.values()
                    if key["sellerRef"] == seller.seller_account_id and key["status"] == "active"
                ),
                "createdAt": "2026-01-01T00:00:00+00:00",
                "updatedAt": "2026-01-01T00:00:00+00:00",
            }
            for seller in list(self._seller_accounts.values())[:limit]
        ]

    def list_seller_api_keys(self, seller_account_id: str):
        return [
            dict(key)
            for key in self._key_summaries.values()
            if key["sellerRef"] == seller_account_id
        ]

    def create_seller_account(
        self,
        *,
        seller_account_id: str,
        tenant_id: str,
        name: str | None = None,
        environment: str = "testnet",
        enabled_networks: tuple[str, ...] = ("eip155:5042002",),
        enabled_schemes: tuple[str, ...] = ("exact",),
        enabled_providers: tuple[str, ...] = ("exact_evm",),
        allowed_assets: tuple[str, ...] = (),
        allowed_pay_to: tuple[str, ...] = (),
        rate_limits: RateLimitPolicy = RateLimitPolicy(),
    ) -> SellerAccountConfig:
        del name
        project = SellerAccountConfig(
            seller_account_id=seller_account_id,
            tenant_id=tenant_id,
            environment=environment,
            enabled_networks=enabled_networks,
            enabled_schemes=enabled_schemes,
            enabled_providers=enabled_providers,
            allowed_assets=allowed_assets,
            allowed_pay_to=allowed_pay_to,
            rate_limits=rate_limits,
        )
        self._seller_accounts[seller_account_id] = project
        return project

    def issue_api_key(self, seller_account_id: str, payment_profile_id: str = "default"):
        project = self._seller_accounts[seller_account_id]
        issued = issue_seller_api_key(seller_account_id, payment_profile_id=payment_profile_id)
        updated = replace(project, api_keys=project.api_keys + (issued.key,))
        self._seller_accounts[seller_account_id] = updated
        self._api_key_index[issued.key] = updated
        self._key_summaries[issued.key_id] = {
            "keyId": issued.key_id,
            "sellerRef": seller_account_id,
            "paymentProfileId": payment_profile_id,
            "keyPrefix": issued.key_prefix,
            "status": "active",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "revokedAt": None,
        }
        return issued

    def revoke_api_key(self, *, seller_account_id: str, key_id: str):
        key = self._key_summaries.get(key_id)
        if key is None or key["sellerRef"] != seller_account_id or key["status"] != "active":
            raise KeyError(key_id)
        key["status"] = "revoked"
        key["revokedAt"] = "2026-01-01T00:00:01+00:00"
        for raw, access in list(self._api_key_index.items()):
            if seller_key_prefix(raw) == key["keyPrefix"]:
                self._api_key_index.pop(raw)
        return dict(key)

    def revoke_api_key_hash(self, key_hash: str):
        for api_key, access in list(self._api_key_index.items()):
            if seller_key_hash(api_key) == key_hash:
                self._api_key_index.pop(api_key)
                return

    def create_seller_account_with_api_key(self, **kwargs):
        kwargs.pop("audit_actor", None)
        kwargs.pop("audit_correlation_id", None)
        project = self.create_seller_account(**kwargs)
        issued = self.issue_api_key(project.seller_account_id)
        return (
            self._seller_accounts[project.seller_account_id],
            issued,
            _seller_audit_event(
                action="seller_create",
                seller_account_id=project.seller_account_id,
                key_prefix=issued.key_prefix,
            ),
        )

    def issue_api_key_with_audit(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        actor: str,
        correlation_id: str,
    ):
        del actor, correlation_id
        issued = self.issue_api_key(seller_account_id, payment_profile_id)
        return issued, _seller_audit_event(
            action="seller_api_key_issue",
            seller_account_id=seller_account_id,
            key_prefix=issued.key_prefix,
        )

    def revoke_api_key_with_audit(
        self,
        *,
        seller_account_id: str,
        key_id: str,
        reason: str,
        actor: str,
        correlation_id: str,
    ):
        del actor, correlation_id
        key = self.revoke_api_key(seller_account_id=seller_account_id, key_id=key_id)
        return key, _seller_audit_event(
            action="seller_api_key_revoke",
            seller_account_id=seller_account_id,
            key_prefix=str(key["keyPrefix"]),
            reason=reason,
        )


class _AtomicOnlyProjectAdminResolver(_ProjectAdminResolver):
    def __init__(self):
        super().__init__()
        self.atomic_calls = 0

    def create_seller_account(self, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("legacy create_seller_account must not be called")

    def issue_api_key(self, _seller_account_id):  # pragma: no cover - must not be called
        raise AssertionError("legacy issue_api_key must not be called")

    def create_seller_account_with_api_key(self, **kwargs):
        self.atomic_calls += 1
        kwargs.pop("audit_actor", None)
        kwargs.pop("audit_correlation_id", None)
        project = SellerAccountConfig(
            seller_account_id=kwargs["seller_account_id"],
            tenant_id=kwargs["tenant_id"],
            environment=kwargs.get("environment", "testnet"),
            enabled_networks=kwargs.get("enabled_networks", ("eip155:5042002",)),
            enabled_schemes=kwargs.get("enabled_schemes", ("exact",)),
            enabled_providers=kwargs.get("enabled_providers", ("exact_evm",)),
            allowed_assets=kwargs.get("allowed_assets", ()),
            allowed_pay_to=kwargs.get("allowed_pay_to", ()),
        )
        issued = issue_seller_api_key(project.seller_account_id)
        updated = replace(project, api_keys=(issued.key,))
        self._seller_accounts[project.seller_account_id] = updated
        self._api_key_index[issued.key] = updated
        return (
            updated,
            issued,
            _seller_audit_event(
                action="seller_create",
                seller_account_id=updated.seller_account_id,
                key_prefix=issued.key_prefix,
            ),
        )


class _DuplicateRaceProjectAdminResolver(_AtomicOnlyProjectAdminResolver):
    def create_seller_account_with_api_key(self, **_kwargs):
        raise ValueError("Seller account already exists")


class _LegacyOnlyProjectAdminResolver(_ProjectAdminResolver):
    def __getattribute__(self, name):
        if name == "create_seller_account_with_api_key":
            raise AttributeError(name)
        return super().__getattribute__(name)


class _LifecycleControlPlaneState(InMemoryControlPlaneState):
    def __init__(self):
        super().__init__()
        self.initialize_calls = 0
        self.close_calls = 0

    def initialize(self) -> None:
        self.initialize_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _LifecycleSettlementStore(InMemorySettlementStore):
    def __init__(self):
        super().__init__()
        self.initialize_calls = 0
        self.close_calls = 0

    def initialize(self) -> None:
        self.initialize_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _FailingSellerSettlementStore(InMemorySettlementStore):
    def list_recent_records_for_seller(self, seller_account_id: str, *, limit: int = 25):
        del seller_account_id, limit
        raise RuntimeError("settlement visibility unavailable")


def _app(
    *,
    store: InMemorySettlementStore | None = None,
    telemetry: _RecordingTelemetry | None = None,
    policy: RateLimitPolicy | None = None,
    clock_value: float = 0.0,
    operations_authorizer: Any | None = None,
    control_plane_state: Any | None = None,
    resolver: Any | None = None,
):
    provider = _Provider()
    store = store or InMemorySettlementStore()
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store)
    resolver = resolver or StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="default",
                api_keys=("secret-key",),
                rate_limits=policy or RateLimitPolicy(),
            )
        ]
    )
    return create_hosted_facilitator_app(
        engine=engine,
        resolver=resolver,
        telemetry=telemetry,
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: clock_value),
        operations_authorizer=operations_authorizer or _AllowAllOperationsAuthorizer(),
        control_plane_state=control_plane_state,
    )


def _hosted_otel_config() -> HostedTelemetryConfig:
    return HostedTelemetryConfig(
        environment="staging",
        required=True,
        otlp_endpoint="https://otel.example.test",
        collector_health_url="https://otel.example.test/health",
        json_logs_required=True,
    )


def _hosted_mode_app(
    *,
    operations_authorizer: Any | None = None,
    control_plane_state: Any | None = None,
):
    provider = _Provider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=_HostedSafeStore(),
    )
    resolver = _HostedSafeProjectResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    return create_hosted_facilitator_app(
        engine=engine,
        resolver=resolver,
        rate_limiter=_HostedSafeRateLimiter(),
        telemetry_config=_hosted_otel_config(),
        operations_authorizer=operations_authorizer or _AllowAllOperationsAuthorizer(),
        control_plane_state=control_plane_state,
    )


def _disable_otel_exporters(monkeypatch) -> None:
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )


def _envelope(resource: str = "https://seller.example.com/compute") -> dict:
    return {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "payload": {
                "signature": "0xsig",
                "authorization": {
                    "from": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "to": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "value": "250000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "11" * 32,
                },
            },
            "accepted": {
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "amount": "250000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USDC", "version": "2"},
            },
        },
        "paymentRequirements": {
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "250000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "maxTimeoutSeconds": 300,
            "resource": resource,
            "description": "compute",
            "mimeType": "application/json",
            "extra": {"name": "USDC", "version": "2"},
        },
    }


def test_health_readiness_and_otel_records_are_sanitized():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "omniclaw_hosted_facilitator"}

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["components"]["settlementStore"]["status"] == "ok"
    assert ready.json()["components"]["rateLimiter"]["status"] == "ok"
    assert ready.json()["components"]["telemetry"]["status"] == "ok"
    assert ready.json()["components"]["providers"]["providerCount"] == 1

    assert client.get("/supported").status_code == 200
    assert client.post("/verify", json=_envelope(), headers=AUTH).status_code == 200
    assert client.post("/settle", json=_envelope(), headers=AUTH).status_code == 200
    assert client.post("/verify", json={"x402Version": 2}, headers=AUTH).status_code == 400
    assert client.get("/metrics").status_code == 404

    counter_names = {name for name, _attrs in telemetry.counters}
    assert "omniclaw.hosted.http.server.requests" in counter_names
    assert "omniclaw.hosted.rate_limit.decisions" in counter_names
    assert "omniclaw.hosted.verify.requests" in counter_names
    assert "omniclaw.hosted.settle.requests" in counter_names
    assert "omniclaw.hosted.invalid_requests" in counter_names

    rendered = f"{telemetry.counters} {telemetry.histograms} {telemetry.spans}"
    assert "secret-key" not in rendered
    assert "0xsig" not in rendered
    assert "seller.example.com" not in rendered
    assert "250000" not in rendered

    http_labels = [
        attrs
        for name, attrs in telemetry.counters
        if name == "omniclaw.hosted.http.server.requests"
    ]
    assert {"endpoint": "verify", "status": "400"} in http_labels
    histogram_labels = [
        attrs
        for name, _value, attrs in telemetry.histograms
        if name == "omniclaw.hosted.http.server.duration"
    ]
    assert {"endpoint": "verify", "status": "400"} in histogram_labels

    log_events = [event for _level, event, _attrs in telemetry.logs]
    assert "http_request" in log_events
    verify_logs = [
        attrs
        for _level, event, attrs in telemetry.logs
        if event == "http_request" and attrs.get("endpoint") == "verify"
    ]
    assert any(attrs.get("omniclaw.correlation_id", "").startswith("tr_") for attrs in verify_logs)
    assert any(attrs.get("omniclaw.seller_account_id") == "default" for attrs in verify_logs)
    assert "secret-key" not in f"{telemetry.logs}"
    assert "seller.example.com" not in f"{telemetry.logs}"


def test_structured_json_formatter_sanitizes_and_emits_correlation_fields():
    formatter = HostedJsonLogFormatter(service_name="omniclaw-hosted", environment="test")
    record = logging.LogRecord(
        name="hosted_facilitator.hosted",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="raw secret-key 0xsig should not be emitted",
        args=(),
        exc_info=None,
    )
    record.omniclaw_event = "http_request"
    record.omniclaw_attrs = {
        "endpoint": "verify",
        "status": "200",
        "http.status_code": 200,
        "omniclaw.correlation_id": "req-123",
        "resource": "https://seller.example.com/compute",
        "authorization": "Bearer secret-key",
    }

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "http_request"
    assert payload["endpoint"] == "verify"
    assert payload["http.status_code"] == 200
    assert payload["omniclaw.correlation_id"] == "req-123"
    rendered = json.dumps(payload)
    assert "secret-key" not in rendered
    assert "seller.example.com" not in rendered
    assert "authorization" not in rendered
    assert "resource" not in rendered


def test_readiness_failure_redacts_exception_message():
    client = TestClient(_app(store=_FailingHealthStore(), telemetry=_RecordingTelemetry()))

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["settlementStore"]["status"] == "unhealthy"
    assert body["components"]["settlementStore"]["errorType"] == "RuntimeError"
    assert "secret-pass" not in response.text
    assert "db.internal" not in response.text


def test_readiness_store_factory_failure_is_sanitized():
    def store_factory():
        raise RuntimeError("postgres://secret-user:secret-pass@db.internal/omniclaw")

    engine = HostedFacilitatorEngine(
        router=ProviderRouter([_Provider()]),
        store=InMemorySettlementStore(),
    )
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        store_factory=store_factory,
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        telemetry=_RecordingTelemetry(),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )
    client = TestClient(app)

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["components"]["settlementStore"]["status"] == "unhealthy"
    assert body["components"]["settlementStore"]["errorType"] == "RuntimeError"
    assert "secret-pass" not in response.text
    assert "db.internal" not in response.text


def test_readiness_telemetry_failure_is_sanitized():
    telemetry = _RecordingTelemetry({"status": "unhealthy", "errorType": "TimeoutError"})
    client = TestClient(_app(telemetry=telemetry))

    response = client.get("/readyz", headers=OPS_AUTH)

    assert response.status_code == 503
    body = response.json()
    assert body["components"]["telemetry"]["status"] == "unhealthy"
    assert body["components"]["telemetry"]["errorType"] == "TimeoutError"
    assert "https://otel.example.test" not in response.text


def test_control_plane_overview_is_sanitized_and_backend_serves_no_inline_shell():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    assert client.get("/control-plane").status_code == 404
    assert client.get("/control-plane/api/overview", headers=OPS_AUTH).status_code == 404
    assert client.post("/control-plane/api/pause", json={}, headers=OPS_AUTH).status_code == 404
    assert client.post("/control-plane/api/pauses", json={}, headers=OPS_AUTH).status_code == 404
    assert client.post("/ops/api/pause", json={}, headers=OPS_AUTH).status_code == 404
    assert client.post("/settle", json=_envelope(), headers=AUTH).status_code == 200

    response = client.get("/ops/api/overview", headers=OPS_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "omniclaw_hosted_facilitator"
    assert body["settlement"]["records"] == 1
    assert body["settlement"]["attempts"] == 1
    assert body["settlement"]["statusCounts"]["settled"] == 1
    assert body["providers"]["names"] == ["exact_evm"]
    assert body["providers"]["items"][0]["name"] == "exact_evm"
    assert body["components"]["providers"]["items"][0]["name"] == "exact_evm"
    rendered = response.text
    assert "secret-key" not in rendered
    assert "0xsig" not in rendered
    assert "seller.example.com" not in rendered
    assert "0x" + "22" * 32 not in rendered
    assert "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in rendered

    assert (
        "omniclaw.hosted.http.server.requests",
        {"endpoint": "ops_overview", "status": "200"},
    ) in telemetry.counters


def test_control_plane_settlement_detail_exposes_authorized_transaction_evidence():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    settle = client.post("/settle", json=_envelope(), headers=AUTH)
    assert settle.status_code == 200
    overview = client.get("/ops/api/overview", headers=OPS_AUTH).json()
    record_id = overview["settlement"]["recentRecords"][0]["recordId"]

    response = client.get(f"/ops/api/settlements/{record_id}", headers=OPS_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["record"]["recordId"] == record_id
    assert body["record"]["transaction"] == "0x" + "22" * 32
    assert body["record"]["transactionKind"] == "evm_tx"
    assert body["record"]["explorerUrl"] == f"https://testnet.arcscan.app/tx/0x{'22' * 32}"
    assert body["record"]["payer"] == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert body["record"]["payTo"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert body["record"]["asset"] == "0x3600000000000000000000000000000000000000"
    assert body["record"]["rawRequirements"]["resourceHash"]
    assert body["attempts"][0]["status"] == "settled"
    assert body["attempts"][0]["transaction"] == "0x" + "22" * 32
    assert body["attempts"][0]["transactionKind"] == "evm_tx"
    assert body["attempts"][0]["explorerUrl"] == f"https://testnet.arcscan.app/tx/0x{'22' * 32}"
    assert response.headers["cache-control"] == "no-store"
    assert "seller.example.com" not in response.text
    assert (
        "omniclaw.hosted.http.server.requests",
        {"endpoint": "ops_settlements", "status": "200"},
    ) in telemetry.counters
    settlement_span = next(
        span
        for span in telemetry.spans
        if span.name == "omniclaw.hosted.control_plane.settlements.detail"
    )
    assert settlement_span.attributes["settlement.record_id"] == record_id


def test_control_plane_settlement_detail_rejects_invalid_record_ids():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    invalid = client.get("/ops/api/settlements/not-a-number", headers=OPS_AUTH)
    zero = client.get("/ops/api/settlements/0", headers=OPS_AUTH)

    assert invalid.status_code == 400
    assert invalid.headers["cache-control"] == "no-store"
    assert invalid.json()["detail"] == "settlement record id is required"
    assert zero.status_code == 400
    assert zero.headers["cache-control"] == "no-store"
    assert zero.json()["detail"] == "settlement record id is required"
    assert (
        "omniclaw.hosted.http.server.requests",
        {"endpoint": "ops_settlements", "status": "400"},
    ) in telemetry.counters


def test_control_plane_settlement_detail_does_not_link_non_hash_transaction_reference():
    store = InMemorySettlementStore()
    client = TestClient(_app(store=store))

    settle = client.post("/settle", json=_envelope(), headers=AUTH)
    assert settle.status_code == 200
    record = store.list_recent_records(limit=1)[0]
    record.transaction = "0xsettled"

    response = client.get(f"/ops/api/settlements/{record.record_id}", headers=OPS_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["record"]["transaction"] == "0xsettled"
    assert body["record"]["transactionKind"] == "reference"
    assert body["record"]["explorerUrl"] == ""


def test_control_plane_settlement_detail_redacts_secret_shaped_error_reasons():
    store = InMemorySettlementStore()
    record, _ = store.claim_for_settlement(
        seller_account_id="default",
        fingerprint="fp-secret-error",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_secret_error",
        raw_requirements={},
    )
    attempt_id = store.start_settle_attempt(record, trace_id="tr_secret_error")
    store.finish_settle_attempt(
        attempt_id,
        status=SettlementAttemptStatus.FAILED,
        error_reason="omck_secretKeyShouldNeverRender",
    )
    store.mark_unknown(record, error_reason="circle_api_key leaked by upstream")
    client = TestClient(_app(store=store))

    response = client.get(f"/ops/api/settlements/{record.record_id}", headers=OPS_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["record"]["errorReason"] == "redacted"
    assert body["attempts"][0]["errorReason"] == "redacted"
    assert "omck_secretKeyShouldNeverRender" not in response.text
    assert "circle_api_key" not in response.text


def test_control_plane_api_uses_oidc_openfga_authorizer():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            operations_authorizer=_RequireOidcOperationsAuthorizer(),
        )
    )

    assert client.get("/ops/api/overview").status_code == 401
    assert (
        client.get(
            "/ops/api/overview",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/ops/api/overview",
            headers=OPS_AUTH,
        ).status_code
        == 200
    )


def test_control_plane_seller_create_issues_api_key_for_seller_facilitator_auth():
    telemetry = _RecordingTelemetry()
    resolver = _ProjectAdminResolver()
    store = InMemorySettlementStore()
    client = TestClient(_app(store=store, telemetry=telemetry, resolver=resolver))

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "corey-alpha",
            "tenantRef": "alpha",
            "name": "Corey Alpha",
            "environment": "testnet",
            "enabledNetworks": ["eip155:5042002"],
            "allowedAssets": ["0x3600000000000000000000000000000000000000"],
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 201
    body = response.json()
    assert "project" not in body
    assert body["seller"]["sellerRef"] == "corey-alpha"
    assert body["seller"]["enabledProviders"] == ["exact_evm", "circle_gateway"]
    assert body["facilitatorPath"] == "/v2/x402"
    assert body["apiKey"].startswith("omck_")
    assert body["apiKeyPrefix"] == body["apiKey"][:16]
    assert body["auditEvent"]["action"] == "seller_create"
    assert body["auditEvent"]["targetType"] == "seller"
    assert body["auditEvent"]["target"] == "corey-alpha"
    assert body["auditEvent"]["reason"] == f"api_key_prefix:{body['apiKeyPrefix']}"

    verify = client.post(
        "/v2/x402/verify",
        json=_envelope(),
        headers={"Authorization": f"Bearer {body['apiKey']}"},
    )
    assert verify.status_code == 200
    default_settle = client.post(
        "/v2/x402/settle",
        json=_envelope("https://seller.example.com/default-seller"),
        headers=AUTH,
    )
    settle = client.post(
        "/v2/x402/settle",
        json=_envelope("https://seller.example.com/corey-alpha"),
        headers={"Authorization": f"Bearer {body['apiKey']}"},
    )
    second_settle_body = _envelope("https://seller.example.com/corey-alpha-second")
    second_settle_body["paymentPayload"]["payload"]["authorization"]["nonce"] = "0x" + "22" * 32
    second_settle = client.post(
        "/v2/x402/settle",
        json=second_settle_body,
        headers={"Authorization": f"Bearer {body['apiKey']}"},
    )
    assert default_settle.status_code == 200
    assert settle.status_code == 200
    assert second_settle.status_code == 200
    store.claim_for_settlement(
        seller_account_id="corey-alpha",
        payment_profile_id="default",
        fingerprint="fp-corey-in-progress",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_corey_in_progress",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "750000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "resource": "https://seller.example.com/corey-in-progress",
            "extra": {"name": "USDC"},
        },
    )
    detail = client.get("/ops/api/sellers/corey-alpha", headers=OPS_AUTH)
    assert detail.status_code == 200
    seller_settlement = detail.json()["settlement"]
    assert seller_settlement["records"] == 3
    assert seller_settlement["attempts"] == 2
    assert seller_settlement["settled"] == 2
    assert seller_settlement["submitted"] == 0
    assert seller_settlement["unknown"] == 0
    assert seller_settlement["failed"] == 0
    assert seller_settlement["totalAmountAtomic"] == "500000"
    assert seller_settlement["totalAmountUsdc"] == "0.5"
    assert seller_settlement["statusCounts"]["settle_in_progress"] == 1
    assert seller_settlement["reconciliationRisk"]["backlog"] == 1
    assert seller_settlement["reconciliationRisk"]["active"] == 1
    assert seller_settlement["reconciliationRisk"]["staleActive"] == 0
    assert seller_settlement["reconciliationRisk"]["manualReview"] == 0
    assert seller_settlement["reconciliationRisk"]["unknown"] == 0
    assert seller_settlement["reconciliationRisk"]["oldestAgeSeconds"] >= 0
    assert len(seller_settlement["recentRecords"]) == 3
    assert seller_settlement["recentRecords"][0]["sellerRef"] == "corey-alpha"
    assert seller_settlement["lastUpdatedAt"] == seller_settlement["recentRecords"][0]["updatedAt"]
    assert any(
        record["status"] == "settle_in_progress" for record in seller_settlement["recentRecords"]
    )
    assert seller_settlement["lastSettlementAt"] in {
        record["updatedAt"]
        for record in seller_settlement["recentRecords"]
        if record["status"] == "settled"
    }
    assert all(
        record["sellerRef"] == "corey-alpha" for record in seller_settlement["recentRecords"]
    )
    overview = client.get("/ops/api/overview", headers=OPS_AUTH)
    assert overview.status_code == 200
    assert overview.json()["auditTail"][0]["action"] == "seller_create"
    assert "omck_" not in json.dumps(telemetry.logs)
    assert (
        "omniclaw.hosted.control_plane.changes",
        {"action": "seller_create", "status": "ok"},
    ) in telemetry.counters


def test_control_plane_seller_detail_fails_closed_when_settlement_visibility_fails():
    telemetry = _RecordingTelemetry()
    client = TestClient(
        _app(
            store=_FailingSellerSettlementStore(),
            telemetry=telemetry,
            resolver=_ProjectAdminResolver(),
        )
    )

    response = client.get("/ops/api/sellers/default", headers=OPS_AUTH)

    assert response.status_code == 503
    assert response.json()["detail"] == "Seller detail unavailable"
    assert (
        "omniclaw.hosted.http.server.requests",
        {"endpoint": "ops_sellers", "status": "503"},
    ) in telemetry.counters


def test_control_plane_seller_detail_requires_settlement_visibility():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            operations_authorizer=_SellerOnlyOperationsAuthorizer(),
        )
    )

    response = client.get("/ops/api/sellers/default", headers=OPS_AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_control_plane_seller_detail_fallback_risk_counts_only_reconciliation_statuses():
    store = InMemorySettlementStore()
    settled_record, _ = store.claim_for_settlement(
        seller_account_id="default",
        payment_profile_id="default",
        fingerprint="fp-settled-fallback",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_settled_fallback",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
    )
    store.mark_settled(
        settled_record,
        transaction="0x" + "11" * 32,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    unknown_record, _ = store.claim_for_settlement(
        seller_account_id="default",
        payment_profile_id="default",
        fingerprint="fp-unknown-fallback",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_unknown_fallback",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        },
    )
    store.mark_unknown(
        unknown_record,
        error_reason="provider status unknown",
    )
    setattr(store, "reconciliation_queue_status_counts", None)
    setattr(store, "list_reconciliation_queue", None)
    client = TestClient(
        _app(store=store, telemetry=_RecordingTelemetry(), resolver=_ProjectAdminResolver())
    )

    response = client.get("/ops/api/sellers/default", headers=OPS_AUTH)

    assert response.status_code == 200
    settlement = response.json()["settlement"]
    assert settlement["records"] == 2
    assert settlement["settled"] == 1
    assert settlement["unknown"] == 1
    assert settlement["reconciliationRisk"]["backlog"] == 1
    assert settlement["reconciliationRisk"]["unknown"] == 1


def test_control_plane_seller_create_rejects_duplicate_seller_ref():
    resolver = _ProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))
    payload = {
        "sellerRef": "corey-alpha",
        "tenantRef": "alpha",
        "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
    }

    first = client.post("/ops/api/sellers", json=payload, headers=OPS_AUTH)
    second = client.post("/ops/api/sellers", json=payload, headers=OPS_AUTH)

    assert first.status_code == 201
    assert second.status_code == 409


def test_control_plane_lists_seller_and_manages_api_key_lifecycle():
    resolver = _ProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))
    payload = {
        "sellerRef": "managed-alpha",
        "tenantRef": "alpha",
        "name": "Managed Alpha",
        "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
    }

    created = client.post("/ops/api/sellers", json=payload, headers=OPS_AUTH)
    listed = client.get("/ops/api/sellers", headers=OPS_AUTH)
    detail = client.get("/ops/api/sellers/managed-alpha", headers=OPS_AUTH)
    issued = client.post(
        "/ops/api/sellers/managed-alpha/api-keys",
        json={"paymentProfileId": "default"},
        headers=OPS_AUTH,
    )
    key_id = issued.json()["key"]["keyId"]
    revoked = client.post(
        f"/ops/api/sellers/managed-alpha/api-keys/{key_id}/revoke",
        json={"reason": "qa key rotation"},
        headers=OPS_AUTH,
    )

    assert created.status_code == 201
    assert listed.status_code == 200
    assert any(seller["sellerRef"] == "managed-alpha" for seller in listed.json()["sellers"])
    assert detail.status_code == 200
    assert detail.json()["seller"]["sellerRef"] == "managed-alpha"
    assert detail.json()["apiKeys"][0]["status"] == "active"
    assert issued.status_code == 201
    assert issued.json()["apiKey"].startswith("omck_")
    assert issued.json()["auditEvent"]["action"] == "seller_api_key_issue"
    assert revoked.status_code == 200
    assert revoked.json()["key"]["status"] == "revoked"
    assert revoked.json()["auditEvent"]["action"] == "seller_api_key_revoke"
    assert revoked.json()["auditEvent"]["reason"].endswith(":reason:qa_key_rotation")


def test_control_plane_seller_detail_rejects_invalid_seller_refs():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    response = client.get("/ops/api/sellers/..bad", headers=OPS_AUTH)

    assert response.status_code == 400
    assert "seller reference must start" in response.json()["detail"]


def test_control_plane_rejects_secret_shaped_api_key_revoke_reason():
    resolver = _ProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))
    payload = {
        "sellerRef": "reject-secret-reason",
        "tenantRef": "alpha",
        "name": "Reject Secret Reason",
        "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
    }

    created = client.post("/ops/api/sellers", json=payload, headers=OPS_AUTH)
    issued = client.post(
        "/ops/api/sellers/reject-secret-reason/api-keys",
        json={"paymentProfileId": "default"},
        headers=OPS_AUTH,
    )
    key_id = issued.json()["key"]["keyId"]
    rejected = client.post(
        f"/ops/api/sellers/reject-secret-reason/api-keys/{key_id}/revoke",
        json={"reason": f"rotate leaked {created.json()['apiKey']}"},
        headers=OPS_AUTH,
    )

    assert created.status_code == 201
    assert issued.status_code == 201
    assert rejected.status_code == 400
    assert "must not contain secrets" in rejected.json()["detail"]


def test_control_plane_seller_create_uses_resolver_atomic_audit():
    resolver = _ProjectAdminResolver()
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            resolver=resolver,
            control_plane_state=_FailingSellerCreateAuditState(),
        )
    )

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "audit-failure-alpha",
            "tenantRef": "alpha",
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 201
    assert response.json()["auditEvent"]["action"] == "seller_create"
    assert resolver.get_seller_account("audit-failure-alpha") is not None
    assert response.json()["apiKey"] in resolver._api_key_index


def test_control_plane_seller_create_uses_atomic_seller_and_api_key_method():
    resolver = _AtomicOnlyProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "atomic-alpha",
            "tenantRef": "alpha",
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 201
    assert resolver.atomic_calls == 1
    api_key = response.json()["apiKey"]
    verify = client.post(
        "/v2/x402/verify",
        json=_envelope(),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert verify.status_code == 200


def test_control_plane_seller_create_maps_atomic_duplicate_race_to_conflict():
    resolver = _DuplicateRaceProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "raced-alpha",
            "tenantRef": "alpha",
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Seller account already exists"


def test_control_plane_seller_create_requires_atomic_admin_in_hosted_mode(monkeypatch):
    resolver = _LegacyOnlyProjectAdminResolver()
    client = TestClient(_app(telemetry=_RecordingTelemetry(), resolver=resolver))
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "legacy-alpha",
            "tenantRef": "alpha",
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Atomic seller administration is unavailable"


def test_control_plane_seller_create_requires_seller_admin_resolver():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    response = client.post(
        "/ops/api/sellers",
        json={
            "sellerRef": "corey-alpha",
            "tenantRef": "alpha",
            "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Seller administration is unavailable"


def test_control_plane_health_is_reported_in_ready_and_overview():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    ready = client.get("/ready", headers=OPS_AUTH)
    overview = client.get("/ops/api/overview", headers=OPS_AUTH)

    assert ready.status_code == 200
    assert ready.json()["components"]["controlPlane"]["status"] == "ok"
    assert overview.status_code == 200
    assert overview.json()["components"]["controlPlane"]["status"] == "ok"


def test_control_plane_health_failure_marks_ready_and_overview_unhealthy():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            control_plane_state=_UnhealthyControlPlaneState(),
        )
    )

    ready = client.get("/ready")
    overview = client.get("/ops/api/overview")

    assert ready.status_code == 503
    assert ready.json()["components"]["controlPlane"]["status"] == "unhealthy"
    assert ready.json()["components"]["controlPlane"]["errorType"] == "RuntimeError"
    assert "control-secret" not in ready.text
    assert "db.internal" not in ready.text
    assert overview.status_code == 503
    assert overview.json()["components"]["controlPlane"]["status"] == "unhealthy"
    assert overview.json()["components"]["controlPlane"]["errorType"] == "RuntimeError"
    assert "control-secret" not in overview.text
    assert "db.internal" not in overview.text


def test_project_resolver_health_failure_marks_ready_unhealthy_and_sanitized():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            resolver=_UnhealthyProjectResolver(
                [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
            ),
        )
    )

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["components"]["sellerAccountResolver"]["status"] == "unhealthy"
    assert response.json()["components"]["sellerAccountResolver"]["errorType"] == "RuntimeError"
    assert "seller-account-secret" not in response.text
    assert "db.internal" not in response.text


def test_control_plane_lifecycle_is_registered():
    control_state = _LifecycleControlPlaneState()

    with TestClient(
        _app(telemetry=_RecordingTelemetry(), control_plane_state=control_state)
    ) as client:
        assert control_state.initialize_calls == 1
        assert client.get("/health").status_code == 200

    assert control_state.close_calls == 1


def test_settlement_store_lifecycle_is_registered_without_request_store_factory():
    store = _LifecycleSettlementStore()

    with TestClient(_app(store=store, telemetry=_RecordingTelemetry())) as client:
        assert store.initialize_calls == 1
        assert client.get("/health").status_code == 200

    assert store.close_calls == 1


def test_settlement_store_factory_startup_migrates_and_closes_factory_store():
    stores: list[_LifecycleSettlementStore] = []

    def store_factory():
        store = _LifecycleSettlementStore()
        stores.append(store)
        return store

    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]))
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        store_factory=store_factory,
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        telemetry=_RecordingTelemetry(),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )

    with TestClient(app) as client:
        assert len(stores) == 1
        assert stores[0].initialize_calls == 1
        assert stores[0].close_calls == 1
        assert client.get("/health").status_code == 200


def test_control_plane_pause_provider_blocks_new_protocol_routes():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    pause = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "provider maintenance",
        },
        headers={"x-request-id": "cp-change-123"},
    )
    assert pause.status_code == 200
    assert pause.json()["event"]["before"] is False
    assert pause.json()["event"]["after"] is True

    supported = client.get("/supported")
    assert supported.status_code == 200
    assert supported.json()["kinds"] == []
    assert client.post("/verify", json=_envelope(), headers=AUTH).status_code == 423
    assert client.post("/settle", json=_envelope(), headers=AUTH).status_code == 423

    overview = client.get("/ops/api/overview").json()
    assert overview["pauseState"]["providers"] == ["exact_evm"]
    assert overview["auditTail"][0]["actor"] == "ops-user"
    assert overview["auditTail"][0]["reason"] == "operator_supplied"
    assert overview["auditTail"][0]["correlationId"] == "cp-change-123"
    assert "omniclaw.hosted.control_plane.changes" in {name for name, _attrs in telemetry.counters}

    resume = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": False,
            "reason": "provider restored",
        },
    )
    assert resume.status_code == 200
    assert client.post("/verify", json=_envelope(), headers=AUTH).status_code == 200


def test_control_plane_generated_correlation_id_is_stable_across_audit_and_logs():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "provider maintenance",
        },
        headers=OPS_AUTH,
    )

    assert response.status_code == 200
    correlation_id = response.json()["event"]["correlationId"]
    assert correlation_id
    change_logs = [
        attrs
        for _level, event, attrs in telemetry.logs
        if event == "control_plane_change" and attrs.get("status") == "ok"
    ]
    request_logs = [
        attrs
        for _level, event, attrs in telemetry.logs
        if event == "control_plane_request" and attrs.get("status") == "200"
    ]
    assert any(attrs.get("omniclaw.correlation_id") == correlation_id for attrs in change_logs)
    assert any(attrs.get("omniclaw.correlation_id") == correlation_id for attrs in request_logs)
    assert any(attrs.get("endpoint") == "ops_pauses" for attrs in request_logs)
    assert "omniclaw.hosted.control_plane.pause" in {span.name for span in telemetry.spans}


def test_control_plane_pause_is_side_effect_free_before_protocol_dispatch():
    provider = _Provider()
    store = InMemorySettlementStore()
    store_factory_calls = {"count": 0}
    request_stores: list[InMemorySettlementStore] = []

    def store_factory():
        store_factory_calls["count"] += 1
        request_store = InMemorySettlementStore()
        request_stores.append(request_store)
        return request_store

    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        store_factory=store_factory,
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        telemetry=_RecordingTelemetry(),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )
    client = TestClient(app)

    assert (
        client.post(
            "/ops/api/pauses",
            json={
                "targetType": "provider",
                "target": "exact_evm",
                "paused": True,
                "reason": "provider isolated",
            },
        ).status_code
        == 200
    )

    assert client.get("/supported").status_code == 200
    assert client.post("/verify", json=_envelope(), headers=AUTH).status_code == 423
    assert client.post("/settle", json=_envelope(), headers=AUTH).status_code == 423
    assert provider.supported_calls == 0
    assert provider.verify_calls == 0
    assert provider.settle_calls == 0
    assert store.count() == 0
    assert store.attempt_count() == 0
    assert store_factory_calls["count"] == 1
    assert len(request_stores) == 1
    assert request_stores[0].count() == 0
    assert request_stores[0].attempt_count() == 0


def test_control_plane_pause_redacts_operator_reason_from_audit():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "rotate leaked sk_live_ABC123",
        },
    )
    overview = client.get("/ops/api/overview")

    assert response.status_code == 200
    assert "sk_live" not in response.text
    assert "ABC123" not in response.text
    assert overview.status_code == 200
    assert "sk_live" not in overview.text
    assert "ABC123" not in overview.text
    assert response.json()["event"]["reason"] == "operator_supplied"
    assert response.json()["event"]["correlationId"]
    assert overview.json()["auditTail"][0]["reason"] == "operator_supplied"


def test_control_plane_pause_blocks_cached_settlement_duplicate_retry():
    provider = _Provider()
    store = InMemorySettlementStore()
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        telemetry=_RecordingTelemetry(),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )
    client = TestClient(app)

    first = client.post("/settle", json=_envelope(), headers=AUTH)
    assert first.status_code == 200
    assert first.json()["success"] is True

    pause = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "provider isolated",
        },
    )
    assert pause.status_code == 200

    retry = client.post("/settle", json=_envelope(), headers=AUTH)

    assert retry.status_code == 423
    assert retry.json()["detail"] == "Hosted facilitator route is paused"
    assert provider.settle_calls == 1
    assert store.count() == 1
    assert store.attempt_count() == 1


def test_control_plane_pause_rejects_unsafe_network_target():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "network",
            "target": "postgres:secret-pass.db.internal",
            "paused": True,
            "reason": "bad target",
        },
    )

    assert response.status_code == 400
    assert "secret-pass" not in response.text
    assert "db.internal" not in response.text


def test_control_plane_pause_rejects_unknown_provider_target():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "secret_provider",
            "paused": True,
            "reason": "unknown provider",
        },
    )

    assert response.status_code == 400
    assert "secret_provider" not in response.text


def test_control_plane_writes_disabled_by_default_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _disable_otel_exporters(monkeypatch)

    async def healthy_telemetry(self):
        return {"status": "ok"}

    monkeypatch.setattr(HostedTelemetry, "health_check", healthy_telemetry)
    client = TestClient(_hosted_mode_app())

    overview = client.get("/ops/api/overview")
    assert overview.status_code == 200
    assert overview.json()["pauseState"]["writesEnabled"] is False

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "hosted default disabled",
        },
    )
    assert response.status_code == 503


def test_hosted_mode_rejects_writable_non_durable_control_plane_state(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _disable_otel_exporters(monkeypatch)

    with pytest.raises(RuntimeError, match="durable control-plane state"):
        _hosted_mode_app(control_plane_state=_HostedSafeNonDurableControlState())


def test_hosted_mode_accepts_writable_durable_control_plane_state(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _disable_otel_exporters(monkeypatch)

    async def healthy_telemetry(self):
        return {"status": "ok"}

    monkeypatch.setattr(HostedTelemetry, "health_check", healthy_telemetry)
    client = TestClient(
        _hosted_mode_app(
            control_plane_state=_HostedSafeDurableControlState(),
        )
    )

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "durable control state",
        },
    )

    assert response.status_code == 200
    assert response.json()["pauseState"]["writesEnabled"] is True


def test_control_plane_overview_reports_multiple_settlement_statuses():
    store = InMemorySettlementStore()
    first, _ = store.claim_for_settlement(
        seller_account_id="default",
        fingerprint="fp_unknown",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_unknown",
        raw_requirements={},
    )
    store.mark_unknown(first, error_reason="provider_timeout")
    second, _ = store.claim_for_settlement(
        seller_account_id="default",
        fingerprint="fp_submitted",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_submitted",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "extra": {"name": "USDC"},
            "resource": "https://seller.example.test/private-path",
        },
    )
    store.mark_submitted(
        second,
        transaction="0xsubmitted",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    third, _ = store.claim_for_settlement(
        seller_account_id="default",
        fingerprint="fp_manual",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_manual",
        raw_requirements={},
    )
    third.status = SettlementStatus.MANUAL_REVIEW
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(store=store, telemetry=telemetry))

    response = client.get("/ops/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["settlement"]["records"] == 3
    assert body["settlement"]["unknown"] == 1
    assert body["settlement"]["submitted"] == 1
    assert body["settlement"]["manualReviewBacklog"] == 1
    assert body["providers"]["networks"] == ["eip155:5042002"]
    assert body["components"]["providers"]["networks"] == ["eip155:5042002"]
    assert body["settlement"]["byProviderNetwork"] == [
        {
            "provider": "exact_evm",
            "network": "eip155:5042002",
            "status": "manual_review",
            "count": 1,
        },
        {
            "provider": "exact_evm",
            "network": "eip155:5042002",
            "status": "submitted",
            "count": 1,
        },
        {
            "provider": "exact_evm",
            "network": "eip155:5042002",
            "status": "unknown",
            "count": 1,
        },
    ]
    assert set(body["settlement"]["oldestAgeSeconds"]) == {
        "manual_review",
        "submitted",
        "unknown",
    }
    assert all(age >= 0 for age in body["settlement"]["oldestAgeSeconds"].values())
    recent = body["settlement"]["recentRecords"]
    assert len(recent) == 3
    submitted_record = next(record for record in recent if record["status"] == "submitted")
    assert submitted_record["sellerRef"] == "default"
    assert submitted_record["paymentProfileId"] == "default"
    assert submitted_record["provider"] == "exact_evm"
    assert submitted_record["network"] == "eip155:5042002"
    assert submitted_record["amountAtomic"] == "1000"
    assert submitted_record["amountUsdc"] == "0.001"
    assert submitted_record["payTo"] == "0xbbbb...bbbb"
    assert submitted_record["payer"] == "0xaaaa...aaaa"
    assert submitted_record["transaction"] == "redacted"
    assert submitted_record["rail"] == "USDC"
    rendered = response.text
    assert "fp_unknown" not in rendered
    assert "private-path" not in rendered
    assert "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in rendered
    assert "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in rendered


def test_control_plane_reconciliation_queue_filters_and_sanitizes_records():
    store = InMemorySettlementStore()
    now = datetime.now(timezone.utc)

    unknown, _ = store.claim_for_settlement(
        seller_account_id="alpha-seller",
        fingerprint="fp_queue_unknown",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_queue_unknown",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "2500000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "resource": "https://seller.example.test/secret-resource",
            "extra": {"name": "USDC"},
        },
    )
    store.mark_unknown(unknown, error_reason="provider_timeout")
    unknown.updated_at = now - timedelta(minutes=20)

    submitted, _ = store.claim_for_settlement(
        seller_account_id="beta-seller",
        fingerprint="fp_queue_submitted",
        provider="circle_gateway",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_queue_submitted",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000000",
            "payTo": "0xcccccccccccccccccccccccccccccccccccccccc",
            "extra": {"name": "GatewayWalletBatched"},
        },
    )
    store.mark_submitted(
        submitted,
        transaction="gateway-transfer-secret-reference",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    submitted.updated_at = now - timedelta(minutes=1)

    in_progress, _ = store.claim_for_settlement(
        seller_account_id="alpha-seller",
        fingerprint="fp_queue_in_progress",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_queue_in_progress",
        raw_requirements={},
    )
    in_progress.updated_at = now - timedelta(minutes=30)

    manual, _ = store.claim_for_settlement(
        seller_account_id="alpha-seller",
        fingerprint="fp_queue_manual",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_queue_manual",
        raw_requirements={},
    )
    manual.status = SettlementStatus.MANUAL_REVIEW
    manual.reconciliation_attempts = 2
    manual.reconciliation_owner = "operator-a"
    manual.updated_at = now - timedelta(minutes=40)

    settled, _ = store.claim_for_settlement(
        seller_account_id="alpha-seller",
        fingerprint="fp_queue_settled",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_queue_settled",
        raw_requirements={},
    )
    store.mark_settled(
        settled, transaction="0x" + "44" * 32, payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )

    telemetry = _RecordingTelemetry()
    client = TestClient(_app(store=store, telemetry=telemetry))

    response = client.get(
        "/ops/api/reconciliation?status=unknown&sellerRef=alpha-seller&provider=exact_evm&network=eip155:5042002&minAgeSeconds=300",
        headers=OPS_AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["statuses"] == ["unknown"]
    assert body["filters"]["sellerRef"] == "alpha-seller"
    assert body["filters"]["provider"] == "exact_evm"
    assert body["filters"]["network"] == "eip155:5042002"
    assert body["counts"] == {"unknown": 1}
    assert len(body["items"]) == 1
    assert body["items"][0]["recordId"] == unknown.record_id
    assert body["items"][0]["ageSeconds"] >= 300
    assert body["items"][0]["payTo"] == "0xbbbb...bbbb"
    rendered = response.text
    assert "secret-resource" not in rendered
    assert "fp_queue_unknown" not in rendered
    assert "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in rendered
    assert "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in rendered

    default_response = client.get("/ops/api/reconciliation", headers=OPS_AUTH)
    assert default_response.status_code == 200
    default_body = default_response.json()
    assert default_body["counts"] == {
        "manual_review": 1,
        "settle_in_progress": 1,
        "unknown": 1,
        "submitted": 1,
    }
    assert [item["recordId"] for item in default_body["items"]][:2] == [
        manual.record_id,
        in_progress.record_id,
    ]
    assert all(item["status"] != "settled" for item in default_body["items"])
    manual_item = next(item for item in default_body["items"] if item["status"] == "manual_review")
    assert manual_item["reconciliation"]["attempts"] == 2
    assert manual_item["reconciliation"]["owner"] == "operator-a"
    assert (
        "omniclaw.hosted.http.server.requests",
        {"endpoint": "ops_reconciliation", "status": "200"},
    ) in telemetry.counters


def test_control_plane_reconciliation_queue_rejects_invalid_filters():
    client = TestClient(_app(telemetry=_RecordingTelemetry()))

    invalid_status = client.get("/ops/api/reconciliation?status=settled<script>", headers=OPS_AUTH)
    terminal_status = client.get("/ops/api/reconciliation?status=settled", headers=OPS_AUTH)
    invalid_age = client.get("/ops/api/reconciliation?minAgeSeconds=-1", headers=OPS_AUTH)
    invalid_seller = client.get("/ops/api/reconciliation?sellerRef=-bad", headers=OPS_AUTH)

    assert invalid_status.status_code == 400
    assert invalid_status.headers["cache-control"] == "no-store"
    assert invalid_status.json()["detail"] == "unsupported settlement status: settled<script>"
    assert terminal_status.status_code == 400
    assert terminal_status.headers["cache-control"] == "no-store"
    assert terminal_status.json()["detail"] == "unsupported reconciliation status: settled"
    assert invalid_age.status_code == 400
    assert invalid_age.headers["cache-control"] == "no-store"
    assert invalid_age.json()["detail"] == "minAgeSeconds must be between 0 and 604800"
    assert invalid_seller.status_code == 400
    assert invalid_seller.headers["cache-control"] == "no-store"
    assert "seller reference must start" in invalid_seller.json()["detail"]


def test_control_plane_reconciliation_queue_counts_beyond_page_limit():
    store = InMemorySettlementStore()
    for index in range(55):
        record, _ = store.claim_for_settlement(
            seller_account_id="alpha-seller",
            fingerprint=f"fp_queue_limit_{index}",
            provider="exact_evm",
            scheme="exact",
            network="eip155:5042002",
            trace_id=f"tr_queue_limit_{index}",
            raw_requirements={},
        )
        store.mark_unknown(record, error_reason="provider_timeout")
    client = TestClient(_app(store=store, telemetry=_RecordingTelemetry()))

    response = client.get("/ops/api/reconciliation?status=unknown&limit=50", headers=OPS_AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"unknown": 55}
    assert len(body["items"]) == 50
    assert body["truncated"] is True


def test_control_plane_overview_sanitizes_provider_health_and_settlement_aggregates():
    class _DirtyProvider(_Provider):
        def health_check(self) -> dict[str, Any]:
            return {
                "status": "ok",
                "provider": "exact_evm<script>",
                "network": "https://rpc.example.test/secret",
                "supportedNetworks": (
                    "eip155:5042002",
                    "postgres://secret@db.internal/omniclaw",
                    "circle_gateway:arc-testnet",
                ),
            }

    class _DirtyAggregateStore(_HostedSafeStore):
        def count(self) -> int:
            return 2

        def attempt_count(self) -> int:
            return 2

        def settlement_provider_network_status_counts(self) -> list[dict[str, Any]]:
            return [
                {
                    "provider": "exact_evm<script>",
                    "network": "postgres://secret@db.internal/omniclaw",
                    "status": SettlementStatus.SUBMITTED.value,
                    "count": -9,
                },
                {
                    "provider": "exact_evm",
                    "network": "eip155:5042002",
                    "status": "not_a_status",
                    "count": 99,
                },
            ]

        def settlement_oldest_age_seconds(self) -> dict[str, int]:
            return {
                SettlementStatus.SUBMITTED.value: -20,
                "not_a_status": 99,
            }

    store = _DirtyAggregateStore()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([_DirtyProvider()]),
        store=store,
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=engine,
            resolver=StaticSellerAccountResolver(
                [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
            ),
            telemetry=_RecordingTelemetry(),
            rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    response = client.get("/ops/api/overview")

    assert response.status_code == 200
    body = response.json()
    provider_item = body["providers"]["items"][0]
    assert provider_item["provider"] == "exact_evmscript"
    assert provider_item["supportedNetworks"] == ["eip155:5042002"]
    assert "network" not in provider_item
    assert body["providers"]["networks"] == ["eip155:5042002"]
    assert body["settlement"]["byProviderNetwork"] == [
        {
            "provider": "exact_evmscript",
            "network": "",
            "status": "submitted",
            "count": 0,
        }
    ]
    assert body["settlement"]["oldestAgeSeconds"] == {"submitted": 0}
    rendered = response.text
    assert "secret" not in rendered
    assert "postgres://" not in rendered
    assert "circle_gateway:arc-testnet" not in rendered


def test_readiness_endpoint_uses_oidc_openfga_authorizer():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            operations_authorizer=_RequireOidcOperationsAuthorizer(),
        )
    )

    assert client.get("/readyz").status_code == 401
    assert client.get("/readyz", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/readyz", headers=OPS_AUTH).status_code == 200
    assert client.get("/metrics", headers=OPS_AUTH).status_code == 404


def test_internal_readiness_keeps_operator_readiness_auth_gated():
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            operations_authorizer=_RequireOidcOperationsAuthorizer(),
        )
    )

    assert client.get("/readyz").status_code == 401
    response = client.get("/internal/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_operations_routes_fail_closed_without_oidc_openfga_in_local_mode():
    from hosted_facilitator.hosted.auth import UnconfiguredOperationsAuthorizer

    control_state = InMemoryControlPlaneState()
    client = TestClient(
        _app(
            telemetry=_RecordingTelemetry(),
            operations_authorizer=UnconfiguredOperationsAuthorizer(),
            control_plane_state=control_state,
        )
    )

    assert client.get("/readyz").status_code == 503
    assert client.get("/ops/api/overview").status_code == 503
    pause = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "must fail closed",
        },
    )
    assert pause.status_code == 503
    assert control_state.pause_state().providers == set()
    assert control_state.audit_tail() == []


def test_old_operations_token_env_does_not_enable_operations_routes(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_OPERATIONS_TOKEN", "old-token")
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_API_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", raising=False)
    provider = _Provider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=InMemorySettlementStore(),
    )
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        telemetry=_RecordingTelemetry(),
    )
    client = TestClient(app)

    headers = {"Authorization": "Bearer old-token"}
    assert client.get("/readyz", headers=headers).status_code == 503
    assert client.get("/ops/api/overview", headers=headers).status_code == 503


def test_readiness_requires_oidc_openfga_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _disable_otel_exporters(monkeypatch)

    async def healthy_telemetry(self):
        return {"status": "ok"}

    monkeypatch.setattr(HostedTelemetry, "health_check", healthy_telemetry)
    client = TestClient(_hosted_mode_app(operations_authorizer=_RequireOidcOperationsAuthorizer()))

    assert client.get("/readyz").status_code == 401
    assert client.get("/readyz", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/readyz", headers=OPS_AUTH).status_code == 200


def test_operations_routes_fail_startup_without_oidc_openfga_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_API_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", raising=False)
    _disable_otel_exporters(monkeypatch)
    provider = _Provider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=_HostedSafeStore(),
    )
    resolver = _HostedSafeProjectResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )

    with pytest.raises(Exception, match="OIDC"):
        create_hosted_facilitator_app(
            engine=engine,
            resolver=resolver,
            rate_limiter=_HostedSafeRateLimiter(),
            telemetry_config=_hosted_otel_config(),
        )


def test_hosted_mode_rejects_injected_telemetry(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([_Provider()]),
        store=_HostedSafeStore(),
    )

    try:
        create_hosted_facilitator_app(
            engine=engine,
            resolver=StaticSellerAccountResolver(),
            rate_limiter=_HostedSafeRateLimiter(),
            telemetry=_RecordingTelemetry(),
        )
    except RuntimeError as exc:
        assert "validated hosted OpenTelemetry configuration" in str(exc)
    else:
        raise AssertionError("hosted app accepted injected telemetry")


def test_hosted_otel_config_fails_closed(monkeypatch):
    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_OTEL_REQUIRED"):
        HostedTelemetryConfig(required=False).validate(hosted_mode="staging")

    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        HostedTelemetryConfig(
            required=True,
            collector_health_url="https://otel.example.test/health",
        ).validate(hosted_mode="staging")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL"):
        HostedTelemetryConfig(
            required=True,
            otlp_endpoint="https://otel.example.test",
        ).validate(hosted_mode="staging")

    with pytest.raises(RuntimeError, match="must use https"):
        HostedTelemetryConfig(
            required=True,
            otlp_endpoint="http://otel.example.test",
            collector_health_url="https://otel.example.test/health",
        ).validate(hosted_mode="staging")

    with pytest.raises(RuntimeError, match="must use https"):
        HostedTelemetryConfig(
            required=True,
            otlp_endpoint="http://otel.example.test",
            collector_health_url="https://otel.example.test/health",
            allow_insecure_otlp=True,
        ).validate(hosted_mode="staging")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_JSON_LOGS"):
        HostedTelemetryConfig(
            required=True,
            otlp_endpoint="https://otel.example.test",
            collector_health_url="https://otel.example.test/health",
        ).validate(hosted_mode="staging")

    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
        "authorization",
    )
    with pytest.raises(RuntimeError, match="unsafe telemetry header capture"):
        _hosted_otel_config().validate(hosted_mode="staging")
    monkeypatch.delenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST")

    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_BODY", "true")
    with pytest.raises(RuntimeError, match="not allowed in hosted telemetry"):
        _hosted_otel_config().validate(hosted_mode="staging")


def test_metric_attributes_exclude_resource_and_tenant_labels():
    assert hosted_telemetry._safe_metric_attributes(
        {
            "service.name": "omniclaw-hosted-facilitator",
            "deployment.environment": "production",
            "omniclaw.seller_account_id": "customer-a",
            "omniclaw.tenant_id": "tenant-a",
            "endpoint": "verify",
            "status": "200",
        }
    ) == {"endpoint": "verify", "status": "200"}


def test_malformed_requirement_values_are_not_telemetry_labels():
    telemetry = _RecordingTelemetry()
    client = TestClient(_app(telemetry=telemetry))
    body = _envelope()
    body["paymentRequirements"]["network"] = "postgres:secret-pass.db.internal"
    body["paymentRequirements"]["scheme"] = "secret-key"

    assert client.post("/verify", json=body, headers=AUTH).status_code == 400

    rendered = f"{telemetry.counters} {telemetry.histograms} {telemetry.spans}"
    assert "secret-pass" not in rendered
    assert "postgres:" not in rendered
    assert "secret-key" not in rendered
    assert "db.internal" not in rendered


def test_telemetry_failure_does_not_change_protocol_response():
    client = TestClient(_app(telemetry=_ExplodingTelemetry()))

    assert client.get("/supported").status_code == 200
    assert client.post("/verify", json=_envelope(), headers=AUTH).status_code == 200
    assert client.post("/settle", json=_envelope(), headers=AUTH).status_code == 200


def test_rate_limit_denials_are_counted_without_payload_labels():
    telemetry = _RecordingTelemetry()
    client = TestClient(
        _app(
            telemetry=telemetry,
            policy=RateLimitPolicy(
                verify_per_minute=1,
                seller_requests_per_minute=100,
                ip_requests_per_minute=100,
            ),
        )
    )

    assert (
        client.post(
            "/verify", json=_envelope("https://seller.example.com/one"), headers=AUTH
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/verify", json=_envelope("https://seller.example.com/two"), headers=AUTH
        ).status_code
        == 429
    )

    decisions = [
        attrs
        for name, attrs in telemetry.counters
        if name == "omniclaw.hosted.rate_limit.decisions"
    ]
    assert {
        "endpoint": "verify",
        "decision": "denied",
    } in decisions
    assert "seller.example.com" not in str(telemetry.counters)
