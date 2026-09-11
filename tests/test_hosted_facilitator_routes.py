from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from hosted_facilitator.exact import ExactFacilitatorConfig
from hosted_facilitator.hosted.app import (
    create_hosted_exact_facilitator_app,
    create_hosted_facilitator_app,
)
from hosted_facilitator.hosted.auth import AuthorizationDeniedError, OperationsPermission, Principal
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import _safe_operator_lease_owner
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.fingerprint import payment_fingerprint
from hosted_facilitator.hosted.limits import (
    InMemoryFixedWindowRateLimiter,
    RateLimitDecision,
    RateLimitPolicy,
)
from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.providers.exact_evm import HostedExactProvider
from hosted_facilitator.hosted.reconciliation import ReconciliationStatus
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import (
    FacilitatorEnvelope,
    ProviderSettlementStatus,
    SettleOutcome,
    VerifyOutcome,
)
from hosted_facilitator.hosted.signers import (
    HostedSignerConfig,
    HostedSignerGuard,
    HostedSignerStatus,
)
from hosted_facilitator.hosted.storage import (
    InMemorySettlementStore,
    SettlementAttemptStatus,
    SettlementRecord,
    SettlementStatus,
    SqliteSettlementStore,
)
from hosted_facilitator.hosted.telemetry import HostedTelemetryConfig
from hosted_facilitator.hosted.tenancy import (
    PaymentProfileConfig,
    SellerAccountConfig,
    StaticSellerAccountResolver,
)

AUTH = {"Authorization": "Bearer secret-key"}
RATE_LIMIT_DETAIL = "Hosted facilitator rate limit exceeded"


class _AllowAllOperationsAuthorizer:
    hosted_safe = True

    async def authorize(self, *_args, **_kwargs):
        return Principal(subject="ops-user", issuer="https://idp.example.test")


class _DenyOperationsAuthorizer:
    hosted_safe = True

    def __init__(self):
        self.permissions: list[OperationsPermission] = []

    async def authorize(self, _authorization_header, permission: OperationsPermission, *_args):
        self.permissions.append(permission)
        raise AuthorizationDeniedError("authorization denied")


class FakeProvider(HostedFacilitatorProvider):
    name = "exact_evm"
    supported_networks = ("eip155:5042002",)

    def __init__(self):
        self.settle_calls = 0
        self.verify_calls = 0

    async def supported(self, context: RequestContext) -> list[dict]:
        return [
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }
        ]

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
            transaction="0xsettled",
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


class _CloseTrackingStore(InMemorySettlementStore):
    def __init__(self):
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _HostedSafeMemoryStore(InMemorySettlementStore):
    hosted_safe = True


class _HostedSafeReconciliationStore(_HostedSafeMemoryStore):
    def claim_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
        lease_until,
        eligible_statuses: tuple[SettlementStatus, ...],
        stale_before=None,
    ):
        record = self.get_record_by_id(record_id)
        if record is None or record.status not in eligible_statuses:
            return None
        if record.reconciliation_owner and record.reconciliation_owner != owner:
            return None
        if stale_before is not None and record.updated_at > stale_before:
            return None
        record.reconciliation_owner = owner
        record.reconciliation_lease_until = lease_until
        record.reconciliation_attempts += 1
        return record

    def release_reconciliation_record(self, *, record_id: int, owner: str):
        record = self.get_record_by_id(record_id)
        if record is None or record.reconciliation_owner != owner:
            return None
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None
        return record

    def mark_manual_review(self, record: SettlementRecord, *, error_reason: str | None, owner: str):
        if record.reconciliation_owner != owner:
            raise RuntimeError("record is not leased by owner")
        if record.status != SettlementStatus.UNKNOWN:
            raise RuntimeError("record is not unknown")
        record.status = SettlementStatus.MANUAL_REVIEW
        record.error_reason = error_reason
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None


class _HostedSafeProjectResolver(StaticSellerAccountResolver):
    hosted_safe = True


class _AllowingHostedRateLimiter:
    hosted_safe = True

    async def check(self, _request):
        return RateLimitDecision(allowed=True)


class _DenyAfterFirstRateLimiter:
    hosted_safe = True

    def __init__(self):
        self.calls = 0

    async def check(self, _request):
        self.calls += 1
        if self.calls == 1:
            return RateLimitDecision(allowed=True)
        return RateLimitDecision(
            allowed=False,
            retry_after_seconds=60,
            limit=1,
            remaining=0,
            reset_after_seconds=60,
        )


class _LifecycleRateLimiter(_AllowingHostedRateLimiter):
    def __init__(self):
        self.initialized = False
        self.closed = False

    async def initialize(self):
        self.initialized = True

    async def close(self):
        self.closed = True


