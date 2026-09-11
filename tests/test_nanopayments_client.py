import pytest

from hosted_facilitator.nanopayments.client import (
    NanopaymentClient,
    NanopaymentHTTPClient,
)
from hosted_facilitator.nanopayments.constants import MAX_TIMEOUT_SECONDS
from hosted_facilitator.nanopayments.exceptions import UnsupportedNetworkError


def test_nanopayment_client_initializes_without_gateway_credentials():
    client = NanopaymentClient(environment="testnet")

    assert client is not None


@pytest.mark.asyncio
async def test_nanopayment_http_client_does_not_send_authorization_header(monkeypatch):
    captured_headers = {}

    class _FakeAsyncClient:
        def __init__(self, *, base_url, timeout, headers):
            del base_url, timeout
            captured_headers.update(headers)

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "hosted_facilitator.nanopayments.client.httpx.AsyncClient",
        _FakeAsyncClient,
    )

    async with NanopaymentHTTPClient(base_url="https://gateway-api-testnet.circle.com"):
        pass

    assert "Authorization" not in captured_headers
    assert captured_headers == {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@pytest.mark.asyncio
async def test_nanopayment_client_preserves_circle_supported_extra(monkeypatch):
    class _FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "kinds": [
                    {
                        "x402Version": 2,
                        "scheme": "exact",
                        "network": "eip155:5042002",
                        "extra": {
                            "name": "GatewayWalletBatched",
                            "version": "1",
                            "verifyingContract": "0x0077777d7eba4688bdef3e311b846f25870a19b9",
                            "minValiditySeconds": 604800,
                            "assets": [
                                {
                                    "symbol": "USDC",
                                    "address": "0x3600000000000000000000000000000000000000",
                                    "decimals": 6,
                                }
                            ],
                        },
                    }
                ]
            }

    class _FakeAsyncClient:
        def __init__(self, *, base_url, timeout, headers):
            del base_url, timeout, headers

        async def get(self, path, **kwargs):
            del kwargs
            assert path == "/v1/x402/supported"
            return _FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "hosted_facilitator.nanopayments.client.httpx.AsyncClient",
        _FakeAsyncClient,
    )

    kinds = await NanopaymentClient(environment="testnet").get_supported(force_refresh=True)

    assert kinds[0].extra["assets"][0]["symbol"] == "USDC"
    assert kinds[0].extra["minValiditySeconds"] == 604800
    assert kinds[0].usdc_address == "0x3600000000000000000000000000000000000000"


@pytest.mark.asyncio
async def test_nanopayment_client_get_x402_transfer(monkeypatch):
    class _FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"id": "transfer-1", "status": "received"}

    class _FakeAsyncClient:
        def __init__(self, *, base_url, timeout, headers):
            del base_url, timeout, headers

        async def get(self, path, **kwargs):
            del kwargs
            assert path == "/v1/x402/transfers/transfer-1"
            return _FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "hosted_facilitator.nanopayments.client.httpx.AsyncClient",
        _FakeAsyncClient,
    )

    transfer = await NanopaymentClient(environment="testnet").get_x402_transfer("transfer-1")

    assert transfer == {"id": "transfer-1", "status": "received"}


def test_gateway_timeout_constant_matches_circle_gateway_sdk_buffer():
    assert MAX_TIMEOUT_SECONDS == 604900


@pytest.mark.asyncio
async def test_nanopayment_client_balance_rejects_unmapped_network():
    client = NanopaymentClient(environment="testnet")

    with pytest.raises(UnsupportedNetworkError):
        await client.check_balance("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "eip155:999999999")
