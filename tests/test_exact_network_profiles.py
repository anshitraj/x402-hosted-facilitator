from __future__ import annotations

from decimal import Decimal

import pytest

from hosted_facilitator.exact import load_exact_facilitator_config_from_env
from hosted_facilitator.networks import (
    build_exact_asset_amount,
    network_to_caip2,
    resolve_exact_settlement_network_profile,
)


def test_arc_profile_defaults_are_supported() -> None:
    profile = resolve_exact_settlement_network_profile("ARC-TESTNET")

    assert profile.label == "ARC-TESTNET"
    assert profile.caip2 == "eip155:5042002"
    assert profile.default_rpc_url == "https://rpc.testnet.arc.network"
    assert profile.explorer_base_url == "https://testnet.arcscan.app/tx/"
    assert profile.default_asset_address == "0x3600000000000000000000000000000000000000"


def test_base_sepolia_aliases_are_supported() -> None:
    profile = resolve_exact_settlement_network_profile("base_sepolia")

    assert profile.label == "BASE-SEPOLIA"
    assert profile.caip2 == "eip155:84532"
    assert network_to_caip2("base-sepolia") == "eip155:84532"
    assert network_to_caip2("eip155:84532") == "eip155:84532"


def test_exact_asset_amount_uses_profile_decimals() -> None:
    profile = resolve_exact_settlement_network_profile("ARC-TESTNET")

    amount = build_exact_asset_amount(
        profile=profile,
        decimal_amount=Decimal("0.25"),
        network="eip155:5042002",
    )

    assert amount is not None
    assert amount.amount == "250000"
    assert amount.asset == "0x3600000000000000000000000000000000000000"
    assert amount.extra == {"name": "USDC", "version": "2"}


def test_exact_asset_amount_returns_none_for_nonmatching_network() -> None:
    profile = resolve_exact_settlement_network_profile("ARC-TESTNET")

    assert (
        build_exact_asset_amount(
            profile=profile,
            decimal_amount="0.25",
            network="eip155:84532",
        )
        is None
    )


def test_exact_asset_amount_rejects_unrepresentable_values() -> None:
    profile = resolve_exact_settlement_network_profile("ARC-TESTNET")

    with pytest.raises(ValueError, match="cannot be represented"):
        build_exact_asset_amount(
            profile=profile,
            decimal_amount="0.0000001",
            network="eip155:5042002",
        )


def test_load_exact_config_from_env_uses_profile_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_NETWORK_PROFILE", "ARC-TESTNET")
    monkeypatch.delenv("OMNICLAW_X402_FACILITATOR_RPC_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_X402_FACILITATOR_NETWORKS", raising=False)

    config = load_exact_facilitator_config_from_env(
        private_key_env_names=("OMNICLAW_X402_FACILITATOR_PRIVATE_KEY",)
    )

    assert config.network_profile == "ARC-TESTNET"
    assert config.rpc_url == "https://rpc.testnet.arc.network"
    assert config.networks == ("eip155:5042002",)
