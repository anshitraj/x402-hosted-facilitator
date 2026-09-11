from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from examples.hosted_facilitator_buyer.pay_seller import (
    _gateway_result_dict,
    _non_payment_required_result,
    _without_gateway_accepts,
)


class _PaymentRequired:
    def __init__(self, accepts: list[dict]):
        self.accepts = accepts

    def model_dump(self, *, by_alias: bool, exclude_none: bool) -> dict:
        assert by_alias is True
        assert exclude_none is True
        return {"x402Version": 2, "accepts": list(self.accepts)}

    @classmethod
    def model_validate(cls, data: dict):
        return cls(list(data["accepts"]))


def test_exact_buyer_example_removes_gateway_accepts_for_exact_mode():
    exact = {"scheme": "exact", "network": "eip155:5042002", "extra": {"name": "USDC"}}
    gateway = {
        "scheme": "exact",
        "network": "eip155:5042002",
        "extra": {"name": "GatewayWalletBatched"},
    }

    filtered = _without_gateway_accepts(_PaymentRequired([gateway, exact]))

    assert filtered.accepts == [exact]


def test_exact_buyer_example_fails_when_only_gateway_is_advertised():
    gateway = {
        "scheme": "exact",
        "network": "eip155:5042002",
        "extra": {"name": "GatewayWalletBatched"},
    }

    with pytest.raises(RuntimeError, match="non-Gateway exact requirement"):
        _without_gateway_accepts(_PaymentRequired([gateway]))


def test_paid_exact_mode_rejects_unprotected_2xx_response():
    result = _non_payment_required_result("exact", httpx.Response(200, json={"ok": True}))

    assert result["success"] is False
    assert result["paymentRequired"] is False
    assert result["httpStatus"] == 200


def test_gateway_mode_requires_actual_nanopayment_transaction():
    result = _gateway_result_dict(
        SimpleNamespace(
            success=True,
            is_nanopayment=False,
            transaction="",
            payer="",
            seller="",
            amount_usdc=None,
            amount_atomic=None,
            network="",
            response_data={"ok": True},
        )
    )

    assert result["success"] is False
    assert result["paymentRequired"] is False
    assert result["transaction"] == ""


def test_gateway_mode_succeeds_only_with_nanopayment_transaction():
    result = _gateway_result_dict(
        SimpleNamespace(
            success=True,
            is_nanopayment=True,
            transaction="transfer-id",
            payer="0xPayer",
            seller="0xSeller",
            amount_usdc="0.001",
            amount_atomic="1000",
            network="eip155:5042002",
            response_data={"ok": True},
        )
    )

    assert result["success"] is True
    assert result["paymentRequired"] is True
    assert result["transaction"] == "transfer-id"
