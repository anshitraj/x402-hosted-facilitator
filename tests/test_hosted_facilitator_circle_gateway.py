from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from hosted_facilitator.hosted.app import create_hosted_facilitator_app
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.providers.circle_gateway import (
    HostedCircleGatewayProvider,
    create_hosted_circle_gateway_provider_from_env,
)
from hosted_facilitator.hosted.reconciliation import ReconciliationStatus
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.runner import create_hosted_facilitator_app_from_env
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, ProviderSettlementStatus
from hosted_facilitator.hosted.storage import SettlementRecord, SettlementStatus
from hosted_facilitator.hosted.tenancy import SellerAccountConfig, StaticSellerAccountResolver
from hosted_facilitator.nanopayments.exceptions import (
    GatewayTimeoutError,
    InsufficientBalanceError,
    SettlementError,
)
from hosted_facilitator.nanopayments.types import SettleResponse, SupportedKind, VerifyResponse

AUTH = {"Authorization": "Bearer seller-api-key"}


class _CircleClient:
    def __init__(self):
        self.settle_calls = []
        self.verify_calls = []
        self.supported_calls = 0
        self.transfer_calls = []
        self.transfer_response = {
            "id": "circle-batch-1",
            "status": "received",
            "fromAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "toAddress": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount": "2500",
        }
        self.settle_error = None
        self.settle_response = SettleResponse(
            success=True,
            transaction="circle-batch-1",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            error_reason=None,
        )
        self.verify_response = VerifyResponse(
            is_valid=True,
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            invalid_reason=None,
        )
        self.supported_kinds = [
            SupportedKind(
                x402_version=2,
                scheme="exact",
                network="eip155:5042002",
                extra={
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": "0x" + "c" * 40,
                    "minValiditySeconds": 604800,
                    "assets": [{"symbol": "USDC", "address": "0x" + "d" * 40, "decimals": 6}],
                },
            ),
            SupportedKind(
                x402_version=2,
                scheme="exact",
                network="eip155:8453",
                extra={
                    "name": "GatewayWalletBatched",
                    "version": "1",
                    "verifyingContract": "0x" + "e" * 40,
                    "minValiditySeconds": 604800,
                    "assets": [{"symbol": "USDC", "address": "0x" + "f" * 40, "decimals": 6}],
                },
            ),
        ]

    async def get_supported(self):
        self.supported_calls += 1
        return self.supported_kinds

    async def verify(self, payload, requirements):
        self.verify_calls.append((payload, requirements))
        return self.verify_response

    async def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        if self.settle_error is not None:
            raise self.settle_error
        return self.settle_response

    async def get_x402_transfer(self, transfer_id):
        self.transfer_calls.append(transfer_id)
        return self.transfer_response


class _HostedSafeProjectResolver(StaticSellerAccountResolver):
    hosted_safe = True


def _seller_account() -> SellerAccountConfig:
    return SellerAccountConfig(
        seller_account_id="seller-a",
        enabled_providers=("circle_gateway",),
        enabled_networks=("eip155:5042002",),
        enabled_schemes=("exact",),
        api_keys=("seller-api-key",),
    )


def _envelope(network: str = "eip155:5042002") -> dict:
    requirement = {
        "scheme": "exact",
        "network": network,
        "asset": "0x" + "d" * 40,
        "amount": "2500",
        "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "maxTimeoutSeconds": 604900,
        "extra": {
            "name": "GatewayWalletBatched",
            "version": "1",
            "verifyingContract": "0x" + "c" * 40,
            "minValiditySeconds": 604800,
            "assets": [{"symbol": "USDC", "address": "0x" + "d" * 40, "decimals": 6}],
        },
    }
    return {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "scheme": "exact",
            "network": network,
            "accepted": requirement,
            "payload": {
                "signature": "0x" + "1" * 130,
                "authorization": {
                    "from": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "to": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "value": "2500",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "2" * 64,
                },
            },
            "resource": {
                "url": "https://seller.example.com/private",
                "description": "private resource",
                "mimeType": "application/json",
            },
        },
        "paymentRequirements": requirement,
    }


@pytest.mark.asyncio
async def test_circle_gateway_supported_kinds_are_provider_scoped():
    client = _CircleClient()
    provider = HostedCircleGatewayProvider(client, networks=("eip155:5042002",))
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())

    kinds = await provider.supported(context)

    assert len(kinds) == 1
    assert kinds[0]["network"] == "eip155:5042002"
    assert kinds[0]["extra"]["name"] == "GatewayWalletBatched"
    assert kinds[0]["extra"]["minValiditySeconds"] == 604800
    assert kinds[0]["extra"]["assets"][0]["address"] == "0x" + "d" * 40


@pytest.mark.asyncio
async def test_circle_gateway_supported_filters_non_gateway_kinds():
    client = _CircleClient()
    client.supported_kinds = [
        SupportedKind(
            x402_version=2,
            scheme="exact",
            network="eip155:5042002",
            extra={"name": "NotGateway", "verifyingContract": "0x" + "c" * 40},
        )
    ]
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())

    assert await provider.supported(context) == []