class SlowProvider(FakeProvider):
    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        self.settle_calls += 1
        await asyncio.sleep(0.05)
        return SettleOutcome(
            success=True,
            transaction="0xslowsettled",
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


class FailingProvider(FakeProvider):
    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        self.settle_calls += 1
        raise TimeoutError("provider did not return a settlement outcome")


class SubmittedProvider(FakeProvider):
    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        self.settle_calls += 1
        return SettleOutcome(
            success=False,
            transaction="0xsubmitted",
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            settlementStatus=ProviderSettlementStatus.SUBMITTED,
        )


class _FakeResult:
    def __init__(self, data):
        self._data = data

    def model_dump(self, by_alias: bool = True, exclude_none: bool = True):
        return dict(self._data)


class _FakeExactFacilitator:
    def get_supported(self):
        return _FakeResult(
            {"kinds": [{"x402Version": 2, "scheme": "exact", "network": "eip155:5042002"}]}
        )

    async def verify(self, payload, requirements):
        return _FakeResult({"isValid": True, "payer": "0xarc"})

    async def settle(self, payload, requirements):
        return _FakeResult(
            {
                "success": True,
                "transaction": "0xarcsettled",
                "network": "eip155:5042002",
                "payer": "0xarc",
            }
        )


class _MalformedExactFacilitator(_FakeExactFacilitator):
    def __init__(self):
        self.settle_calls = 0

    async def settle(self, payload, requirements):
        self.settle_calls += 1
        return _FakeResult({"network": "eip155:5042002", "payer": "0xarc"})


class _FailedExactFacilitator(_FakeExactFacilitator):
    def __init__(self, error_reason):
        self.error_reason = error_reason

    async def settle(self, payload, requirements):
        data = {"success": False, "network": "eip155:5042002", "payer": "0xarc"}
        if self.error_reason is not None:
            data["errorReason"] = self.error_reason
        return _FakeResult(data)


class _SlowExactFacilitator(_FakeExactFacilitator):
    async def verify(self, payload, requirements):
        await asyncio.sleep(1)
        return await super().verify(payload, requirements)

    async def settle(self, payload, requirements):
        await asyncio.sleep(1)
        return await super().settle(payload, requirements)


class _ConcurrentExactFacilitator(_FakeExactFacilitator):
    def __init__(self):
        self.active_settles = 0
        self.max_active_settles = 0
        self.settle_calls = 0

    async def settle(self, payload, requirements):
        self.settle_calls += 1
        self.active_settles += 1
        self.max_active_settles = max(self.max_active_settles, self.active_settles)
        await asyncio.sleep(0.02)
        self.active_settles -= 1
        return await super().settle(payload, requirements)


class _DelegatingExactFacilitator(_FakeExactFacilitator):
    async def reconcile_settlement(self, record):
        return {"status": "settled", "transaction": record.transaction, "payer": record.payer}


class _BalanceExactFacilitator(_FakeExactFacilitator):
    def __init__(self, balance_wei: int):
        self.settle_calls = 0
        self._signer = _BalanceSigner(balance_wei)

    async def settle(self, payload, requirements):
        self.settle_calls += 1
        return await super().settle(payload, requirements)


class _BalanceSigner:
    def __init__(self, balance_wei: int):
        self._account = type(
            "_Account", (), {"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        )()
        self._w3 = type("_W3", (), {"eth": _BalanceEth(balance_wei)})()


class _BalanceEth:
    def __init__(self, balance_wei: int):
        self._balance_wei = balance_wei

    def get_balance(self, _address):
        return self._balance_wei


def _client(
    provider: FakeProvider | None = None,
) -> tuple[TestClient, FakeProvider, InMemorySettlementStore]:
    provider = provider or FakeProvider()
    store = InMemorySettlementStore()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
            SellerAccountConfig(
                seller_account_id="corey-alpha",
                api_keys=("corey-secret-key",),
            ),
        ]
    )
    return (
        TestClient(
            create_hosted_facilitator_app(
                engine=engine,
                resolver=resolver,
                operations_authorizer=_AllowAllOperationsAuthorizer(),
            )
        ),
        provider,
        store,
    )


def test_supported_is_scoped_to_api_key_payment_profile():
    provider = FakeProvider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=InMemorySettlementStore(),
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="arc-main",
                        seller_account_id="seller-a",
                        api_keys=("arc-profile-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                    PaymentProfileConfig(
                        payment_profile_id="locked-profile",
                        seller_account_id="seller-a",
                        api_keys=("locked-profile-key",),
                        allowed_pay_to=("0xcccccccccccccccccccccccccccccccccccccccc",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=engine,
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    allowed = client.get("/supported", headers={"Authorization": "Bearer arc-profile-key"})
    denied = client.get("/supported", headers={"Authorization": "Bearer locked-profile-key"})

    assert allowed.status_code == 200
    assert len(allowed.json()["kinds"]) == 1
    assert denied.status_code == 200
    assert denied.json()["kinds"] == []


def test_static_resolver_rejects_duplicate_profile_api_keys():
    with pytest.raises(ValueError, match="Duplicate seller API key configured"):
        StaticSellerAccountResolver(
            [
                SellerAccountConfig(
                    seller_account_id="seller-a",
                    payment_profiles=(
                        PaymentProfileConfig(
                            payment_profile_id="profile-a",
                            seller_account_id="seller-a",
                            api_keys=("shared-key",),
                        ),
                        PaymentProfileConfig(
                            payment_profile_id="profile-b",
                            seller_account_id="seller-a",
                            api_keys=("shared-key",),
                        ),
                    ),
                )
            ]
        )


def test_verify_and_settle_enforce_api_key_payment_profile_policy():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="allowed",
                        seller_account_id="seller-a",
                        api_keys=("allowed-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                    PaymentProfileConfig(
                        payment_profile_id="denied",
                        seller_account_id="seller-a",
                        api_keys=("denied-key",),
                        allowed_pay_to=("0xcccccccccccccccccccccccccccccccccccccccc",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    verify = client.post(
        "/verify", json=_envelope(), headers={"Authorization": "Bearer denied-key"}
    )
    settle = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer denied-key"}
    )
    allowed = client.post(
        "/verify", json=_envelope(), headers={"Authorization": "Bearer allowed-key"}
    )

    assert verify.status_code == 403
    assert settle.status_code == 403
    assert allowed.status_code == 200
    assert provider.verify_calls == 1
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_settle_duplicate_is_blocked_across_profiles_for_same_authorization():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="profile-a",
                        seller_account_id="seller-a",
                        api_keys=("profile-a-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                    PaymentProfileConfig(
                        payment_profile_id="profile-b",
                        seller_account_id="seller-a",
                        api_keys=("profile-b-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    first = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-a-key"}
    )
    second = client.post(
        "/settle",
        json=_envelope("https://seller.example.com/compute-b"),
        headers={"Authorization": "Bearer profile-b-key"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_cross_profile_duplicate_still_enforces_current_profile_policy():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="profile-a",
                        seller_account_id="seller-a",
                        api_keys=("profile-a-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                    PaymentProfileConfig(
                        payment_profile_id="profile-b",
                        seller_account_id="seller-a",
                        api_keys=("profile-b-key",),
                        allowed_pay_to=("0xcccccccccccccccccccccccccccccccccccccccc",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    first = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-a-key"}
    )
    second = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-b-key"}
    )

    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json()["detail"] == "Seller policy denied route"
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_cross_profile_duplicate_settlement_bypasses_settle_rate_limit():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    one_settle = RateLimitPolicy(
        seller_requests_per_minute=100,
        verify_per_minute=100,
        settle_per_minute=1,
        invalid_requests_per_minute=100,
        ip_requests_per_minute=100,
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="profile-a",
                        seller_account_id="seller-a",
                        api_keys=("profile-a-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                        rate_limits=one_settle,
                    ),
                    PaymentProfileConfig(
                        payment_profile_id="profile-b",
                        seller_account_id="seller-a",
                        api_keys=("profile-b-key",),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                        rate_limits=one_settle,
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    first = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-a-key"}
    )
    second = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-b-key"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert provider.settle_calls == 1


def test_profile_specific_supported_rate_limit_blocks_before_provider_call():
    provider = FakeProvider()
    policy = RateLimitPolicy(
        seller_requests_per_minute=100,
        supported_per_minute=1,
        verify_per_minute=100,
        settle_per_minute=100,
        ip_requests_per_minute=100,
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="limited",
                        seller_account_id="seller-a",
                        api_keys=("limited-profile-key",),
                        rate_limits=policy,
                    ),
                ),
            )
        ]
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(
            router=ProviderRouter([provider]),
            store=InMemorySettlementStore(),
        ),
        resolver=resolver,
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )
    client = TestClient(app)

    first = client.get("/supported", headers={"Authorization": "Bearer limited-profile-key"})
    second = client.get("/supported", headers={"Authorization": "Bearer limited-profile-key"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.verify_calls == 0
    assert provider.settle_calls == 0


def test_settle_duplicate_is_blocked_across_rotated_keys_on_same_profile():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="profile-a",
                        seller_account_id="seller-a",
                        api_keys=("profile-a-key-1", "profile-a-key-2"),
                        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    first = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-a-key-1"}
    )
    second = client.post(
        "/settle", json=_envelope(), headers={"Authorization": "Bearer profile-a-key-2"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_disabled_payment_profile_fails_before_provider_or_store_use():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="seller-a",
                payment_profiles=(
                    PaymentProfileConfig(
                        payment_profile_id="disabled",
                        seller_account_id="seller-a",
                        status="disabled",
                        api_keys=("disabled-key",),
                    ),
                ),
            )
        ]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
            resolver=resolver,
            operations_authorizer=_AllowAllOperationsAuthorizer(),
        )
    )

    response = client.post(
        "/verify", json=_envelope(), headers={"Authorization": "Bearer disabled-key"}
    )

    assert response.status_code == 401
    assert provider.verify_calls == 0
    assert store.count() == 0


def _hosted_otel_config() -> HostedTelemetryConfig:
    return HostedTelemetryConfig(
        environment="staging",
        required=True,
        otlp_endpoint="https://otel.example.test",
        collector_health_url="https://otel.example.test/health",
        json_logs_required=True,
    )


def _hosted_oidc_openfga_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", "omniclaw-ops")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_URL", "https://openfga.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", "store-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", "model-id")


def _rate_limited_client(
    policy: RateLimitPolicy,
    *,
    provider: FakeProvider | None = None,
    clock_value: float = 0.0,
) -> tuple[TestClient, FakeProvider, InMemorySettlementStore]:
    provider = provider or FakeProvider()
    store = InMemorySettlementStore()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="default", api_keys=("secret-key",), rate_limits=policy
            )
        ]
    )
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=resolver,
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: clock_value),
    )
    return TestClient(app), provider, store


