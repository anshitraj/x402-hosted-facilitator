from __future__ import annotations

import base64
import json

import httpx
import pytest

from hosted_facilitator.nanopayments.adapter import NanopaymentAdapter
from hosted_facilitator.nanopayments.signing import EIP3009Signer


class _HttpClient:
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def request(self, **kwargs):
        del kwargs
        return httpx.Response(self.status_code, text='{"error":"failed"}')


class _Balance:
    available = 10_000


class _GatewayClient:
    async def check_balance(self, *, address: str, network: str):
        del address, network
        return _Balance()

    async def get_verifying_contract(self, network: str) -> str:
        del network
        return "0x" + "c" * 40


class _GatewayHttpClient:
    def __init__(self):
        self.retry_header = ""
        self.calls = 0

    async def request(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            payment_required = {
                "x402Version": 2,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:5042002",
                        "asset": "0x" + "d" * 40,
                        "amount": "1000",
                        "payTo": "0x" + "b" * 40,
                        "maxTimeoutSeconds": 604900,
                        "extra": {
                            "name": "GatewayWalletBatched",
                            "version": "1",
                            "verifyingContract": "0x" + "c" * 40,
                            "minValiditySeconds": 604800,
                            "assets": [
                                {
                                    "symbol": "USDC",
                                    "address": "0x" + "d" * 40,
                                    "decimals": 6,
                                }
                            ],
                        },
                    }
                ],
            }
            encoded = base64.b64encode(json.dumps(payment_required).encode()).decode()
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": encoded},
                json={"resource": {"url": "http://seller.local/compute"}},
            )

        self.retry_header = kwargs["headers"]["PAYMENT-SIGNATURE"]
        payment_response = base64.b64encode(
            json.dumps({"success": True, "transaction": "gateway-transfer-1"}).encode()
        ).decode()
        return httpx.Response(
            200,
            headers={"PAYMENT-RESPONSE": payment_response},
            json={"ok": True},
        )


@pytest.mark.asyncio
async def test_nanopayment_adapter_non_402_server_error_is_not_success():
    adapter = NanopaymentAdapter(
        signer=EIP3009Signer("0x" + "1" * 64),
        nanopayment_client=object(),  # unused for non-402 responses
        http_client=_HttpClient(500),  # type: ignore[arg-type]
        auto_topup_enabled=False,
    )

    result = await adapter.pay_x402_url("http://seller.local/compute")

    assert result.success is False
    assert result.is_nanopayment is False


@pytest.mark.asyncio
async def test_nanopayment_adapter_non_402_success_is_success():
    adapter = NanopaymentAdapter(
        signer=EIP3009Signer("0x" + "1" * 64),
        nanopayment_client=object(),  # unused for non-402 responses
        http_client=_HttpClient(204),  # type: ignore[arg-type]
        auto_topup_enabled=False,
    )

    result = await adapter.pay_x402_url("http://seller.local/compute")

    assert result.success is True
    assert result.is_nanopayment is False


@pytest.mark.asyncio
async def test_nanopayment_adapter_preserves_gateway_extra_in_retry_payload():
    http_client = _GatewayHttpClient()
    adapter = NanopaymentAdapter(
        signer=EIP3009Signer("0x" + "1" * 64),
        nanopayment_client=_GatewayClient(),  # type: ignore[arg-type]
        http_client=http_client,  # type: ignore[arg-type]
        auto_topup_enabled=False,
        strict_settlement=True,
    )

    result = await adapter.pay_x402_url("http://seller.local/compute")

    retry_payload = json.loads(base64.b64decode(http_client.retry_header))
    accepted_extra = retry_payload["accepted"]["extra"]
    assert result.success is True
    assert result.transaction == "gateway-transfer-1"
    assert accepted_extra["name"] == "GatewayWalletBatched"
    assert accepted_extra["verifyingContract"] == "0x" + "c" * 40
    assert accepted_extra["minValiditySeconds"] == 604800
    assert accepted_extra["assets"][0]["symbol"] == "USDC"