@pytest.mark.asyncio
async def test_circle_gateway_health_requires_usable_gateway_kind_for_configured_network():
    client = _CircleClient()
    client.supported_kinds = [
        SupportedKind(
            x402_version=2,
            scheme="exact",
            network="eip155:5042002",
            extra={"name": "NotGateway", "verifyingContract": "0x" + "c" * 40},
        )
    ]
    provider = HostedCircleGatewayProvider(client, networks=("eip155:5042002",))

    health = await provider.health_check()

    assert health["status"] == "unhealthy"
    assert health["errorType"] == "GatewayNetworkUnavailable"
    assert health["missingNetworkCount"] == 1


@pytest.mark.asyncio
async def test_circle_gateway_health_reports_configured_gateway_network_ok():
    client = _CircleClient()
    provider = HostedCircleGatewayProvider(client, networks=("eip155:5042002",))

    health = await provider.health_check()

    assert health["status"] == "ok"
    assert health["networkCount"] == 1


@pytest.mark.asyncio
async def test_circle_gateway_provider_settles_gateway_payment():
    client = _CircleClient()
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.success is True
    assert outcome.provider is None
    assert outcome.transaction == "circle-batch-1"
    assert outcome.settlement_status == ProviderSettlementStatus.SETTLED
    payload, requirements = client.settle_calls[0]
    assert payload.accepted.network == "eip155:5042002"
    assert requirements.accepts[0].extra.name == "GatewayWalletBatched"
    assert requirements.accepts[0].extra.assets[0]["symbol"] == "USDC"


@pytest.mark.asyncio
async def test_circle_gateway_reconciles_received_transfer_as_pending():
    client = _CircleClient()
    provider = HostedCircleGatewayProvider(client)
    record = SettlementRecord(
        record_id=1,
        seller_account_id="seller-a",
        fingerprint="fp",
        provider="circle_gateway",
        scheme="exact",
        network="eip155:5042002",
        status=SettlementStatus.UNKNOWN,
        transaction="circle-batch-1",
    )

    outcome = await provider.reconcile_settlement(record)

    assert outcome.status == ReconciliationStatus.PENDING
    assert outcome.transaction == "circle-batch-1"
    assert outcome.payer == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert outcome.error_reason == "circle_gateway_transfer_pending"
    assert client.transfer_calls == ["circle-batch-1"]