def _store_factory_client(
    provider: FakeProvider | None = None,
) -> tuple[TestClient, FakeProvider, list[_CloseTrackingStore], dict[str, int]]:
    provider = provider or FakeProvider()
    stores: list[_CloseTrackingStore] = []
    calls = {"count": 0}

    def store_factory() -> _CloseTrackingStore:
        calls["count"] += 1
        store = _CloseTrackingStore()
        stores.append(store)
        return store

    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=InMemorySettlementStore(),
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=resolver,
        store_factory=store_factory,
    )
    return TestClient(app), provider, stores, calls


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


def _settlement_record(
    *,
    transaction: str | None,
    status: SettlementStatus = SettlementStatus.SUBMITTED,
) -> SettlementRecord:
    return SettlementRecord(
        record_id=1,
        seller_account_id="default",
        fingerprint="fp-arc-reconcile",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        status=status,
        transaction=transaction,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        raw_requirements=_envelope()["paymentRequirements"],
    )


def _transfer_receipt(
    *,
    status: int | str = 1,
    transaction: str = "0xarcsettled",
    asset: str = "0x3600000000000000000000000000000000000000",
    payer: str = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    pay_to: str = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    amount: int = 250000,
) -> dict:
    return {
        "status": status,
        "transactionHash": transaction,
        "logs": [
            {
                "address": asset,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + payer[2:],
                    "0x" + "0" * 24 + pay_to[2:],
                ],
                "data": hex(amount),
            }
        ],
    }


def test_supported_route_aliases_return_same_kinds():
    client, _, _ = _client()

    for path in ("/supported", "/v1/x402/supported", "/v2/x402/supported"):
        response = client.get(path, headers={"x-request-id": "tr_test"})
        assert response.status_code == 200
        body = response.json()
        assert body["traceId"] == "tr_test"
        assert body["kinds"][0]["network"] == "eip155:5042002"


def test_supported_is_filtered_by_project_policy():
    provider = FakeProvider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=InMemorySettlementStore(),
    )
    resolver = StaticSellerAccountResolver(
        [
            SellerAccountConfig(
                seller_account_id="default",
                enabled_networks=("eip155:1",),
                allowed_assets=("0x3600000000000000000000000000000000000000",),
                allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
            )
        ]
    )
    client = TestClient(create_hosted_facilitator_app(engine=engine, resolver=resolver))

    response = client.get("/supported")

    assert response.status_code == 200
    assert response.json()["kinds"] == []


def test_verify_and_settle_route_aliases_work():
    client, provider, _ = _client()

    verify = client.post(
        "/v2/x402/verify",
        json=_envelope(),
        headers={**AUTH, "x-request-id": "tr_v"},
    )
    assert verify.status_code == 200
    assert verify.json()["isValid"] is True
    assert verify.json()["provider"] == "exact_evm"

    settle = client.post(
        "/v1/x402/settle",
        json=_envelope(),
        headers={**AUTH, "x-request-id": "tr_s"},
    )
    assert settle.status_code == 200
    assert settle.json()["success"] is True
    assert settle.json()["transaction"] == "0xsettled"
    assert provider.settle_calls == 1


def test_supported_and_verify_do_not_open_request_scoped_store():
    client, provider, stores, calls = _store_factory_client()

    supported = client.get("/supported")
    verify = client.post("/verify", json=_envelope(), headers=AUTH)

    assert supported.status_code == 200
    assert verify.status_code == 200
    assert provider.verify_calls == 1
    assert calls["count"] == 0
    assert stores == []


def test_settle_closes_request_scoped_store_on_success():
    client, provider, stores, calls = _store_factory_client()

    response = client.post("/settle", json=_envelope(), headers=AUTH)

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert provider.settle_calls == 1
    assert calls["count"] == 1
    assert stores[0].close_calls == 1


def test_settle_closes_request_scoped_store_on_handled_error():
    client, provider, stores, calls = _store_factory_client()
    body = _envelope()
    body["paymentPayload"]["accepted"]["payTo"] = "0xcccccccccccccccccccccccccccccccccccccccc"

    response = client.post("/settle", json=body, headers=AUTH)

    assert response.status_code == 400
    assert "paymentPayload.accepted.payTo" in response.json()["detail"]
    assert provider.settle_calls == 0
    assert calls["count"] == 1
    assert stores[0].count() == 0
    assert stores[0].close_calls == 1


def test_settle_duplicate_returns_cached_result_without_second_provider_call():
    client, provider, _ = _client()

    first = client.post("/settle", json=_envelope(), headers={**AUTH, "x-request-id": "tr_1"})
    second = client.post("/settle", json=_envelope(), headers={**AUTH, "x-request-id": "tr_2"})

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["transaction"] == "0xsettled"
    assert provider.settle_calls == 1


def test_submitted_settlement_duplicate_preserves_submission_proof():
    client, provider, _ = _client(SubmittedProvider())

    first = client.post("/settle", json=_envelope(), headers={**AUTH, "x-request-id": "tr_1"})
    second = client.post("/settle", json=_envelope(), headers={**AUTH, "x-request-id": "tr_2"})

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert first.json()["settlementStatus"] == "submitted"
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["transaction"] == "0xsubmitted"
    assert second.json()["settlementStatus"] == "submitted"
    assert second.json()["errorReason"] == "settlement_submitted"
    assert provider.settle_calls == 1


def test_settle_records_attempt_before_provider_call():
    client, provider, store = _client()

    response = client.post("/settle", json=_envelope(), headers=AUTH)

    assert response.status_code == 200
    assert provider.settle_calls == 1
    assert store.count() == 1
    assert store.attempt_count() == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_settle_claim_calls_provider_once():
    provider = SlowProvider()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=InMemorySettlementStore(),
    )
    project = SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))
    context = RequestContext(trace_id="tr_concurrent", seller_account=project)
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    results = await asyncio.gather(*(engine.settle(envelope, context) for _ in range(10)))

    assert provider.settle_calls == 1
    assert sum(1 for result in results if result.success) == 1
    assert sum(1 for result in results if result.duplicate) == 9
    assert {result.error_reason for result in results if result.duplicate} == {
        "settlement_in_progress"
    }


def test_provider_exception_marks_settlement_unknown():
    provider = FailingProvider()
    store = InMemorySettlementStore()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    client = TestClient(create_hosted_facilitator_app(engine=engine, resolver=resolver))
    body = _envelope()

    response = client.post("/settle", json=body, headers=AUTH)
    fingerprint = payment_fingerprint(
        payment_payload=body["paymentPayload"],
        payment_requirements=body["paymentRequirements"],
        provider="exact_evm",
        x402_version=2,
    )
    record = store.get("default", fingerprint)

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["errorReason"] == "settlement_outcome_unknown"
    assert provider.settle_calls == 1
    assert store.attempt_count() == 1
    assert record is not None
    assert record.status == SettlementStatus.UNKNOWN


