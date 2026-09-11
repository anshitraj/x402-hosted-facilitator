from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from hosted_facilitator.hosted.app import create_hosted_facilitator_app
from hosted_facilitator.hosted.auth import (
    OPS_CONSOLE_OBJECT,
    AuthenticationError,
    AuthorizationDeniedError,
    AuthorizationUnavailableError,
    JWKSTokenClaimsVerifier,
    OIDCAuthenticator,
    OIDCAuthorizationAuthorizer,
    OIDCConfig,
    OpenFGAHttpAuthorizer,
    OperationsPermission,
    Principal,
    StaticRelationshipAuthorizer,
    UnconfiguredOperationsAuthorizer,
    _resolve_local_openfga_ids,
    operations_authorizer_from_env,
)
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import InMemoryControlPlaneState
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.limits import InMemoryFixedWindowRateLimiter
from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, SettleOutcome, VerifyOutcome
from hosted_facilitator.hosted.storage import InMemorySettlementStore
from hosted_facilitator.hosted.telemetry import HostedTelemetryConfig
from hosted_facilitator.hosted.tenancy import SellerAccountConfig, StaticSellerAccountResolver


class _ClaimsVerifier:
    def __init__(self, claims: dict[str, Any] | Exception):
        self.claims = claims

    def verify(self, token: str):
        if token != "good-token":
            raise AuthenticationError("bad token")
        if isinstance(self.claims, Exception):
            raise self.claims
        return self.claims


class _Provider(HostedFacilitatorProvider):
    name = "exact_evm"

    async def supported(self, context: RequestContext) -> list[dict]:
        del context
        return []

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        del envelope, context
        return VerifyOutcome(isValid=True)

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        del envelope, context
        return SettleOutcome(success=True)


class _HostedSafeStore(InMemorySettlementStore):
    hosted_safe = True


class _HostedSafeRateLimiter(InMemoryFixedWindowRateLimiter):
    hosted_safe = True


class _HostedSafeProjectResolver(StaticSellerAccountResolver):
    hosted_safe = True


def _hosted_otel_config() -> HostedTelemetryConfig:
    return HostedTelemetryConfig(
        environment="production",
        required=True,
        otlp_endpoint="https://otel.example.test",
        collector_health_url="https://otel.example.test/health",
        json_logs_required=True,
    )


def _claims(**overrides) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": "https://idp.example.test",
        "aud": "omniclaw-ops",
        "sub": "user-123",
        "email": "ops@example.test",
        "exp": now + 300,
        "iat": now,
        "token_use": "access",
    }
    claims.update(overrides)
    return claims


def _authenticator(claims: dict[str, Any] | Exception) -> OIDCAuthenticator:
    return OIDCAuthenticator(
        OIDCConfig(
            issuer="https://idp.example.test",
            audiences=("omniclaw-ops",),
            jwks_url="https://idp.example.test/jwks",
        ),
        verifier=_ClaimsVerifier(claims),
    )


def _app(operations_authorizer, *, store: InMemorySettlementStore | None = None):
    return create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(
            router=ProviderRouter([_Provider()]),
            store=store or InMemorySettlementStore(),
        ),
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        rate_limiter=InMemoryFixedWindowRateLimiter(),
        control_plane_state=InMemoryControlPlaneState(),
        operations_authorizer=operations_authorizer,
    )


@pytest.mark.asyncio
async def test_oidc_authorizer_allows_permission_through_relationship_check():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_OVERVIEW}},
        ),
    )

    principal = await authorizer.authorize("Bearer good-token", OperationsPermission.OPS_OVERVIEW)

    assert principal.subject == "user-123"
    assert principal.actor == "user-123"


@pytest.mark.asyncio
async def test_oidc_authorizer_denies_missing_permission():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_OVERVIEW}},
        ),
    )

    with pytest.raises(AuthorizationDeniedError):
        await authorizer.authorize("Bearer good-token", OperationsPermission.OPS_PAUSE)


def test_oidc_authenticator_rejects_expired_token():
    authenticator = _authenticator(_claims(exp=int(time.time()) - 120))

    with pytest.raises(AuthenticationError, match="expired"):
        authenticator.authenticate("Bearer good-token")


