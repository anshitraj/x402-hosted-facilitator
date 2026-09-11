from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from x402.schemas import AssetAmount


@dataclass(frozen=True)
class ExactSettlementNetworkProfile:
    network: str
    caip2: str
    label: str
    default_rpc_url: str | None = None
    explorer_base_url: str | None = None
    default_asset_address: str | None = None
    default_asset_name: str = "USDC"
    default_asset_version: str = "2"
    default_asset_decimals: int = 6


_PROFILES: dict[str, ExactSettlementNetworkProfile] = {}
_ALIASES: dict[str, str] = {}


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _register(
    label: str,
    *,
    caip2: str,
    default_rpc_url: str,
    explorer_base_url: str,
    default_asset_address: str,
    aliases: tuple[str, ...] = (),
) -> None:
    profile = ExactSettlementNetworkProfile(
        network=label,
        caip2=caip2,
        label=label,
        default_rpc_url=default_rpc_url,
        explorer_base_url=explorer_base_url,
        default_asset_address=default_asset_address,
    )
    _PROFILES[label] = profile
    for alias in (label, caip2, *aliases):
        _ALIASES[_normalize_key(alias)] = label


_register(
    "ETH",
    caip2="eip155:1",
    default_rpc_url="https://ethereum-rpc.publicnode.com",
    explorer_base_url="https://etherscan.io/tx/",
    default_asset_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    aliases=("ethereum", "mainnet"),
)
_register(
    "ETH-SEPOLIA",
    caip2="eip155:11155111",
    default_rpc_url="https://ethereum-sepolia-rpc.publicnode.com",
    explorer_base_url="https://sepolia.etherscan.io/tx/",
    default_asset_address="0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
    aliases=("sepolia",),
)
_register(
    "BASE",
    caip2="eip155:8453",
    default_rpc_url="https://mainnet.base.org",
    explorer_base_url="https://basescan.org/tx/",
    default_asset_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
)
_register(
    "BASE-SEPOLIA",
    caip2="eip155:84532",
    default_rpc_url="https://sepolia.base.org",
    explorer_base_url="https://sepolia.basescan.org/tx/",
    default_asset_address="0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    aliases=("base_sepolia",),
)
_register(
    "OP",
    caip2="eip155:10",
    default_rpc_url="https://mainnet.optimism.io",
    explorer_base_url="https://optimistic.etherscan.io/tx/",
    default_asset_address="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    aliases=("optimism",),
)
_register(
    "OP-SEPOLIA",
    caip2="eip155:11155420",
    default_rpc_url="https://sepolia.optimism.io",
    explorer_base_url="https://sepolia-optimism.etherscan.io/tx/",
    default_asset_address="0x5fd84259d66Cd46123540766Be93DFE6D43130D7",
    aliases=("optimism-sepolia",),
)
_register(
    "ARB",
    caip2="eip155:42161",
    default_rpc_url="https://arb1.arbitrum.io/rpc",
    explorer_base_url="https://arbiscan.io/tx/",
    default_asset_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    aliases=("arbitrum",),
)
_register(
    "ARB-SEPOLIA",
    caip2="eip155:421614",
    default_rpc_url="https://sepolia-rollup.arbitrum.io/rpc",
    explorer_base_url="https://sepolia.arbiscan.io/tx/",
    default_asset_address="0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",
    aliases=("arbitrum-sepolia",),
)
_register(
    "UNI",
    caip2="eip155:130",
    default_rpc_url="https://mainnet.unichain.org",
    explorer_base_url="https://uniscan.xyz/tx/",
    default_asset_address="0x078D782b760474a361dDA0AF3839290b0EF57AD6",
    aliases=("unichain",),
)
_register(
    "UNI-SEPOLIA",
    caip2="eip155:1301",
    default_rpc_url="https://sepolia.unichain.org",
    explorer_base_url="https://sepolia.uniscan.xyz/tx/",
    default_asset_address="0x31d0220469e10c4E71834a79b1f276d740d3768F",
    aliases=("unichain-sepolia",),
)
_register(
    "ARC-TESTNET",
    caip2="eip155:5042002",
    default_rpc_url="https://rpc.testnet.arc.network",
    explorer_base_url="https://testnet.arcscan.app/tx/",
    default_asset_address="0x3600000000000000000000000000000000000000",
    aliases=("arc", "arc-testnet", "arc_testnet"),
)


def resolve_exact_settlement_network_profile(
    network: str | None,
) -> ExactSettlementNetworkProfile:
    label = _ALIASES.get(_normalize_key(network or "BASE-SEPOLIA"))
    if label is None:
        supported = ", ".join(sorted(_PROFILES))
        raise ValueError(f"Unsupported exact settlement network: {network}. Supported: {supported}")
    return _PROFILES[label]


def network_to_caip2(network: str | None) -> str | None:
    if network is None:
        return None
    label = _ALIASES.get(_normalize_key(network))
    if label is None:
        return network.lower() if ":" in network else None
    return _PROFILES[label].caip2


def build_exact_asset_amount(
    *,
    profile: ExactSettlementNetworkProfile,
    decimal_amount: float | str | Decimal,
    network: str,
) -> AssetAmount | None:
    if network != profile.caip2 or not profile.default_asset_address:
        return None

    amount_decimal = Decimal(str(decimal_amount))
    scaled = amount_decimal * (Decimal(10) ** profile.default_asset_decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"Amount {decimal_amount} cannot be represented with "
            f"{profile.default_asset_decimals} decimals"
        )

    return AssetAmount(
        amount=str(int(scaled)),
        asset=profile.default_asset_address,
        extra={
            "name": profile.default_asset_name,
            "version": profile.default_asset_version,
        },
    )