def test_provider_malformed_response_marks_settlement_unknown_before_retry():
    store = InMemorySettlementStore()
    exact = _MalformedExactFacilitator()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([HostedExactProvider(exact)]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    client = TestClient(create_hosted_facilitator_app(engine=engine, resolver=resolver))
    body = _envelope()

    response = client.post("/settle", json=body, headers=AUTH)
    retry = client.post("/settle", json=body, headers=AUTH)
    fingerprint = payment_fingerprint(
        payment_payload=body["paymentPayload"],
        payment_requirements=body["paymentRequirements"],
        provider="exact_evm",
        x402_version=2,
    )
    record = store.get("default", fingerprint)

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["settlementStatus"] == "unknown"
    assert response.json()["errorReason"] == "malformed_provider_settlement_response"
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["errorReason"] == "settlement_outcome_unknown"
    assert exact.settle_calls == 1
    assert store.attempt_count() == 1
    assert record is not None
    assert record.status == SettlementStatus.UNKNOWN


def test_paused_exact_signer_blocks_settle_before_claim_and_provider_call():
    exact = _BalanceExactFacilitator(balance_wei=10**18)
    provider = HostedExactProvider(
        {"eip155:5042002": exact},
        signer_guard=HostedSignerGuard(
            HostedSignerConfig(
                signer_id="hosted-exact-alpha",
                provider="exact_evm",
                network="evm",
                environment="test",
                status=HostedSignerStatus.PAUSED,
            )
        ),
    )
    client, _, store = _client(provider)

    response = client.post("/settle", json=_envelope(), headers=AUTH)

    assert response.status_code == 503
    assert response.json()["detail"] == "Provider unavailable"
    assert exact.settle_calls == 0
    assert store.count() == 0
    assert store.attempt_count() == 0


def test_low_gas_exact_signer_blocks_settle_before_claim_and_provider_call():
    exact = _BalanceExactFacilitator(balance_wei=100)
    provider = HostedExactProvider(
        {"eip155:5042002": exact},
        signer_guard=HostedSignerGuard(
            HostedSignerConfig(
                signer_id="hosted-exact-alpha",
                provider="exact_evm",
                network="evm",
                environment="test",
                min_native_balance_wei=1000,
            )
        ),
    )
    client, _, store = _client(provider)

    response = client.post("/settle", json=_envelope(), headers=AUTH)

    assert response.status_code == 503
    assert response.json()["detail"] == "Provider unavailable"
    assert exact.settle_calls == 0
    assert store.count() == 0
    assert store.attempt_count() == 0


@pytest.mark.asyncio
async def test_exact_provider_serializes_concurrent_signer_submissions():
    exact = _ConcurrentExactFacilitator()
    provider = HostedExactProvider(
        {"eip155:5042002": exact},
        signer_guard=HostedSignerGuard(
            HostedSignerConfig(
                signer_id="hosted-exact-alpha",
                provider="exact_evm",
                network="evm",
                environment="test",
            )
        ),
        max_concurrent_settlements=2,
    )
    context = RequestContext(
        trace_id="tr-concurrent",
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )

    results = await asyncio.gather(
        provider.settle(FacilitatorEnvelope.model_validate(_envelope()), context),
        provider.settle(FacilitatorEnvelope.model_validate(_envelope()), context),
    )

    assert [result.success for result in results] == [True, True]
    assert exact.settle_calls == 2
    assert exact.max_active_settles == 1


def test_exact_provider_rejects_invalid_concurrency_limit():
    with pytest.raises(ValueError, match="max_concurrent_settlements"):
        HostedExactProvider(
            {"eip155:5042002": _FakeExactFacilitator()}, max_concurrent_settlements=0
        )


def test_readiness_reports_sanitized_low_gas_network_health():
    exact = _BalanceExactFacilitator(balance_wei=100)
    provider = HostedExactProvider(
        {"eip155:5042002": exact},
        signer_guard=HostedSignerGuard(
            HostedSignerConfig(
                signer_id="hosted-exact-alpha",
                provider="exact_evm",
                network="evm",
                environment="test",
                min_native_balance_wei=1000,
            )
        ),
    )
    client, _, _ = _client(provider)

    response = client.get("/readyz")

    assert response.status_code == 503
    providers = response.json()["components"]["providers"]
    assert providers["status"] == "unhealthy"
    assert providers["items"][0]["networkCount"] == 1
    assert providers["items"][0]["unhealthyNetworkCount"] == 1
    assert providers["items"][0]["maxConcurrentSettlements"] == 1
    assert providers["items"][0]["signerLockScope"] == "network"
    assert providers["items"][0]["network"] == "eip155:5042002"
    assert providers["items"][0]["errorType"] == "SignerGasBelowMinimum"
    assert "hosted-exact-alpha" not in response.text


@pytest.mark.asyncio
async def test_exact_evm_false_timeout_settlement_is_unknown():
    provider = HostedExactProvider(_FailedExactFacilitator("rpc timeout waiting for receipt"))
    context = RequestContext(
        trace_id="tr_timeout",
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )

    outcome = await provider.settle(FacilitatorEnvelope.model_validate(_envelope()), context)

    assert outcome.success is False
    assert outcome.settlement_status == ProviderSettlementStatus.UNKNOWN
    assert outcome.error_reason == "rpc timeout waiting for receipt"


@pytest.mark.asyncio
async def test_exact_evm_provider_verify_timeout_fails_closed():
    provider = HostedExactProvider(_SlowExactFacilitator(), timeout_seconds=0.01)
    context = RequestContext(
        trace_id="tr_verify_timeout",
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )

    with pytest.raises(RuntimeError, match="exact_evm_verify_timeout"):
        await provider.verify(FacilitatorEnvelope.model_validate(_envelope()), context)


@pytest.mark.asyncio
async def test_exact_evm_provider_settle_timeout_is_unknown():
    provider = HostedExactProvider(_SlowExactFacilitator(), timeout_seconds=0.01)
    context = RequestContext(
        trace_id="tr_settle_timeout",
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )

    outcome = await provider.settle(FacilitatorEnvelope.model_validate(_envelope()), context)

    assert outcome.success is False
    assert outcome.settlement_status == ProviderSettlementStatus.UNKNOWN
    assert outcome.error_reason == "provider_timeout"


@pytest.mark.asyncio
async def test_exact_evm_false_final_failure_stays_final():
    provider = HostedExactProvider(_FailedExactFacilitator("invalid payment signature"))
    context = RequestContext(
        trace_id="tr_final",
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )

    outcome = await provider.settle(FacilitatorEnvelope.model_validate(_envelope()), context)

    assert outcome.success is False
    assert outcome.settlement_status == ProviderSettlementStatus.FAILED_FINAL
    assert outcome.error_reason == "invalid payment signature"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_settles_successful_receipt():
    calls = []

    def receipt_checker(transaction, record):
        calls.append((transaction, record.fingerprint))
        return _transfer_receipt(transaction=transaction)

    provider = HostedExactProvider(_FakeExactFacilitator(), receipt_checker=receipt_checker)

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="arcsettled"))

    assert outcome.status == ReconciliationStatus.SETTLED
    assert outcome.transaction == "0xarcsettled"
    assert outcome.payer == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert calls == [("0xarcsettled", "fp-arc-reconcile")]


@pytest.mark.asyncio
async def test_exact_evm_reconcile_parses_json_rpc_hex_success_status():
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda transaction, _: _transfer_receipt(
            status="0x1", transaction=transaction
        ),
    )

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xarcsettled"))

    assert outcome.status == ReconciliationStatus.SETTLED