@pytest.mark.asyncio
async def test_circle_gateway_reconciles_confirmed_transfer_as_settled():
    client = _CircleClient()
    client.transfer_response = {
        "id": "circle-batch-1",
        "status": "confirmed",
        "fromAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    provider = HostedCircleGatewayProvider(client)
    record = SettlementRecord(
        record_id=1,
        seller_account_id="seller-a",
        fingerprint="fp",
        provider="circle_gateway",
        scheme="exact",
        network="eip155:5042002",
        status=SettlementStatus.UNKNOWN,
        transaction="circle-batch-1",
    )

    outcome = await provider.reconcile_settlement(record)

    assert outcome.status == ReconciliationStatus.SETTLED
    assert outcome.transaction == "circle-batch-1"


@pytest.mark.asyncio
async def test_circle_gateway_reconciles_failed_transfer_as_final_failure():
    client = _CircleClient()
    client.transfer_response = {"id": "circle-batch-1", "status": "failed"}
    provider = HostedCircleGatewayProvider(client)
    record = SettlementRecord(
        record_id=1,
        seller_account_id="seller-a",
        fingerprint="fp",
        provider="circle_gateway",
        scheme="exact",
        network="eip155:5042002",
        status=SettlementStatus.UNKNOWN,
        transaction="circle-batch-1",
    )

    outcome = await provider.reconcile_settlement(record)

    assert outcome.status == ReconciliationStatus.FAILED_FINAL
    assert outcome.error_reason == "circle_gateway_transfer_failed"


@pytest.mark.asyncio
async def test_circle_gateway_provider_treats_success_without_transaction_as_unknown():
    client = _CircleClient()
    client.settle_response = SettleResponse(
        success=True,
        transaction=None,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.success is False
    assert outcome.error_reason == "circle_gateway_outcome_unknown"
    assert outcome.settlement_status == ProviderSettlementStatus.UNKNOWN


@pytest.mark.asyncio
async def test_circle_gateway_provider_maps_balance_failures_to_final_failure():
    client = _CircleClient()
    client.settle_error = InsufficientBalanceError(
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.success is False
    assert outcome.error_reason == "circle_gateway_insufficient_balance"
    assert outcome.settlement_status == ProviderSettlementStatus.FAILED_FINAL


@pytest.mark.asyncio
async def test_circle_gateway_provider_sanitizes_unknown_error_reason():
    client = _CircleClient()
    client.settle_error = InsufficientBalanceError(
        reason="private-provider-detail-123",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.error_reason == "circle_gateway_settlement_failed"


@pytest.mark.asyncio
async def test_circle_gateway_provider_maps_timeout_to_unknown():
    client = _CircleClient()
    client.settle_error = GatewayTimeoutError("/v1/x402/settle")
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.success is False
    assert outcome.error_reason == "circle_gateway_outcome_unknown"
    assert outcome.settlement_status == ProviderSettlementStatus.UNKNOWN


@pytest.mark.asyncio
async def test_circle_gateway_provider_maps_ambiguous_settlement_error_to_unknown():
    client = _CircleClient()
    client.settle_error = SettlementError(
        reason="settle_exact_evm_transaction_confirmation_timed_out",
        transaction="circle-batch-1",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.settle(envelope, context)

    assert outcome.success is False
    assert outcome.transaction == "circle-batch-1"
    assert outcome.error_reason == (
        "circle_gateway_settle_exact_evm_transaction_confirmation_timed_out"
    )
    assert outcome.settlement_status == ProviderSettlementStatus.UNKNOWN


@pytest.mark.asyncio
async def test_circle_gateway_verify_requires_strict_true():
    client = _CircleClient()
    client.verify_response = VerifyResponse(
        is_valid="false",  # type: ignore[arg-type]
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        invalid_reason=None,
    )
    provider = HostedCircleGatewayProvider(client)
    context = RequestContext(trace_id="tr_circle", seller_account=_seller_account())
    envelope = FacilitatorEnvelope.model_validate(_envelope())

    outcome = await provider.verify(envelope, context)

    assert outcome.is_valid is False


def test_hosted_app_routes_gateway_batched_payment_to_circle_provider():
    circle_client = _CircleClient()
    provider = HostedCircleGatewayProvider(circle_client)
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]))
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver([_seller_account()]),
    )
    client = TestClient(app)

    response = client.post("/v2/x402/settle", json=_envelope(), headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["provider"] == "circle_gateway"
    assert body["transaction"] == "circle-batch-1"
    assert len(circle_client.settle_calls) == 1


def test_hosted_app_routes_gateway_verify_to_circle_provider():
    circle_client = _CircleClient()
    provider = HostedCircleGatewayProvider(circle_client)
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]))
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver([_seller_account()]),
    )
    client = TestClient(app)

    response = client.post("/v2/x402/verify", json=_envelope(), headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["isValid"] is True
    assert body["provider"] == "circle_gateway"
    assert len(circle_client.verify_calls) == 1


def test_hosted_app_supported_includes_circle_kinds_for_project_policy():
    circle_client = _CircleClient()
    provider = HostedCircleGatewayProvider(circle_client, networks=("eip155:5042002",))
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]))
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver([_seller_account()]),
    )
    client = TestClient(app)

    response = client.get("/v2/x402/supported", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert len(body["kinds"]) == 1
    assert body["kinds"][0]["extra"]["name"] == "GatewayWalletBatched"


def test_hosted_app_rejects_gateway_network_outside_circle_provider_config():
    circle_client = _CircleClient()
    provider = HostedCircleGatewayProvider(circle_client, networks=("eip155:5042002",))
    project = SellerAccountConfig(
        seller_account_id="seller-a",
        enabled_providers=("circle_gateway",),
        enabled_networks=("eip155:8453",),
        enabled_schemes=("exact",),
        api_keys=("seller-api-key",),
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([provider])),
        resolver=_HostedSafeProjectResolver([project]),
    )
    client = TestClient(app)

    response = client.post("/v2/x402/settle", json=_envelope("eip155:8453"), headers=AUTH)

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported payment route"
    assert circle_client.settle_calls == []


def test_generic_hosted_app_from_env_can_run_circle_without_exact(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "local")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_ENABLED", "false")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS", "eip155:5042002")
    monkeypatch.delenv("OMNICLAW_X402_FACILITATOR_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("OMNICLAW_X402_FACILITATOR_RPC_URL", raising=False)

    app = create_hosted_facilitator_app_from_env()

    assert set(app.state.omniclaw_hosted_providers) == {"circle_gateway"}


def test_circle_gateway_provider_from_env_requires_explicit_enable(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", raising=False)

    assert create_hosted_circle_gateway_provider_from_env() is None


def test_circle_gateway_provider_from_env_does_not_require_circle_credentials(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")

    provider = create_hosted_circle_gateway_provider_from_env()

    assert provider is not None


def test_circle_gateway_provider_from_env_accepts_network_allowlist(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS", "eip155:5042002,eip155:8453")

    provider = create_hosted_circle_gateway_provider_from_env()

    assert provider is not None
    assert provider.supported_networks == ("eip155:5042002", "eip155:8453")


def test_circle_gateway_provider_from_env_rejects_invalid_network(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS", "eip155:abc")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS"):
        create_hosted_circle_gateway_provider_from_env()


def test_circle_gateway_provider_from_env_rejects_invalid_timeout(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_TIMEOUT_SECONDS", "0")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_CIRCLE_GATEWAY_TIMEOUT_SECONDS"):
        create_hosted_circle_gateway_provider_from_env()


def test_hosted_circle_import_smoke():
    assert importlib.import_module("hosted_facilitator.hosted.runner")
    assert importlib.import_module("hosted_facilitator.hosted.providers.circle_gateway")
