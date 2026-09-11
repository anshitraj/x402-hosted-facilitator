from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.hosted_facilitator_doh import install_trycloudflare_doh_resolver

from hosted_facilitator.nanopayments.adapter import NanopaymentAdapter
from hosted_facilitator.nanopayments.client import NanopaymentClient
from hosted_facilitator.x402_compat import patch_x402_web3_compat
from hosted_facilitator.x402_exact import HostedExactClientSigner

ARC_TESTNET_NETWORK = "eip155:5042002"


def _load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"env file not found: {path}")
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _buyer_private_key() -> str:
    private_key = (
        os.environ.get("BUYER_OMNICLAW_PRIVATE_KEY") or os.environ.get("OMNICLAW_PRIVATE_KEY") or ""
    ).strip()
    if not private_key:
        raise RuntimeError("Set BUYER_OMNICLAW_PRIVATE_KEY or OMNICLAW_PRIVATE_KEY")
    return private_key if private_key.startswith("0x") else f"0x{private_key}"


def _decode_payment_required(response: httpx.Response) -> dict[str, Any]:
    header = response.headers.get("payment-required") or response.headers.get("PAYMENT-REQUIRED")
    if not header:
        raise RuntimeError(f"Expected PAYMENT-REQUIRED header, got HTTP {response.status_code}")
    return json.loads(base64.b64decode(header))