@pytest.mark.asyncio
async def test_exact_evm_reconcile_failed_submitted_receipt_becomes_unknown_first():
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda *_: _transfer_receipt(status=0, transaction="0xfailed"),
    )

    outcome = await provider.reconcile_settlement(
        _settlement_record(transaction="0xfailed", status=SettlementStatus.SUBMITTED)
    )

    assert outcome.status == ReconciliationStatus.UNKNOWN
    assert outcome.transaction == "0xfailed"
    assert outcome.error_reason == "exact_evm_transaction_failed"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_parses_json_rpc_hex_failed_status():
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda *_: _transfer_receipt(status="0x0", transaction="0xfailed"),
    )

    outcome = await provider.reconcile_settlement(
        _settlement_record(transaction="0xfailed", status=SettlementStatus.SUBMITTED)
    )

    assert outcome.status == ReconciliationStatus.UNKNOWN
    assert outcome.error_reason == "exact_evm_transaction_failed"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_failed_unknown_receipt_is_final_failure():
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda *_: _transfer_receipt(status=0, transaction="0xfailed"),
    )

    outcome = await provider.reconcile_settlement(
        _settlement_record(transaction="0xfailed", status=SettlementStatus.UNKNOWN)
    )

    assert outcome.status == ReconciliationStatus.FAILED_FINAL
    assert outcome.transaction == "0xfailed"
    assert outcome.error_reason == "exact_evm_transaction_failed"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_without_transaction_stays_pending():
    provider = HostedExactProvider(
        _FakeExactFacilitator(), receipt_checker=lambda *_: _transfer_receipt()
    )

    outcome = await provider.reconcile_settlement(_settlement_record(transaction=None))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.transaction is None
    assert outcome.error_reason == "exact_evm_transaction_unavailable"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_missing_receipt_stays_pending():
    provider = HostedExactProvider(_FakeExactFacilitator(), receipt_checker=lambda *_: None)

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xpending"))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.transaction == "0xpending"
    assert outcome.error_reason == "exact_evm_receipt_not_found"


@pytest.mark.asyncio
async def test_exact_evm_delegated_settled_outcome_still_requires_receipt_proof():
    provider = HostedExactProvider(
        _DelegatingExactFacilitator(), receipt_checker=lambda *_: {"status": 1}
    )

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xarcsettled"))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.transaction == "0xarcsettled"
    assert outcome.error_reason == "exact_evm_receipt_proof_unavailable"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_successful_receipt_without_matching_transfer_stays_pending():
    provider = HostedExactProvider(
        _FakeExactFacilitator(), receipt_checker=lambda *_: {"status": 1}
    )

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xarcsettled"))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.transaction == "0xarcsettled"
    assert outcome.error_reason == "exact_evm_receipt_proof_unavailable"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_mismatched_receipt_hash_stays_pending():
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda *_: _transfer_receipt(transaction="0xother"),
    )

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xarcsettled"))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.error_reason == "exact_evm_receipt_transaction_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SettlementStatus.SUBMITTED, SettlementStatus.UNKNOWN])
async def test_exact_evm_reconcile_mismatched_failed_receipt_hash_stays_pending(status):
    provider = HostedExactProvider(
        _FakeExactFacilitator(),
        receipt_checker=lambda *_: _transfer_receipt(status=0, transaction="0xother"),
    )

    outcome = await provider.reconcile_settlement(
        _settlement_record(transaction="0xfailed", status=status)
    )

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.error_reason == "exact_evm_receipt_transaction_mismatch"


@pytest.mark.asyncio
async def test_exact_evm_reconcile_direct_receipt_not_found_stays_pending():
    class _ReceiptMissingExact(_FakeExactFacilitator):
        def get_transaction_receipt(self, transaction):
            raise LookupError(f"transaction {transaction} not found")

    provider = HostedExactProvider(_ReceiptMissingExact())

    outcome = await provider.reconcile_settlement(_settlement_record(transaction="0xmissing"))

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.error_reason == "exact_evm_receipt_not_found"


def test_sqlite_store_persists_cached_settlement_across_instances(tmp_path):
    db_path = str(tmp_path / "settlements.sqlite3")
    first_provider = FakeProvider()
    first_store = SqliteSettlementStore(db_path)
    first_engine = HostedFacilitatorEngine(
        router=ProviderRouter([first_provider]),
        store=first_store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    first_client = TestClient(create_hosted_facilitator_app(engine=first_engine, resolver=resolver))

    first = first_client.post("/settle", json=_envelope(), headers=AUTH)
    first_store.close()

    second_provider = FakeProvider()
    second_store = SqliteSettlementStore(db_path)
    second_engine = HostedFacilitatorEngine(
        router=ProviderRouter([second_provider]),
        store=second_store,
    )
    second_client = TestClient(
        create_hosted_facilitator_app(engine=second_engine, resolver=resolver)
    )

    second = second_client.post("/settle", json=_envelope(), headers=AUTH)
    fingerprint = payment_fingerprint(
        payment_payload=_envelope()["paymentPayload"],
        payment_requirements=_envelope()["paymentRequirements"],
        provider="exact_evm",
        x402_version=2,
    )
    record = second_store.get("default", fingerprint)

    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["transaction"] == "0xsettled"
    assert first_provider.settle_calls == 1
    assert second_provider.settle_calls == 0
    assert second_store.count() == 1
    assert second_store.attempt_count() == 1
    assert record is not None
    assert "resource" not in record.raw_requirements
    assert "description" not in record.raw_requirements
    assert "resourceHash" in record.raw_requirements
    assert record.raw_requirements["metadataRedacted"] is True
    second_store.close()


def test_sqlite_unknown_settlement_persists_and_blocks_retry(tmp_path):
    db_path = str(tmp_path / "settlements.sqlite3")
    first_provider = FailingProvider()
    first_store = SqliteSettlementStore(db_path)
    first_engine = HostedFacilitatorEngine(
        router=ProviderRouter([first_provider]),
        store=first_store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    first_client = TestClient(create_hosted_facilitator_app(engine=first_engine, resolver=resolver))
    body = _envelope()

    first = first_client.post("/settle", json=body, headers=AUTH)
    first_store.close()

    second_provider = FakeProvider()
    second_store = SqliteSettlementStore(db_path)
    second_engine = HostedFacilitatorEngine(
        router=ProviderRouter([second_provider]),
        store=second_store,
    )
    second_client = TestClient(
        create_hosted_facilitator_app(engine=second_engine, resolver=resolver)
    )

    second = second_client.post("/settle", json=body, headers=AUTH)

    assert first.status_code == 200
    assert first.json()["settlementStatus"] == "unknown"
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["errorReason"] == "settlement_outcome_unknown"
    assert second_provider.settle_calls == 0
    assert second_store.count() == 1
    assert second_store.attempt_count() == 1
    second_store.close()


def test_non_local_hosted_app_requires_durable_settlement_store(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")

    with pytest.raises(RuntimeError, match="approved hosted settlement store"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([FakeProvider()])),
            resolver=StaticSellerAccountResolver(),
        )

    durable_store = SqliteSettlementStore(str(tmp_path / "settlements.sqlite3"))
    with pytest.raises(RuntimeError, match="approved hosted settlement store"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(
                router=ProviderRouter([FakeProvider()]),
                store=durable_store,
            ),
            resolver=StaticSellerAccountResolver(),
        )
    durable_store.close()


def test_non_local_hosted_app_requires_hosted_safe_rate_limiter(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([FakeProvider()]),
        store=_HostedSafeMemoryStore(),
    )

    with pytest.raises(RuntimeError, match="approved hosted rate limiter"):
        create_hosted_facilitator_app(engine=engine, resolver=StaticSellerAccountResolver())


def test_non_local_hosted_app_requires_hosted_safe_project_resolver(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _hosted_oidc_openfga_env(monkeypatch)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([FakeProvider()]),
        store=_HostedSafeMemoryStore(),
    )

    with pytest.raises(RuntimeError, match="approved hosted seller account resolver"):
        create_hosted_facilitator_app(
            engine=engine,
            resolver=StaticSellerAccountResolver(),
            rate_limiter=_AllowingHostedRateLimiter(),
            telemetry_config=_hosted_otel_config(),
        )


def test_non_local_hosted_app_accepts_hosted_safe_rate_limiter(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _hosted_oidc_openfga_env(monkeypatch)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([FakeProvider()]),
        store=_HostedSafeMemoryStore(),
    )

    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver(),
        rate_limiter=_AllowingHostedRateLimiter(),
        telemetry_config=_hosted_otel_config(),
    )

    assert app.title == "OmniClaw Hosted Facilitator"


def test_app_initializes_and_closes_rate_limiter_lifespan():
    limiter = _LifecycleRateLimiter()
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([FakeProvider()])),
        resolver=StaticSellerAccountResolver(),
        rate_limiter=limiter,
    )

    with TestClient(app) as client:
        assert limiter.initialized is True
        assert client.get("/supported").status_code == 200

    assert limiter.closed is True


