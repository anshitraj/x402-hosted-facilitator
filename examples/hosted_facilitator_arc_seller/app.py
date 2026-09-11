from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import httpx
from examples.hosted_facilitator_doh import install_trycloudflare_doh_resolver
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from web3 import Web3
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import AssetAmount, Network
from x402.server import x402ResourceServer

ARC_TESTNET_NETWORK: Network = "eip155:5042002"
ARC_TESTNET_USDC_FALLBACK = "0x3600000000000000000000000000000000000000"

install_trycloudflare_doh_resolver()


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _seller_address() -> str:
    explicit = _env("X402_SELLER_PAY_TO") or _env("SELLER_ADDRESS")
    if explicit:
        return Web3.to_checksum_address(explicit)
    raise RuntimeError("Set X402_SELLER_PAY_TO or SELLER_ADDRESS")


def _facilitator_url() -> str:
    return _env("X402_FACILITATOR_URL", "http://127.0.0.1:4022").rstrip("/")


def _auth_headers() -> dict[str, str]:
    api_key = _env("X402_FACILITATOR_API_KEY")
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _facilitator_client() -> HTTPFacilitatorClient:
    headers = _auth_headers()
    if not headers:
        return HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))

    def create_headers() -> dict[str, dict[str, str]]:
        return {"supported": headers, "verify": headers, "settle": headers}

    return HTTPFacilitatorClient({"url": FACILITATOR_URL, "create_headers": create_headers})


def _supported_kinds() -> list[dict[str, Any]]:
    response = httpx.get(
        f"{FACILITATOR_URL}/v2/x402/supported",
        headers=_auth_headers(),
        timeout=15.0,
    )
    response.raise_for_status()
    kinds = response.json().get("kinds", [])
    if not isinstance(kinds, list):
        raise RuntimeError("Facilitator /supported returned malformed kinds")
    return [dict(kind) for kind in kinds if isinstance(kind, dict)]


def _kind_asset(kind: dict[str, Any]) -> str:
    asset = kind.get("asset")
    if isinstance(asset, str) and asset:
        return asset
    extra = kind.get("extra")
    assets = extra.get("assets") if isinstance(extra, dict) else None
    if isinstance(assets, list):
        for candidate in assets:
            if (
                isinstance(candidate, dict)
                and candidate.get("symbol") == "USDC"
                and isinstance(candidate.get("address"), str)
            ):
                return candidate["address"]
    if kind.get("network") == ARC_TESTNET_NETWORK:
        return ARC_TESTNET_USDC_FALLBACK
    return ""


def _asset_for_network(network: str) -> str:
    for kind in SUPPORTED_KINDS:
        if kind.get("network") == network:
            asset = _kind_asset(kind)
            if asset:
                return asset
    if network == ARC_TESTNET_NETWORK:
        return ARC_TESTNET_USDC_FALLBACK
    return ""


def _arc_usdc_amount(decimal_amount: float | str | Decimal, network: str) -> AssetAmount | None:
    if network != ARC_TESTNET_NETWORK:
        return None
    asset = _asset_for_network(network)
    if not asset:
        return None
    scaled = Decimal(str(decimal_amount)) * Decimal(10**6)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Arc USDC amount {decimal_amount} cannot be represented with 6 decimals")
    return AssetAmount(
        amount=str(int(scaled)),
        asset=asset,
        extra={"name": "USDC", "version": "2"},
    )


def _accept_mode() -> str:
    value = _env("X402_ACCEPT_MODE", "all").lower()
    if value not in {"all", "exact", "gateway"}:
        raise RuntimeError("X402_ACCEPT_MODE must be all, exact, or gateway")
    return value


def _payment_options() -> list[PaymentOption]:
    mode = _accept_mode()
    options: list[PaymentOption] = []
    for kind in SUPPORTED_KINDS:
        if kind.get("scheme") != "exact" or kind.get("network") != ARC_TESTNET_NETWORK:
            continue
        extra = kind.get("extra") if isinstance(kind.get("extra"), dict) else {}
        is_gateway = extra.get("name") == "GatewayWalletBatched"
        if mode == "exact" and is_gateway:
            continue
        if mode == "gateway" and not is_gateway:
            continue
        if not _kind_asset(kind):
            continue
        options.append(
            PaymentOption(
                scheme="exact",
                pay_to=PAY_TO,
                price=PRICE,
                network=ARC_TESTNET_NETWORK,
                max_timeout_seconds=604900 if is_gateway else 345600,
                extra=dict(extra) if extra else None,
            )
        )
    if not options:
        raise RuntimeError("No usable Arc x402 rails were returned by the hosted facilitator")
    return options


