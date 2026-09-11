from __future__ import annotations

import pytest

from hosted_facilitator.hosted.production_env import validate_production_env


def _production_safe_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_NETWORK_PROFILE", "ARC-TESTNET")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_PROVIDER_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS", "1")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY",
        "0xa9832e80f4c8edc8d0590aa6b91ee03e4cbc60dd24c55a44ab18c58460dea9db",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV",
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-prod")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ENVIRONMENT", "production")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_STATUS", "active")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100000000000000")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://svc:secret@pg.example.com:5432/settlement?sslmode=require",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN",
        "postgresql://svc:secret@pg.example.com:5432/control?sslmode=require",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "postgresql://svc:secret@pg.example.com:5432/control?sslmode=require",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_POSTGRES_POOL_SIZE", "10")
    monkeypatch.setenv("OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS", "2")
    monkeypatch.setenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", "rediss://redis.example.com:6379/0")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_ISSUER", "https://idp.example.com/realms/omniclaw")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", "omniclaw-ops")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", "https://idp.example.com/jwks")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_URL", "https://openfga.example.com")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", "store-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", "model-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENVIRONMENT", "testnet")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS", "eip155:5042002")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL", "https://gateway-api-testnet.circle.com"
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_API_KEY_HASH_PEPPER",
        "prod-api-key-pepper-9c7c8e3d4a1b2f6a",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_OTEL_REQUIRED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_JSON_LOGS", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.example.com")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL", "https://otel.example.com/health"
    )


def test_production_env_validator_accepts_production_safe_config(monkeypatch):
    _production_safe_env(monkeypatch)

    summary = validate_production_env()

    assert summary == {
        "status": "ok",
        "exactNetworks": ["eip155:5042002"],
        "configuredExactNetworks": ["eip155:5042002"],
        "exactProviderTimeoutSeconds": 30.0,
        "exactMaxConcurrentSettlements": 1,
        "signerId": "hosted-exact-prod",
        "circleGatewayEnabled": True,
        "circleGatewayNetworks": ["eip155:5042002"],
    }


def test_production_env_validator_rejects_local_mode_bypass(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "local")

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_FACILITATOR_MODE=production" in str(exc)
    else:
        raise AssertionError("expected production validator to reject local mode")


def test_production_env_validator_rejects_insecure_redis(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", "redis://redis:6379/0")

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL" in str(exc)
    else:
        raise AssertionError("expected production validator to reject insecure Redis")


def test_production_env_validator_rejects_split_control_and_seller_databases(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "postgresql://seller:secret@pg.example.com:5432/seller_accounts?sslmode=require",
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "must point at the same Postgres database" in str(exc)
    else:
        raise AssertionError("expected production validator to reject split seller database")


def test_production_env_validator_rejects_split_control_and_seller_database_users(
    monkeypatch,
):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "postgresql://seller:secret@pg.example.com:5432/control?sslmode=require",
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "must point at the same Postgres database" in str(exc)
    else:
        raise AssertionError("expected production validator to reject split seller database user")


def test_production_env_validator_rejects_split_control_and_seller_database_options(
    monkeypatch,
):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "postgresql://svc:secret@pg.example.com:5432/control?sslmode=require&options=-csearch_path%3Dseller",
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "must point at the same Postgres database" in str(exc)
    else:
        raise AssertionError(
            "expected production validator to reject split seller database options"
        )


def test_production_env_validator_rejects_placeholder_signer_key(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY",
        "0x1111111111111111111111111111111111111111111111111111111111111111",
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "low-entropy key material" in str(exc)
    else:
        raise AssertionError("expected production validator to reject placeholder signer key")


def test_production_env_validator_rejects_repeated_chunk_signer_key(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY",
        "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "low-entropy key material" in str(exc)
    else:
        raise AssertionError("expected production validator to reject repeated signer key")


def test_production_env_validator_rejects_signer_address_mismatch(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "0x0000000000000000000000000000000000000001"
    )

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS" in str(exc)
    else:
        raise AssertionError("expected production validator to reject signer address mismatch")


def test_production_env_validator_rejects_malformed_signer_key(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "not-a-private-key")

    with pytest.raises(RuntimeError, match="32-byte EVM private key"):
        validate_production_env()


def test_production_env_validator_requires_postgres_pool_config(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.delenv("OMNICLAW_HOSTED_POSTGRES_POOL_SIZE")

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_POSTGRES_POOL_SIZE" in str(exc)
    else:
        raise AssertionError("expected production validator to require pool sizing")


def test_production_env_validator_rejects_bad_pool_checkout(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS", "0")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS"):
        validate_production_env()


def test_production_env_validator_rejects_bad_optional_operational_addresses(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", "not-an-address")

    try:
        validate_production_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS" in str(exc)
    else:
        raise AssertionError("expected production validator to reject invalid canary address")


def test_production_env_validator_rejects_bad_signer_address(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "not-an-address")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS"):
        validate_production_env()


def test_production_env_validator_rejects_bad_gateway_canary_minimum(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "0")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC"):
        validate_production_env()


def test_production_env_validator_rejects_configured_default_api_key(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_DEFAULT_API_KEY", "omck_forbidden_default")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_DEFAULT_API_KEY"):
        validate_production_env()


def test_production_env_validator_requires_api_key_hash_pepper(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.delenv("OMNICLAW_HOSTED_API_KEY_HASH_PEPPER")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_API_KEY_HASH_PEPPER"):
        validate_production_env()


def test_production_env_validator_rejects_low_entropy_api_key_hash_pepper(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_API_KEY_HASH_PEPPER", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_API_KEY_HASH_PEPPER"):
        validate_production_env()


def test_production_env_validator_rejects_placeholder_api_key_hash_pepper(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_API_KEY_HASH_PEPPER",
        "replace-with-32-plus-character-random-pepper",
    )

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_API_KEY_HASH_PEPPER"):
        validate_production_env()


def test_production_env_validator_rejects_placeholder_ops_session_secret(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_OPS_SESSION_SECRET", "replace-with-32-byte-random-secret")

    with pytest.raises(RuntimeError, match="OMNICLAW_OPS_SESSION_SECRET"):
        validate_production_env()


def test_production_env_validator_rejects_short_ops_session_secret(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_OPS_SESSION_SECRET", "short")

    with pytest.raises(RuntimeError, match="OMNICLAW_OPS_SESSION_SECRET"):
        validate_production_env()


def test_production_env_validator_rejects_short_openfga_token(monkeypatch):
    _production_safe_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_TOKEN", "short")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_OPENFGA_API_TOKEN"):
        validate_production_env()