def test_hosted_mode_must_be_explicit_outside_pytest(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_MODE", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_FACILITATOR_MODE must be set"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([FakeProvider()])),
            resolver=StaticSellerAccountResolver(),
        )

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "")
    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_FACILITATOR_MODE must be set"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([FakeProvider()])),
            resolver=StaticSellerAccountResolver(),
        )


def test_project_scoped_routes_are_not_registered():
    client, _, _ = _client()

    response = client.post(
        "/sellers/rk_corey_alpha/v2/x402/verify",
        json=_envelope(),
        headers={"Authorization": "Bearer corey-secret-key"},
    )
    openapi = client.get("/openapi.json").json()

    assert response.status_code == 404
    assert all(not path.startswith("/projects/") for path in openapi["paths"])


def test_unscoped_bearer_auth_resolves_seller_account_for_settle():
    client, provider, store = _client()

    response = client.post(
        "/v2/x402/settle",
        json=_envelope(),
        headers={"Authorization": "Bearer corey-secret-key"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_verify_and_settle_require_project_auth():
    client, provider, _ = _client()

    verify = client.post("/verify", json=_envelope())
    settle = client.post("/settle", json=_envelope())

    assert verify.status_code == 401
    assert settle.status_code == 401
    assert provider.verify_calls == 0
    assert provider.settle_calls == 0


def test_public_seller_account_id_does_not_authorize_settlement():
    client, provider, _ = _client()

    response = client.post("/sellers/corey-alpha/v2/x402/settle", json=_envelope())

    assert response.status_code == 404
    assert provider.settle_calls == 0


def test_verify_is_side_effect_free_for_settlement_records():
    client, provider, store = _client()

    response = client.post("/verify", json=_envelope(), headers=AUTH)

    assert response.status_code == 200
    assert provider.verify_calls == 1
    assert store.count() == 0


def test_verify_rate_limit_blocks_before_provider_call():
    client, provider, store = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            verify_per_minute=1,
            settle_per_minute=100,
            ip_requests_per_minute=100,
        )
    )

    first = client.post("/verify", json=_envelope(), headers=AUTH)
    second = client.post("/verify", json=_envelope(), headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert second.headers["retry-after"] == "60"
    assert provider.verify_calls == 1
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_settle_rate_limit_blocks_before_settlement_record_or_provider_call():
    client, provider, store = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=1,
            ip_requests_per_minute=100,
        )
    )

    first = client.post("/settle", json=_envelope("https://seller.example.com/one"), headers=AUTH)
    second_body = _envelope("https://seller.example.com/two")
    second_body["paymentPayload"]["payload"]["authorization"]["nonce"] = "0x" + "22" * 32
    second = client.post("/settle", json=second_body, headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_settle_rate_limit_does_not_block_duplicate_cached_result():
    client, provider, store = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=1,
            ip_requests_per_minute=100,
        )
    )

    first = client.post("/settle", json=_envelope(), headers=AUTH)
    second = client.post("/settle", json=_envelope(), headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["transaction"] == "0xsettled"
    assert provider.settle_calls == 1
    assert store.count() == 1


def test_invalid_request_penalty_bucket_blocks_before_provider_call():
    client, provider, store = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            invalid_requests_per_minute=1,
            ip_requests_per_minute=100,
        )
    )
    body = _envelope()
    body["paymentPayload"]["accepted"]["payTo"] = "0xcccccccccccccccccccccccccccccccccccccccc"

    first = client.post("/settle", json=body, headers=AUTH)
    second = client.post("/settle", json=body, headers=AUTH)

    assert first.status_code == 400
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_settle_project_policy_denial_consumes_invalid_penalty_bucket():
    policy = RateLimitPolicy(
        seller_requests_per_minute=100,
        verify_per_minute=100,
        settle_per_minute=100,
        invalid_requests_per_minute=1,
        ip_requests_per_minute=100,
    )
    provider = FakeProvider()
    store = InMemorySettlementStore()
    project = SellerAccountConfig(
        seller_account_id="default",
        api_keys=("secret-key",),
        allowed_pay_to=("0xcccccccccccccccccccccccccccccccccccccccc",),
        rate_limits=policy,
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
        resolver=StaticSellerAccountResolver([project]),
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
    )
    client = TestClient(app)

    first = client.post("/settle", json=_envelope(), headers=AUTH)
    second = client.post("/settle", json=_envelope(), headers=AUTH)

    assert first.status_code == 403
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_settle_unsupported_route_consumes_invalid_penalty_bucket():
    client, provider, store = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            invalid_requests_per_minute=1,
            ip_requests_per_minute=100,
        )
    )
    body = _envelope()
    body["paymentPayload"]["accepted"]["network"] = "eip155:84532"
    body["paymentRequirements"]["network"] = "eip155:84532"

    first = client.post("/settle", json=body, headers=AUTH)
    second = client.post("/settle", json=body, headers=AUTH)

    assert first.status_code == 400
    assert first.json()["detail"] == "Unsupported payment route"
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_ip_bucket_applies_across_project_endpoints():
    client, provider, _ = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            supported_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            ip_requests_per_minute=1,
        )
    )

    first = client.get("/supported")
    second = client.post("/verify", json=_envelope(), headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.verify_calls == 0


def test_untrusted_x_forwarded_for_does_not_bypass_ip_bucket():
    client, provider, _ = _rate_limited_client(
        RateLimitPolicy(
            seller_requests_per_minute=100,
            supported_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            ip_requests_per_minute=1,
        )
    )

    first = client.get("/supported", headers={"x-forwarded-for": "203.0.113.10"})
    second = client.post(
        "/verify",
        json=_envelope(),
        headers={**AUTH, "x-forwarded-for": "203.0.113.11"},
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert provider.verify_calls == 0


def test_trusted_proxy_x_forwarded_for_is_used_for_ip_bucket():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    project = SellerAccountConfig(
        seller_account_id="default",
        api_keys=("secret-key",),
        rate_limits=RateLimitPolicy(
            seller_requests_per_minute=100,
            supported_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            ip_requests_per_minute=1,
        ),
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
        resolver=StaticSellerAccountResolver([project]),
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
        trusted_proxy_cidrs=("10.0.0.0/24",),
    )
    client = TestClient(app, client=("10.0.0.8", 50000))

    first = client.get("/supported", headers={"x-forwarded-for": "203.0.113.10"})
    second = client.post(
        "/verify",
        json=_envelope(),
        headers={**AUTH, "x-forwarded-for": "203.0.113.11"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert provider.verify_calls == 1


def test_trusted_proxy_prefers_cloudflare_connecting_ip_for_ip_bucket():
    provider = FakeProvider()
    store = InMemorySettlementStore()
    project = SellerAccountConfig(
        seller_account_id="default",
        api_keys=("secret-key",),
        rate_limits=RateLimitPolicy(
            seller_requests_per_minute=100,
            supported_per_minute=100,
            verify_per_minute=100,
            settle_per_minute=100,
            ip_requests_per_minute=1,
        ),
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store),
        resolver=StaticSellerAccountResolver([project]),
        rate_limiter=InMemoryFixedWindowRateLimiter(clock=lambda: 0.0),
        trusted_proxy_cidrs=("10.0.0.0/24",),
    )
    client = TestClient(app, client=("10.0.0.8", 50000))

    first = client.get(
        "/supported",
        headers={
            "cf-connecting-ip": "203.0.113.10",
            "x-forwarded-for": "198.51.100.1",
        },
    )
    second = client.post(
        "/verify",
        json=_envelope(),
        headers={
            **AUTH,
            "cf-connecting-ip": "203.0.113.11",
            "x-forwarded-for": "198.51.100.1",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert provider.verify_calls == 1


def test_invalid_trusted_proxy_cidr_fails_fast():
    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_TRUSTED_PROXY_CIDRS"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(router=ProviderRouter([FakeProvider()])),
            resolver=StaticSellerAccountResolver(),
            trusted_proxy_cidrs=("not-a-cidr",),
        )


def test_accepted_requirements_mismatch_rejected_before_provider_call():
    client, provider, store = _client()
    body = _envelope()
    body["paymentPayload"]["accepted"]["payTo"] = "0xcccccccccccccccccccccccccccccccccccccccc"

    response = client.post("/settle", json=body, headers=AUTH)

    assert response.status_code == 400
    assert "paymentPayload.accepted.payTo" in response.json()["detail"]
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_routing_metadata_mismatch_rejected_before_provider_call():
    client, provider, store = _client()
    body = _envelope()
    body["paymentRequirements"]["extra"] = {"name": "GatewayWalletBatched"}

    response = client.post("/settle", json=body, headers=AUTH)

    assert response.status_code == 400
    assert "paymentPayload.accepted.extra" in response.json()["detail"]
    assert provider.settle_calls == 0
    assert store.count() == 0


def test_verify_rejects_routing_metadata_mismatch_before_provider_call():
    client, provider, store = _client()
    body = _envelope()
    body["paymentRequirements"]["extra"] = {"name": "GatewayWalletBatched"}

    response = client.post("/verify", json=body, headers=AUTH)

    assert response.status_code == 400
    assert "paymentPayload.accepted.extra" in response.json()["detail"]
    assert provider.verify_calls == 0
    assert store.count() == 0


def test_typed_x402_validation_happens_before_settlement_claim():
    store = InMemorySettlementStore()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([HostedExactProvider(_FakeExactFacilitator())]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    client = TestClient(create_hosted_facilitator_app(engine=engine, resolver=resolver))
    body = _envelope()
    del body["paymentPayload"]["payload"]

    response = client.post("/settle", json=body, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid x402 payload"
    assert store.count() == 0


def test_malformed_payload_rate_limit_response_uses_hosted_contract():
    store = InMemorySettlementStore()
    limiter = _DenyAfterFirstRateLimiter()
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([HostedExactProvider(_FakeExactFacilitator())]),
        store=store,
    )
    resolver = StaticSellerAccountResolver(
        [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
    )
    client = TestClient(
        create_hosted_facilitator_app(
            engine=engine,
            resolver=resolver,
            rate_limiter=limiter,
        )
    )
    body = _envelope()
    del body["paymentPayload"]["payload"]

    first = client.post("/settle", json=body, headers=AUTH)
    second = client.post("/settle", json=body, headers=AUTH)

    assert first.status_code == 400
    assert second.status_code == 429
    assert second.json()["detail"] == RATE_LIMIT_DETAIL
    assert second.headers["retry-after"] == "60"
    assert store.count() == 0


def test_wrong_version_rejected_before_provider_call():
    client, provider, _ = _client()
    body = _envelope()
    body["x402Version"] = 1

    response = client.post("/verify", json=body)

    assert response.status_code == 400
    assert response.json()["detail"] == "Only x402Version=2 is supported"
    assert provider.verify_calls == 0


def test_hosted_exact_factory_wraps_existing_exact_facilitator():
    recorded = {}

    def fake_signer_factory(**kwargs):
        recorded["signer_kwargs"] = kwargs
        return object()

    def fake_register(facilitator, *, signer, networks):
        recorded["networks"] = networks

    app = create_hosted_exact_facilitator_app(
        ExactFacilitatorConfig(
            private_key="0x123",
            rpc_url="https://rpc.example",
            networks=("eip155:5042002",),
        ),
        seller_accounts=[
            SellerAccountConfig(seller_account_id="default"),
            SellerAccountConfig(seller_account_id="arc-alpha", api_keys=("secret-key",)),
        ],
        signer_factory=fake_signer_factory,
        facilitator_factory=_FakeExactFacilitator,
        register_facilitator=fake_register,
    )
    client = TestClient(app)

    supported = client.get("/v2/x402/supported")
    settle = client.post("/v2/x402/settle", json=_envelope(), headers=AUTH)

    assert recorded["signer_kwargs"] == {
        "private_key": "0x123",
        "rpc_url": "https://rpc.example",
    }
    assert recorded["networks"] == ["eip155:5042002"]
    assert supported.status_code == 200
    assert supported.json()["kinds"][0]["network"] == "eip155:5042002"
    assert settle.status_code == 200
    assert settle.json()["transaction"] == "0xarcsettled"


def test_hosted_exact_factory_routes_multiple_evm_network_configs_with_shared_signer():
    signer_kwargs = []
    registered_networks = []

    class NetworkAwareExactFacilitator:
        network = "unknown"

        def get_supported(self):
            return _FakeResult(
                {"kinds": [{"x402Version": 2, "scheme": "exact", "network": self.network}]}
            )

        async def verify(self, payload, requirements):
            return _FakeResult({"isValid": True, "payer": "0xarc"})

        async def settle(self, payload, requirements):
            return _FakeResult(
                {
                    "success": True,
                    "transaction": f"0xsettled{self.network.split(':')[-1]}",
                    "network": self.network,
                    "payer": "0xarc",
                }
            )

    def fake_signer_factory(**kwargs):
        signer_kwargs.append(kwargs)
        return object()

    def fake_register(facilitator, *, signer, networks):
        facilitator.network = networks[0]
        registered_networks.extend(networks)

    primary = ExactFacilitatorConfig(
        private_key="0xshared",
        rpc_url="https://arc-rpc.example",
        networks=("eip155:5042002",),
    )
    base = ExactFacilitatorConfig(
        private_key="0xshared",
        rpc_url="https://base-rpc.example",
        networks=("eip155:84532",),
    )
    app = create_hosted_exact_facilitator_app(
        primary,
        network_configs=(primary, base),
        seller_accounts=[
            SellerAccountConfig(
                seller_account_id="default",
                api_keys=("secret-key",),
                enabled_networks=("eip155:5042002", "eip155:84532"),
            )
        ],
        signer_factory=fake_signer_factory,
        facilitator_factory=NetworkAwareExactFacilitator,
        register_facilitator=fake_register,
    )
    client = TestClient(app)

    supported = client.get("/v2/x402/supported")
    base_body = _envelope_for_network("eip155:84532")
    base_settle = client.post("/v2/x402/settle", json=base_body, headers=AUTH)

    assert signer_kwargs == [
        {"private_key": "0xshared", "rpc_url": "https://arc-rpc.example"},
        {"private_key": "0xshared", "rpc_url": "https://base-rpc.example"},
    ]
    assert registered_networks == ["eip155:5042002", "eip155:84532"]
    assert {kind["network"] for kind in supported.json()["kinds"]} == {
        "eip155:5042002",
        "eip155:84532",
    }
    assert base_settle.status_code == 200
    assert base_settle.json()["network"] == "eip155:84532"
    assert base_settle.json()["transaction"] == "0xsettled84532"


def test_hosted_exact_factory_rejects_network_configs_with_different_signers():
    primary = ExactFacilitatorConfig(
        private_key="0xshared",
        rpc_url="https://arc-rpc.example",
        networks=("eip155:5042002",),
    )
    base = ExactFacilitatorConfig(
        private_key="0xdifferent",
        rpc_url="https://base-rpc.example",
        networks=("eip155:84532",),
    )

    with pytest.raises(RuntimeError, match="one shared EVM signer key"):
        create_hosted_exact_facilitator_app(primary, network_configs=(primary, base))


def test_hosted_exact_factory_rejects_network_config_with_multiple_networks():
    config = ExactFacilitatorConfig(
        private_key="0xshared",
        rpc_url="https://rpc.example",
        networks=("eip155:5042002", "eip155:84532"),
    )

    with pytest.raises(RuntimeError, match="exactly one network"):
        create_hosted_exact_facilitator_app(config, network_configs=(config,))


def test_hosted_exact_factory_requires_signer_guard_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    config = ExactFacilitatorConfig(
        private_key="0xshared",
        rpc_url="https://rpc.example",
        networks=("eip155:5042002",),
    )

    with pytest.raises(RuntimeError, match="requires an approved signer guard"):
        create_hosted_exact_facilitator_app(config)


def test_control_plane_settlement_detail_preserves_gateway_attempt_kind():
    store = _HostedSafeMemoryStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="alpha",
        payment_profile_id="default",
        fingerprint="fp-gateway-detail",
        provider="circle_gateway",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_gateway_detail",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "extra": {"name": "GatewayWalletBatched"},
        },
    )
    attempt_id = store.start_settle_attempt(record, trace_id="tr_gateway_detail")
    store.mark_settled(
        record,
        transaction="gateway-transfer-123",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    store.finish_settle_attempt(
        attempt_id,
        status=SettlementAttemptStatus.SETTLED,
        transaction="gateway-transfer-123",
    )
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([]),
        store=store,
    )
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )

    response = TestClient(app).get(f"/ops/api/settlements/{record.record_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["record"]["provider"] == "circle_gateway"
    assert body["record"]["transactionKind"] == "gateway_transfer"
    assert body["record"]["explorerUrl"] == ""
    assert body["attempts"][0]["transactionKind"] == "gateway_transfer"
    assert body["attempts"][0]["explorerUrl"] == ""


def test_control_plane_reconciliation_actions_claim_release_and_mark_manual_review():
    store = _HostedSafeReconciliationStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="alpha",
        payment_profile_id="default",
        fingerprint="fp-manual-action",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_manual_action",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "1000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "extra": {"name": "USDC"},
        },
    )
    store.mark_unknown(record, error_reason="provider_timeout")
    engine = HostedFacilitatorEngine(router=ProviderRouter([]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )
    client = TestClient(app)

    claim_response = client.post(
        f"/ops/api/reconciliation/{record.record_id}/claim",
        json={"reason": "operator triage", "leaseSeconds": 120},
    )
    release_response = client.post(
        f"/ops/api/reconciliation/{record.record_id}/release",
        json={"reason": "handoff"},
    )
    reclaim_response = client.post(
        f"/ops/api/reconciliation/{record.record_id}/claim",
        json={"reason": "manual review triage"},
    )
    manual_response = client.post(
        f"/ops/api/reconciliation/{record.record_id}/manual-review",
        json={"reason": "move to manual review"},
    )

    assert claim_response.status_code == 200
    assert claim_response.json()["record"]["reconciliation"]["owner"] == _safe_operator_lease_owner(
        "ops-user", issuer="https://idp.example.test"
    )
    assert claim_response.json()["auditEvent"]["action"] == "reconciliation_claim"
    assert claim_response.json()["auditEvent"]["targetType"] == "settlement"
    assert claim_response.json()["auditEvent"]["reason"] == "operator_supplied"
    assert release_response.status_code == 200
    assert release_response.json()["record"]["reconciliation"]["owner"] == ""
    assert release_response.json()["auditEvent"]["action"] == "reconciliation_release"
    assert reclaim_response.status_code == 200
    assert manual_response.status_code == 200
    assert manual_response.json()["record"]["status"] == "manual_review"
    assert manual_response.json()["record"]["errorReason"] == "manual_review_operator_requested"
    assert manual_response.json()["record"]["reconciliation"]["owner"] == ""
    assert manual_response.json()["auditEvent"]["action"] == "reconciliation_manual_review"


def test_control_plane_reconciliation_manual_review_requires_operator_claim():
    store = _HostedSafeReconciliationStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="alpha",
        payment_profile_id="default",
        fingerprint="fp-manual-requires-claim",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_manual_requires_claim",
        raw_requirements={},
    )
    store.mark_unknown(record, error_reason="provider_timeout")
    engine = HostedFacilitatorEngine(router=ProviderRouter([]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )

    response = TestClient(app).post(
        f"/ops/api/reconciliation/{record.record_id}/manual-review",
        json={"reason": "manual review without claim"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Settlement record must be claimed by this operator"
    assert record.status == SettlementStatus.UNKNOWN


def test_control_plane_reconciliation_actions_require_manual_review_permission():
    store = _HostedSafeReconciliationStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="alpha",
        payment_profile_id="default",
        fingerprint="fp-manual-denied",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_manual_denied",
        raw_requirements={},
    )
    store.mark_unknown(record, error_reason="provider_timeout")
    authorizer = _DenyOperationsAuthorizer()
    engine = HostedFacilitatorEngine(router=ProviderRouter([]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        operations_authorizer=authorizer,
    )

    response = TestClient(app).post(
        f"/ops/api/reconciliation/{record.record_id}/claim",
        json={"reason": "operator triage"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
    assert authorizer.permissions == [OperationsPermission.OPS_MANUAL_REVIEW]
    assert record.reconciliation_owner is None
    assert record.status == SettlementStatus.UNKNOWN


def test_control_plane_reconciliation_claim_rejects_fresh_active_records():
    store = _HostedSafeReconciliationStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="alpha",
        payment_profile_id="default",
        fingerprint="fp-active-claim",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_active_claim",
        raw_requirements={},
    )
    store.mark_submitted(record, transaction="0xabc", payer="0xaaa")
    engine = HostedFacilitatorEngine(router=ProviderRouter([]), store=store)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        operations_authorizer=_AllowAllOperationsAuthorizer(),
    )

    response = TestClient(app).post(
        f"/ops/api/reconciliation/{record.record_id}/claim",
        json={"reason": "operator triage"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Active settlement record is not stale enough to claim"
    assert record.reconciliation_owner is None
    assert record.status == SettlementStatus.SUBMITTED


def _envelope_for_network(network: str) -> dict:
    body = _envelope()
    body["paymentPayload"]["accepted"]["network"] = network
    body["paymentRequirements"]["network"] = network
    return body