def test_oidc_authenticator_rejects_missing_token_type():
    claims = _claims()
    claims.pop("token_use")
    authenticator = _authenticator(claims)

    with pytest.raises(AuthenticationError, match="token type"):
        authenticator.authenticate("Bearer good-token")


@pytest.mark.asyncio
async def test_operations_authorizer_fails_closed_when_relationship_check_unavailable():
    class _UnavailableRelationships:
        async def check(self, principal, permission, obj):
            del principal, permission, obj
            raise RuntimeError("openfga unavailable")

    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=_UnavailableRelationships(),
    )

    with pytest.raises(AuthorizationUnavailableError):
        await authorizer.authorize("Bearer good-token", OperationsPermission.OPS_PAUSE)


def test_control_plane_pause_uses_authenticated_principal_as_actor():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_OVERVIEW, OperationsPermission.OPS_PAUSE}},
        ),
    )
    client = TestClient(_app(authorizer))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "maintenance",
        },
        headers={
            "Authorization": "Bearer good-token",
            "x-omniclaw-operator": "spoof@example.test",
        },
    )

    assert response.status_code == 200
    assert response.json()["event"]["actor"] == "user-123"


def test_control_plane_authz_denial_is_forbidden():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_OVERVIEW}},
        ),
    )
    client = TestClient(_app(authorizer))

    response = client.post(
        "/ops/api/pauses",
        json={
            "targetType": "provider",
            "target": "exact_evm",
            "paused": True,
            "reason": "maintenance",
        },
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403


def test_control_plane_authn_failure_is_unauthorized():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(default_allowed=True),
    )
    client = TestClient(_app(authorizer))

    response = client.get(
        "/ops/api/overview",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401


def test_control_plane_overview_authz_denial_is_forbidden():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_PAUSE}},
        ),
    )
    client = TestClient(_app(authorizer))

    response = client.get(
        "/ops/api/overview",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403


def test_control_plane_settlement_detail_requires_settlement_permission():
    store = InMemorySettlementStore()
    record, _claim = store.claim_for_settlement(
        seller_account_id="default",
        payment_profile_id="default",
        fingerprint="fp-auth-settlement",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_auth_settlement",
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "250000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "resource": "https://seller.example.com/auth-settlement",
            "extra": {"name": "USDC"},
        },
    )
    store.mark_settled(record, transaction="0x" + "44" * 32, payer=None)
    overview_authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_OVERVIEW}},
        ),
    )
    settlement_authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_SETTLEMENTS}},
        ),
    )

    denied = TestClient(_app(overview_authorizer, store=store)).get(
        f"/ops/api/settlements/{record.record_id}",
        headers={"Authorization": "Bearer good-token"},
    )
    allowed = TestClient(_app(settlement_authorizer, store=store)).get(
        f"/ops/api/settlements/{record.record_id}",
        headers={"Authorization": "Bearer good-token"},
    )
    denied_queue = TestClient(_app(overview_authorizer, store=store)).get(
        "/ops/api/reconciliation",
        headers={"Authorization": "Bearer good-token"},
    )
    allowed_queue = TestClient(_app(settlement_authorizer, store=store)).get(
        "/ops/api/reconciliation",
        headers={"Authorization": "Bearer good-token"},
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert denied_queue.status_code == 403
    assert allowed_queue.status_code == 200


def test_readiness_authz_denial_is_forbidden():
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(
            {"user-123": {OperationsPermission.OPS_PAUSE}},
        ),
    )
    client = TestClient(_app(authorizer))

    response = client.get(
        "/readyz",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unconfigured_operations_authorizer_fails_closed():
    authorizer = UnconfiguredOperationsAuthorizer()

    with pytest.raises(AuthorizationUnavailableError, match="OIDC/OpenFGA"):
        await authorizer.authorize("Bearer anything", OperationsPermission.OPS_OVERVIEW)


def test_operations_authorizer_from_env_requires_oidc_outside_local(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", raising=False)

    with pytest.raises(AuthorizationUnavailableError, match="OIDC"):
        operations_authorizer_from_env(
            hosted_mode="production",
        )


def test_operations_authorizer_from_env_returns_fail_closed_authorizer_in_local_without_oidc(
    monkeypatch,
):
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", raising=False)

    authorizer = operations_authorizer_from_env(
        hosted_mode="test",
    )

    assert isinstance(authorizer, UnconfiguredOperationsAuthorizer)
    assert authorizer.hosted_safe is False


def test_non_local_hosted_mode_rejects_injected_non_hosted_safe_authorizer(monkeypatch):
    class _UnsafeAuthorizer:
        hosted_safe = False

        async def authorize(self, *_args, **_kwargs):
            return Principal(subject="unsafe")

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )

    with pytest.raises(RuntimeError, match="approved hosted operations authorizer"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(
                router=ProviderRouter([_Provider()]),
                store=_HostedSafeStore(),
            ),
            resolver=_HostedSafeProjectResolver(),
            rate_limiter=_HostedSafeRateLimiter(),
            telemetry_config=_hosted_otel_config(),
            operations_authorizer=_UnsafeAuthorizer(),
        )


def test_non_local_hosted_mode_rejects_injected_unconfigured_authorizer(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )

    with pytest.raises(RuntimeError, match="approved hosted operations authorizer"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(
                router=ProviderRouter([_Provider()]),
                store=_HostedSafeStore(),
            ),
            resolver=_HostedSafeProjectResolver(),
            rate_limiter=_HostedSafeRateLimiter(),
            telemetry_config=_hosted_otel_config(),
            operations_authorizer=UnconfiguredOperationsAuthorizer(),
        )


def test_non_local_hosted_mode_rejects_oidc_authorizer_with_static_relationships(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )
    authorizer = OIDCAuthorizationAuthorizer(
        authenticator=_authenticator(_claims()),
        relationships=StaticRelationshipAuthorizer(default_allowed=True),
    )

    with pytest.raises(RuntimeError, match="approved hosted operations authorizer"):
        create_hosted_facilitator_app(
            engine=HostedFacilitatorEngine(
                router=ProviderRouter([_Provider()]),
                store=_HostedSafeStore(),
            ),
            resolver=_HostedSafeProjectResolver(),
            rate_limiter=_HostedSafeRateLimiter(),
            telemetry_config=_hosted_otel_config(),
            operations_authorizer=authorizer,
        )


def test_operations_authorizer_from_env_requires_openfga_with_oidc(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", "omniclaw-ops")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_API_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", raising=False)

    with pytest.raises(AuthorizationUnavailableError, match="OpenFGA"):
        operations_authorizer_from_env(
            hosted_mode="production",
        )


def test_openfga_from_env_requires_https_outside_local(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_URL", "http://openfga.internal")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", "store-a")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", "model-a")

    with pytest.raises(AuthorizationUnavailableError, match="https"):
        OpenFGAHttpAuthorizer.from_env(hosted_mode="production")


def test_openfga_from_env_can_resolve_seeded_local_store_and_latest_model(monkeypatch):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url).endswith("/stores"):
            return httpx.Response(
                200, json={"stores": [{"id": "store-a", "name": "omniclaw-facilitator"}]}
            )
        if str(request.url).endswith("/stores/store-a/authorization-models"):
            return httpx.Response(
                200,
                json={
                    "authorization_models": [
                        {"id": "model-old", "created_at": "2026-01-01T00:00:00Z"},
                        {"id": "model-new", "created_at": "2026-01-02T00:00:00Z"},
                    ]
                },
            )
        return httpx.Response(404)

    store_id, model_id = _resolve_local_openfga_ids(
        api_url="http://openfga:8080",
        store_name="omniclaw-facilitator",
        transport=httpx.MockTransport(handler),
    )

    assert store_id == "store-a"
    assert model_id == "model-new"
    assert requests == [
        "http://openfga:8080/stores",
        "http://openfga:8080/stores/store-a/authorization-models",
    ]


@pytest.mark.asyncio
async def test_openfga_checks_all_allowed_relations_until_allowed():
    calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        relation = body["tuple_key"]["relation"]
        return httpx.Response(200, json={"allowed": relation == "ops_admin"})

    authorizer = OpenFGAHttpAuthorizer(
        api_url="https://openfga.example.test",
        store_id="store-a",
        authorization_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    allowed = await authorizer.check(
        Principal(subject="user-123"),
        OperationsPermission.OPS_PAUSE,
        OPS_CONSOLE_OBJECT,
    )

    assert allowed is True
    assert [call["tuple_key"]["relation"] for call in calls] == ["ops_admin"]
    assert calls[0]["authorization_model_id"] == "model-a"
    assert calls[0]["tuple_key"]["user"] == "user:user-123"
    assert calls[0]["tuple_key"]["object"] == "ops_console:default"


@pytest.mark.asyncio
async def test_openfga_settlement_permission_checks_settlement_and_admin_relations():
    calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        relation = body["tuple_key"]["relation"]
        return httpx.Response(200, json={"allowed": relation == "ops_admin"})

    authorizer = OpenFGAHttpAuthorizer(
        api_url="https://openfga.example.test",
        store_id="store-a",
        authorization_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    allowed = await authorizer.check(
        Principal(subject="user-123"),
        OperationsPermission.OPS_SETTLEMENTS,
        OPS_CONSOLE_OBJECT,
    )

    assert allowed is True
    assert [call["tuple_key"]["relation"] for call in calls] == [
        "settlement_operator",
        "ops_admin",
    ]


@pytest.mark.asyncio
async def test_openfga_settlement_permission_allows_security_admin_fallback():
    calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        relation = body["tuple_key"]["relation"]
        return httpx.Response(200, json={"allowed": relation == "security_admin"})

    authorizer = OpenFGAHttpAuthorizer(
        api_url="https://openfga.example.test",
        store_id="store-a",
        authorization_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    allowed = await authorizer.check(
        Principal(subject="user-123"),
        OperationsPermission.OPS_SETTLEMENTS,
        OPS_CONSOLE_OBJECT,
    )

    assert allowed is True
    assert [call["tuple_key"]["relation"] for call in calls] == [
        "settlement_operator",
        "ops_admin",
        "security_admin",
    ]


@pytest.mark.asyncio
async def test_openfga_denies_when_all_relations_are_denied():
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"allowed": False})

    authorizer = OpenFGAHttpAuthorizer(
        api_url="https://openfga.example.test",
        store_id="store-a",
        authorization_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    allowed = await authorizer.check(
        Principal(subject="user-123"),
        OperationsPermission.OPS_PAUSE,
        OPS_CONSOLE_OBJECT,
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_openfga_non_2xx_maps_to_unavailable():
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "unavailable"})

    authorizer = OpenFGAHttpAuthorizer(
        api_url="https://openfga.example.test",
        store_id="store-a",
        authorization_model_id="model-a",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AuthorizationUnavailableError):
        await authorizer.check(
            Principal(subject="user-123"),
            OperationsPermission.OPS_PAUSE,
            OPS_CONSOLE_OBJECT,
        )


def test_jwks_verifier_rejects_none_algorithm():
    header = _b64url_json({"alg": "none", "kid": "key-a"})
    payload = _b64url_json(_claims())
    token = f"{header}.{payload}."
    verifier = JWKSTokenClaimsVerifier(
        OIDCConfig(
            issuer="https://idp.example.test",
            audiences=("omniclaw-ops",),
            jwks_url="https://idp.example.test/jwks",
        )
    )

    with pytest.raises(AuthenticationError, match="algorithm"):
        verifier.verify(token)


@pytest.mark.asyncio
async def test_static_relationship_authorizer_ignores_token_claim_roles():
    authorizer = StaticRelationshipAuthorizer({"user-123": {OperationsPermission.OPS_OVERVIEW}})
    principal = Principal(
        subject="user-123",
        claims={"roles": ["security_admin"]},
    )

    allowed = await authorizer.check(principal, OperationsPermission.OPS_PAUSE, OPS_CONSOLE_OBJECT)

    assert allowed is False


def _b64url_json(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
