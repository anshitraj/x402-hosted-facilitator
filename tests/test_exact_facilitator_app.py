from __future__ import annotations

from fastapi.testclient import TestClient

from hosted_facilitator.exact import (
    ExactFacilitatorConfig,
    _normalize_tx_hash,
    create_exact_facilitator_app,
)


class _FakeResult:
    def __init__(self, data):
        self._data = data

    def model_dump(self, by_alias: bool = True, exclude_none: bool = True):
        return dict(self._data)


class _FakeFacilitator:
    def get_supported(self):
        return _FakeResult({"kinds": [{"scheme": "exact", "network": "eip155:84532"}]})

    async def verify(self, payload, requirements):
        return _FakeResult({"isValid": True, "payer": "0xabc"})

    async def settle(self, payload, requirements):
        return _FakeResult({"success": True, "transaction": "0xsettled"})


def test_normalize_tx_hash_adds_prefix_when_missing():
    assert _normalize_tx_hash("abc123") == "0xabc123"
    assert _normalize_tx_hash("0xabc123") == "0xabc123"


def test_create_exact_facilitator_app_registers_networks():
    recorded = {}

    def fake_signer_factory(**kwargs):
        recorded["signer_kwargs"] = kwargs
        return object()

    def fake_register(facilitator, *, signer, networks):
        recorded["networks"] = networks
        recorded["signer"] = signer

    app = create_exact_facilitator_app(
        ExactFacilitatorConfig(
            private_key="0x123",
            rpc_url="https://rpc.example",
            networks=("eip155:84532", "arc:testnet"),
            title="Test Facilitator",
        ),
        signer_factory=fake_signer_factory,
        facilitator_factory=_FakeFacilitator,
        register_facilitator=fake_register,
    )

    assert app.title == "Test Facilitator"
    assert recorded["signer_kwargs"] == {
        "private_key": "0x123",
        "rpc_url": "https://rpc.example",
    }
    assert recorded["networks"] == ["eip155:84532", "arc:testnet"]


def test_exact_facilitator_app_routes_work():
    app = create_exact_facilitator_app(
        ExactFacilitatorConfig(
            private_key="0x123",
            rpc_url="https://rpc.example",
            networks=("eip155:84532",),
        ),
        signer_factory=lambda **kwargs: object(),
        facilitator_factory=_FakeFacilitator,
        register_facilitator=lambda facilitator, *, signer, networks: None,
    )

    client = TestClient(app)

    supported = client.get("/supported")
    assert supported.status_code == 200
    assert supported.json()["kinds"][0]["scheme"] == "exact"

    payload = {
        "x402Version": 2,
        "paymentPayload": {
            "x402Version": 2,
            "payload": {
                "signature": "0x1234",
                "authorization": {
                    "from": "0x1111111111111111111111111111111111111111",
                    "to": "0x2222222222222222222222222222222222222222",
                    "value": "250000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "11" * 32,
                },
            },
            "accepted": {
                "scheme": "exact",
                "network": "eip155:84532",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "amount": "250000",
                "payTo": "0x2222222222222222222222222222222222222222",
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USDC", "version": "2"},
            },
        },
        "paymentRequirements": {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "250000",
            "payTo": "0x2222222222222222222222222222222222222222",
            "maxTimeoutSeconds": 300,
            "resource": "http://127.0.0.1:4021/compute?size=74000",
            "description": "compute",
            "mimeType": "application/json",
            "outputSchema": None,
            "extra": {"name": "USDC", "version": "2"},
        },
    }

    verify = client.post("/verify", json=payload)
    assert verify.status_code == 200
    assert verify.json() == {"isValid": True, "payer": "0xabc"}

    settle = client.post("/settle", json=payload)
    assert settle.status_code == 200
    assert settle.json() == {"success": True, "transaction": "0xsettled"}


def test_exact_facilitator_app_rejects_wrong_version():
    app = create_exact_facilitator_app(
        ExactFacilitatorConfig(
            private_key="0x123",
            rpc_url="https://rpc.example",
            networks=("eip155:84532",),
        ),
        signer_factory=lambda **kwargs: object(),
        facilitator_factory=_FakeFacilitator,
        register_facilitator=lambda facilitator, *, signer, networks: None,
    )

    client = TestClient(app)
    response = client.post(
        "/verify",
        json={"x402Version": 1, "paymentPayload": {}, "paymentRequirements": {}},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only x402Version=2 is supported"
