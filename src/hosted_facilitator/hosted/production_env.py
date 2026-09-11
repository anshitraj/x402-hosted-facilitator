from __future__ import annotations

import json
import os
import re
from typing import Any

from eth_account import Account

from hosted_facilitator.hosted.providers.circle_gateway import (
    create_hosted_circle_gateway_provider_from_env,
)
from hosted_facilitator.hosted.runner import (
    _assert_hosted_runtime_env_safe,
    _hosted_exact_max_concurrent_settlements,
    _hosted_exact_provider_timeout_seconds,
    load_hosted_exact_runtime_config_from_env,
)

_EVM_PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_PLACEHOLDER_SECRET_MARKERS = (
    "change-before-production",
    "example",
    "placeholder",
    "replace",
)


def validate_production_env() -> dict[str, Any]:
    """Validate hosted production runtime config without opening external sockets."""

    mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    if mode != "production":
        raise RuntimeError(
            "Production env validation requires OMNICLAW_HOSTED_FACILITATOR_MODE=production"
        )
    _assert_hosted_runtime_env_safe()
    _validate_production_secret_material()
    _validate_production_pool_config()
    _validate_optional_operational_env()
    runtime_config = load_hosted_exact_runtime_config_from_env()
    circle_provider = create_hosted_circle_gateway_provider_from_env()
    return {
        "status": "ok",
        "exactNetworks": list(runtime_config.exact_config.networks),
        "configuredExactNetworks": [
            network for config in runtime_config.network_configs for network in config.networks
        ],
        "exactProviderTimeoutSeconds": _hosted_exact_provider_timeout_seconds(),
        "exactMaxConcurrentSettlements": _hosted_exact_max_concurrent_settlements(),
        "signerId": runtime_config.signer_guard.config.signer_id,
        "circleGatewayEnabled": circle_provider is not None,
        "circleGatewayNetworks": list(circle_provider.supported_networks)
        if circle_provider
        else [],
    }


def _validate_production_secret_material() -> None:
    signer_key_env = (
        os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV", "").strip()
        or "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY"
    )
    signer_key = os.environ.get(signer_key_env, "").strip()
    if not _EVM_PRIVATE_KEY_RE.fullmatch(signer_key):
        raise RuntimeError(f"{signer_key_env} must contain one 32-byte EVM private key")
    hex_key = signer_key[2:].lower()
    if len(set(hex_key)) < 8 or _is_repeated_key_pattern(hex_key):
        raise RuntimeError(f"{signer_key_env} looks like placeholder or low-entropy key material")
    try:
        signer_address = Account.from_key(signer_key).address
    except Exception as exc:
        raise RuntimeError(f"{signer_key_env} is not a usable EVM private key") from exc
    configured_address = os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "").strip()
    if configured_address and configured_address.lower() != signer_address.lower():
        raise RuntimeError("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS does not match signer private key")
    for name in ("OMNICLAW_HOSTED_DEFAULT_API_KEY",):
        if os.environ.get(name, "").strip():
            raise RuntimeError(
                f"{name} is not supported in hosted mode; issue seller-scoped keys "
                "through the control plane"
            )
    api_key_pepper = os.environ.get("OMNICLAW_HOSTED_API_KEY_HASH_PEPPER", "").strip()
    if (
        len(api_key_pepper) < 32
        or _is_repeated_secret(api_key_pepper)
        or _is_placeholder_secret(api_key_pepper)
    ):
        raise RuntimeError(
            "OMNICLAW_HOSTED_API_KEY_HASH_PEPPER must be a real 32+ character random secret"
        )
    session_secret = os.environ.get("OMNICLAW_OPS_SESSION_SECRET", "").strip()
    if session_secret:
        if (
            session_secret == "replace-with-32-byte-random-secret"
            or len(session_secret) < 32
            or _is_repeated_secret(session_secret)
            or _is_placeholder_secret(session_secret)
        ):
            raise RuntimeError("OMNICLAW_OPS_SESSION_SECRET must be a real 32+ byte random secret")
    openfga_token = os.environ.get("OMNICLAW_HOSTED_OPENFGA_API_TOKEN", "").strip()
    if openfga_token and len(openfga_token) < 20:
        raise RuntimeError("OMNICLAW_HOSTED_OPENFGA_API_TOKEN is too short for hosted mode")


def _is_repeated_key_pattern(hex_key: str) -> bool:
    for chunk_size in (2, 4, 8, 16, 32):
        if len(hex_key) % chunk_size == 0:
            chunk = hex_key[:chunk_size]
            if chunk * (len(hex_key) // chunk_size) == hex_key:
                return True
    return False


def _is_repeated_secret(value: str) -> bool:
    if len(set(value)) < 8:
        return True
    for chunk_size in (1, 2, 4, 8, 16):
        if len(value) % chunk_size == 0:
            chunk = value[:chunk_size]
            if chunk * (len(value) // chunk_size) == value:
                return True
    return False


def _is_placeholder_secret(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_SECRET_MARKERS)


def _validate_production_pool_config() -> None:
    _required_positive_int("OMNICLAW_HOSTED_POSTGRES_POOL_SIZE")
    _required_positive_float("OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS")


def _validate_optional_operational_env() -> None:
    signer_address = os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "").strip()
    if signer_address and not _EVM_ADDRESS_RE.fullmatch(signer_address):
        raise RuntimeError("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS must be an EVM address")
    canary_address = os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", "").strip()
    if canary_address and not _EVM_ADDRESS_RE.fullmatch(canary_address):
        raise RuntimeError("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS must be an EVM address")
    canary_min = os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "").strip()
    if canary_min:
        _positive_int_value("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", canary_min)


def _required_positive_int(name: str) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in hosted production mode")
    return _positive_int_value(name, value)


def _positive_int_value(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed


def _required_positive_float(name: str) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in hosted production mode")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be positive") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive")
    return parsed


def main() -> None:
    print(json.dumps(validate_production_env(), sort_keys=True))


if __name__ == "__main__":
    main()