def _summarize_accepts(payment_required: dict[str, Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for accept in payment_required.get("accepts", []):
        extra = accept.get("extra") or {}
        summary.append(
            {
                "scheme": accept.get("scheme"),
                "network": accept.get("network"),
                "asset": accept.get("asset"),
                "amount": accept.get("amount"),
                "payTo": accept.get("payTo"),
                "provider": "circle_gateway"
                if extra.get("name") == "GatewayWalletBatched"
                else "exact_evm",
                "extraName": extra.get("name"),
            }
        )
    return summary


async def inspect_seller(url: str, method: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(method, url)
    if response.status_code != 402:
        return {
            "httpStatus": response.status_code,
            "paymentRequired": False,
            "body": _safe_body(response),
        }
    payment_required = _decode_payment_required(response)
    return {
        "httpStatus": response.status_code,
        "paymentRequired": True,
        "accepts": _summarize_accepts(payment_required),
    }


async def pay_gateway(url: str, method: str, network: str) -> dict[str, Any]:
    client = NanopaymentClient(
        environment=os.environ.get("OMNICLAW_NANOPAYMENTS_ENVIRONMENT", "testnet"),
    )
    async with httpx.AsyncClient(timeout=60.0) as http:
        adapter = NanopaymentAdapter.from_private_key(
            private_key=_buyer_private_key(),
            nanopayment_client=client,
            http_client=http,
            network=network,
            auto_topup_enabled=False,
            strict_settlement=True,
        )
        result = await adapter.pay_x402_url(url=url, method=method)
    return _gateway_result_dict(result)


def _gateway_result_dict(result: Any) -> dict[str, Any]:
    transaction = str(result.transaction or "")
    success = bool(result.success and result.is_nanopayment and transaction)
    return {
        "mode": "gateway",
        "success": success,
        "payer": result.payer,
        "seller": result.seller,
        "transaction": transaction,
        "amountUSDC": result.amount_usdc,
        "amountAtomic": result.amount_atomic,
        "network": result.network,
        "isNanopayment": result.is_nanopayment,
        "paymentRequired": bool(result.is_nanopayment),
        "response": _safe_json_or_text(result.response_data),
    }


async def pay_exact(
    url: str, method: str, network: str, max_amount_usdc: Decimal
) -> dict[str, Any]:
    patch_x402_web3_compat()

    from x402.client import max_amount, prefer_network, prefer_scheme, x402Client
    from x402.http import x402HTTPClient
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentPayload

    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(HostedExactClientSigner(_buyer_private_key())))
    client.register_policy(prefer_scheme("exact"))
    client.register_policy(prefer_network(network))
    client.register_policy(max_amount(int(max_amount_usdc * Decimal(10**6))))

    selected_state: dict[str, Any] = {}

    async def capture_selection(ctx: Any) -> None:
        selected_state["requirements"] = ctx.selected_requirements

    client.on_after_payment_creation(capture_selection)
    x402_http = x402HTTPClient(client)

    async with httpx.AsyncClient(timeout=60.0) as http:
        initial = await http.request(method, url)
        if initial.status_code != 402:
            return _non_payment_required_result("exact", initial)

        await initial.aread()
        get_header, body_data = x402_http._handle_402_common(
            dict(initial.headers),
            initial.content or None,
        )
        payment_required = x402_http.get_payment_required_response(get_header, body_data)
        payment_required = _without_gateway_accepts(payment_required)
        payment_payload = await x402_http.create_payment_payload(payment_required)
        selected_requirements = (
            selected_state.get("requirements")
            or getattr(payment_payload, "accepted", None)
            or payment_required.accepts[0]
        )

        if getattr(payment_payload, "accepted", None) is None:
            payload_data = payment_payload.model_dump(by_alias=True, exclude_none=True)
            payload_data["accepted"] = selected_requirements
            if not payload_data.get("resource") and getattr(payment_required, "resource", None):
                payload_data["resource"] = payment_required.resource
            payment_payload = PaymentPayload.model_validate(payload_data)

        retry_headers = x402_http.encode_payment_signature_header(payment_payload)
        final = await http.request(method, url, headers=retry_headers)

    settle_response = None
    settle_error = None
    try:
        settle_response = x402_http.get_payment_settle_response(
            lambda name: final.headers.get(name)
        )
    except Exception as exc:  # pragma: no cover - diagnostic output path
        settle_error = str(exc)

    settlement = _settlement_dict(settle_response)
    settlement_transaction = settlement.get("transaction") if settlement else ""
    success = bool(
        200 <= final.status_code < 300
        and settlement
        and settlement.get("success")
        and settlement_transaction
    )
    return {
        "mode": "exact",
        "success": success,
        "httpStatus": final.status_code,
        "paymentRequired": True,
        "selected": {
            "scheme": str(selected_requirements.scheme),
            "network": str(selected_requirements.network),
            "asset": str(selected_requirements.asset),
            "amount": str(selected_requirements.amount),
            "payTo": str(selected_requirements.pay_to),
            "extra": selected_requirements.extra,
        },
        "settlement": settlement,
        "settlementError": settle_error,
        "response": _safe_body(final),
    }


def _non_payment_required_result(mode: str, response: httpx.Response) -> dict[str, Any]:
    return {
        "mode": mode,
        "success": False,
        "httpStatus": response.status_code,
        "paymentRequired": False,
        "note": "paid QA mode requires a 402 x402 challenge before retrying",
        "response": _safe_body(response),
    }


def _without_gateway_accepts(payment_required: Any) -> Any:
    data = payment_required.model_dump(by_alias=True, exclude_none=True)
    accepts = [
        accept
        for accept in data.get("accepts", [])
        if not (
            isinstance(accept.get("extra"), dict)
            and accept["extra"].get("name") == "GatewayWalletBatched"
        )
    ]
    if not accepts:
        raise RuntimeError("seller did not advertise a non-Gateway exact requirement")
    data["accepts"] = accepts
    return payment_required.__class__.model_validate(data)


def _settlement_dict(settle_response: Any) -> dict[str, Any] | None:
    if settle_response is None:
        return None
    return {
        "success": bool(getattr(settle_response, "success", False)),
        "transaction": str(getattr(settle_response, "transaction", "") or ""),
        "network": str(getattr(settle_response, "network", "") or ""),
        "payer": str(getattr(settle_response, "payer", "") or ""),
    }


def _safe_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def _safe_json_or_text(value: Any) -> Any:
    if isinstance(value, (dict, list)) or value is None:
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Pay an external x402 seller API")
    parser.add_argument("--url", default="http://127.0.0.1:4023/compute")
    parser.add_argument("--mode", choices=("inspect", "gateway", "exact"), default="inspect")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--network", default=ARC_TESTNET_NETWORK)
    parser.add_argument("--max-amount", default="1.00")
    parser.add_argument("--env-file", action="append", default=[])
    args = parser.parse_args()

    for env_file in args.env_file:
        _load_env_file(env_file)

    install_trycloudflare_doh_resolver()

    method = args.method.upper()
    if args.mode == "inspect":
        result = await inspect_seller(args.url, method)
    elif args.mode == "gateway":
        result = await pay_gateway(args.url, method, args.network)
    else:
        result = await pay_exact(args.url, method, args.network, Decimal(args.max_amount))

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success", True) else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