def _payment_option_summary(option: PaymentOption) -> dict[str, Any]:
    extra = option.extra or {}
    return {
        "scheme": option.scheme,
        "network": option.network,
        "asset": _asset_for_network(str(option.network)),
        "price": PRICE,
        "payTo": option.pay_to,
        "maxTimeoutSeconds": option.max_timeout_seconds,
        "provider": "circle_gateway"
        if extra.get("name") == "GatewayWalletBatched"
        else "exact_evm",
        "extra": extra or None,
    }


def _install_extra_aware_matching(resource_server: Any) -> None:
    def find_matching_requirements(available: list[Any], payload: Any) -> Any | None:
        accepted = getattr(payload, "accepted", None)
        if accepted is None:
            return None
        for requirement in available:
            if (
                _field(accepted, "scheme") == _field(requirement, "scheme")
                and _field(accepted, "network") == _field(requirement, "network")
                and _field(accepted, "amount") == _field(requirement, "amount")
                and _field(accepted, "asset").lower() == _field(requirement, "asset").lower()
                and _field(accepted, "pay_to", "payTo").lower()
                == _field(requirement, "pay_to", "payTo").lower()
                and _canonical_extra(_field(accepted, "extra"))
                == _canonical_extra(_field(requirement, "extra"))
            ):
                return requirement
        return None

    resource_server.find_matching_requirements = find_matching_requirements


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return ""


def _canonical_extra(extra: Any) -> str:
    if isinstance(extra, dict) and extra.get("name") == "GatewayWalletBatched":
        comparable = {
            "name": extra.get("name", ""),
            "version": extra.get("version", ""),
            "verifyingContract": extra.get("verifyingContract", ""),
            "minValiditySeconds": extra.get("minValiditySeconds"),
            "assets": extra.get("assets", []),
        }
        return json.dumps(comparable, sort_keys=True, separators=(",", ":"))
    return json.dumps(extra or {}, sort_keys=True, separators=(",", ":"))


FACILITATOR_URL = _facilitator_url()
PRICE = _env("X402_PRICE", "$0.001")
PAY_TO = _seller_address()
SUPPORTED_KINDS = _supported_kinds()
PAYMENT_OPTIONS = _payment_options()

app = FastAPI(title="Hosted Facilitator Arc Seller Example")

facilitator = _facilitator_client()
server = x402ResourceServer(facilitator)
exact = ExactEvmServerScheme()
exact.register_money_parser(_arc_usdc_amount)
server.register("eip155:*", exact)
_install_extra_aware_matching(server)
server.initialize()

routes: dict[str, RouteConfig] = {
    "GET /compute": RouteConfig(
        accepts=PAYMENT_OPTIONS,
        mime_type="application/json",
        description="Arc seller compute endpoint using the hosted facilitator",
    )
}

app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "hosted-facilitator-arc-seller-example",
        "facilitator": FACILITATOR_URL,
        "network": ARC_TESTNET_NETWORK,
        "price": PRICE,
        "payTo": PAY_TO,
        "acceptMode": _accept_mode(),
        "acceptCount": len(PAYMENT_OPTIONS),
    }


@app.get("/rails")
async def rails() -> dict[str, Any]:
    return {
        "facilitator": FACILITATOR_URL,
        "network": ARC_TESTNET_NETWORK,
        "acceptMode": _accept_mode(),
        "supportedKinds": SUPPORTED_KINDS,
        "advertisedPaymentOptions": [_payment_option_summary(option) for option in PAYMENT_OPTIONS],
    }


@app.get("/compute")
async def compute(size: int = 70000) -> JSONResponse:
    return JSONResponse(
        {
            "service": "hosted-facilitator-arc-seller-example",
            "network": ARC_TESTNET_NETWORK,
            "asset": _asset_for_network(ARC_TESTNET_NETWORK),
            "price": PRICE,
            "payTo": PAY_TO,
            "input": {"size": size},
            "output": {"primeCount": _prime_count(size)},
        }
    )


def _prime_count(limit: int) -> int:
    if limit < 2:
        return 0
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    n = 2
    while n * n <= limit:
        if sieve[n]:
            start = n * n
            step = n
            sieve[start : limit + 1 : step] = b"\x00" * (((limit - start) // step) + 1)
        n += 1
    return int(sum(sieve))
