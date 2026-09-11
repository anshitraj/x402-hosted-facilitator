from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hosted_facilitator.networks import resolve_exact_settlement_network_profile
from hosted_facilitator.x402_compat import (
    get_signed_raw_transaction_bytes,
    patch_x402_web3_compat,
)

patch_x402_web3_compat()

from x402 import x402Facilitator  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_facilitator  # noqa: E402
from x402.mechanisms.evm.signers import FacilitatorWeb3Signer  # noqa: E402
from x402.schemas import PaymentPayload, PaymentRequirements  # noqa: E402


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _required_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    missing = ", ".join(names)
    raise RuntimeError(f"Missing required environment variable. Set one of: {missing}")


def _normalize_tx_hash(tx_hash: Any) -> str:
    value = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    return value if value.startswith("0x") else f"0x{value}"


@dataclass(frozen=True)
class ExactFacilitatorConfig:
    private_key: str
    rpc_url: str
    networks: tuple[str, ...]
    network_profile: str | None = None
    port: int = 4022
    host: str = "0.0.0.0"
    title: str = "OmniClaw Exact Facilitator"


class CompatFacilitatorWeb3Signer(FacilitatorWeb3Signer):
    """Handle eth-account and web3 compatibility differences in this runtime."""

    def write_contract(
        self,
        address: str,
        abi: list[dict[str, Any]],
        function_name: str,
        *args: Any,
    ) -> str:
        from web3 import Web3

        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=abi,
        )
        func = getattr(contract.functions, function_name)
        tx = func(*args).build_transaction(
            {
                "from": self._account.address,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gas": 300000,
                "gasPrice": self._w3.eth.gas_price,
            }
        )
        signed_tx = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(get_signed_raw_transaction_bytes(signed_tx))
        return _normalize_tx_hash(tx_hash)

    def send_transaction(self, to: str, data: bytes) -> str:
        from web3 import Web3

        tx = {
            "from": self._account.address,
            "to": Web3.to_checksum_address(to),
            "data": data,
            "nonce": self._w3.eth.get_transaction_count(self._account.address),
            "gas": 300000,
            "gasPrice": self._w3.eth.gas_price,
        }
        signed_tx = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(get_signed_raw_transaction_bytes(signed_tx))
        return _normalize_tx_hash(tx_hash)


class FacilitatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    x402_version: int = Field(alias="x402Version")
    payment_payload: dict[str, Any] = Field(alias="paymentPayload")
    payment_requirements: dict[str, Any] = Field(alias="paymentRequirements")


def load_exact_facilitator_config_from_env(
    *,
    private_key_env_names: tuple[str, ...] = ("OMNICLAW_X402_FACILITATOR_PRIVATE_KEY",),
) -> ExactFacilitatorConfig:
    profile_name = _env(
        "OMNICLAW_X402_FACILITATOR_NETWORK_PROFILE",
        _env("OMNICLAW_NETWORK", "BASE-SEPOLIA"),
    )
    profile = resolve_exact_settlement_network_profile(profile_name)
    explicit_networks = tuple(
        value.strip()
        for value in _env("OMNICLAW_X402_FACILITATOR_NETWORKS", "").split(",")
        if value.strip()
    )
    networks = explicit_networks or (profile.caip2,)
    rpc_url = _env("OMNICLAW_X402_FACILITATOR_RPC_URL", profile.default_rpc_url or "")
    if not rpc_url:
        raise RuntimeError(
            "Missing OMNICLAW_X402_FACILITATOR_RPC_URL and no default RPC is known for "
            f"{profile.label}"
        )

    return ExactFacilitatorConfig(
        port=int(_env("OMNICLAW_X402_FACILITATOR_PORT", "4022")),
        host=_env("OMNICLAW_X402_FACILITATOR_HOST", "0.0.0.0"),
        rpc_url=rpc_url,
        private_key=_required_env(*private_key_env_names),
        networks=networks,
        network_profile=profile.label,
        title=_env(
            "OMNICLAW_X402_FACILITATOR_TITLE",
            f"OmniClaw Exact Facilitator ({profile.label})",
        ),
    )


def create_exact_facilitator_app(
    config: ExactFacilitatorConfig,
    *,
    signer_factory: Callable[..., Any] = CompatFacilitatorWeb3Signer,
    facilitator_factory: Callable[[], Any] = x402Facilitator,
    register_facilitator: Callable[..., Any] = register_exact_evm_facilitator,
) -> FastAPI:
    app = FastAPI(title=config.title)

    signer = signer_factory(
        private_key=config.private_key,
        rpc_url=config.rpc_url,
    )
    facilitator = facilitator_factory()
    register_facilitator(
        facilitator,
        signer=signer,
        networks=list(config.networks),
    )

    app.state.omniclaw_exact_facilitator_config = config
    app.state.omniclaw_exact_facilitator = facilitator

    @app.get("/supported")
    async def supported() -> dict[str, Any]:
        result = facilitator.get_supported()
        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True, exclude_none=True)
        return result

    @app.post("/verify")
    async def verify(request: FacilitatorRequest) -> dict[str, Any]:
        if request.x402_version != 2:
            raise HTTPException(status_code=400, detail="Only x402Version=2 is supported")

        payload = PaymentPayload.model_validate(request.payment_payload)
        requirements = PaymentRequirements.model_validate(request.payment_requirements)
        result = await facilitator.verify(payload, requirements)
        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True, exclude_none=True)
        return result

    @app.post("/settle")
    async def settle(request: FacilitatorRequest) -> dict[str, Any]:
        if request.x402_version != 2:
            raise HTTPException(status_code=400, detail="Only x402Version=2 is supported")

        payload = PaymentPayload.model_validate(request.payment_payload)
        requirements = PaymentRequirements.model_validate(request.payment_requirements)
        result = await facilitator.settle(payload, requirements)
        if hasattr(result, "model_dump"):
            return result.model_dump(by_alias=True, exclude_none=True)
        return result

    return app
