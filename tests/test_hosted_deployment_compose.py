from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_COMPOSE_PATH = ROOT / "docker-compose.yml"
COMPOSE_PATH = ROOT / "infra" / "compose" / "docker-compose.production.yml"
ENV_EXAMPLE_PATH = ROOT / "infra" / "compose" / "production.env.example"
COMPOSE_ENV_EXAMPLE_PATH = ROOT / "infra" / "compose" / "production.compose.env.example"
METRICS_ENV_EXAMPLE_PATH = ROOT / "infra" / "compose" / "production.metrics.env.example"
OPS_ENV_EXAMPLE_PATH = ROOT / "infra" / "compose" / "production.ops.env.example"
PROMETHEUS_PRODUCTION_PATH = ROOT / "infra" / "prometheus" / "prometheus.production.yml"
ALERTS_PRODUCTION_PATH = ROOT / "infra" / "alerts" / "hosted-facilitator-alerts.production.yaml"
DEPLOYMENT_DOC_PATH = ROOT / "docs" / "deployment-docker-compose.md"
OPS_RUNBOOK_PATH = ROOT / "docs" / "runbooks" / "hosted-facilitator-ops.md"
README_PATH = ROOT / "README.md"
OPS_DOCKERFILE_PATH = ROOT / "apps" / "ops-console" / "Dockerfile"


def test_repository_compose_keycloak_uses_production_mode_and_postgres():
    data = yaml.safe_load(REPOSITORY_COMPOSE_PATH.read_text(encoding="utf-8"))
    services = data["services"]
    keycloak = services["keycloak"]

    assert keycloak["command"] == ["start", "--import-realm"]
    assert "start-dev" not in " ".join(keycloak["command"])
    assert keycloak["depends_on"]["postgres-auth-init"] == {
        "condition": "service_completed_successfully"
    }
    assert services["postgres-auth-init"]["command"][2].startswith("psql -h postgres -U omniclaw")

    environment = keycloak["environment"]
    assert environment["KC_BOOTSTRAP_ADMIN_USERNAME"] == (
        "${OMNICLAW_KEYCLOAK_ADMIN_USERNAME:-admin}"
    )
    assert environment["KC_BOOTSTRAP_ADMIN_PASSWORD"] == (
        "${OMNICLAW_KEYCLOAK_ADMIN_PASSWORD:-admin}"
    )
    assert environment["KC_DB"] == "postgres"
    assert environment["KC_DB_URL_HOST"] == "postgres"
    assert environment["KC_DB_URL_DATABASE"] == "omniclaw"
    assert environment["KC_DB_SCHEMA"] == "keycloak"
    assert environment["KC_PROXY_HEADERS"] == "xforwarded"
    assert environment["KC_HOSTNAME_STRICT"] == "true"
    assert "KEYCLOAK_ADMIN" not in environment
    assert "KEYCLOAK_ADMIN_PASSWORD" not in environment

    for service_name in ("hosted-facilitator", "hosted-reconciler", "hosted-metrics-exporter"):
        service_environment = services[service_name]["environment"]
        dsn = service_environment["OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN"]
        assert "${OMNICLAW_POSTGRES_PASSWORD:-omniclaw}" in dsn
        assert "omniclaw:omniclaw@postgres" not in dsn


def test_keycloak_realm_allows_local_and_alpha_ops_redirects():
    realm_path = ROOT / "infra" / "auth-bootstrap" / "keycloak" / "omniclaw-realm.json"
    realm = yaml.safe_load(realm_path.read_text(encoding="utf-8"))
    client = next(item for item in realm["clients"] if item["clientId"] == "omniclaw-ops-console")

    assert "https://ops.omniclaw.ai/api/auth/callback" in client["redirectUris"]
    assert "https://ops.omniclaw.ai" in client["webOrigins"]
    assert realm["displayName"] == "OmniClaw Facilitator"


def test_production_compose_keeps_hosted_runtime_standalone():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = data["services"]

    assert {
        "hosted-facilitator",
        "hosted-reconciler",
        "hosted-metrics-exporter",
        "ops-console",
        "prometheus",
    } <= set(services)
    assert "postgres" not in services
    assert "redis" not in services
    assert "seller" not in services
    assert "buyer" not in services
    assert "otel-collector" not in services


def test_production_compose_uses_published_images_and_private_default_binds():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = data["services"]

    assert services["hosted-facilitator"]["image"] == (
        "${OMNICLAW_HOSTED_FACILITATOR_IMAGE:?set OMNICLAW_HOSTED_FACILITATOR_IMAGE}"
    )
    assert services["ops-console"]["image"] == (
        "${OMNICLAW_HOSTED_OPS_CONSOLE_IMAGE:?set OMNICLAW_HOSTED_OPS_CONSOLE_IMAGE}"
    )
    assert "internal/readyz" in services["hosted-facilitator"]["healthcheck"]["test"][1]
    assert (
        services["hosted-facilitator"]["environment"]["OMNICLAW_HOSTED_INITIALIZE_STORAGE"]
        == "true"
    )
    assert (
        "${OMNICLAW_HOSTED_FACILITATOR_BIND:-127.0.0.1:4022}:4022"
        in services["hosted-facilitator"]["ports"]
    )
    assert "${OMNICLAW_PROMETHEUS_BIND:-127.0.0.1:9090}:9090" in services["prometheus"]["ports"]
    for service_name in ("hosted-facilitator", "hosted-reconciler"):
        command = " ".join(services[service_name]["command"])
        assert "python -m hosted_facilitator.hosted.production_env" in command
        assert "$${OMNICLAW_HOSTED_FACILITATOR_MODE}" in command
        assert "production_env || exit 1" in command

    reconciler_health = services["hosted-reconciler"]["healthcheck"]["test"][1]
    assert "reconciler-heartbeat.json" in reconciler_health
    assert "data.get('status')=='ok'" in reconciler_health
    assert "age < 180" in reconciler_health
    assert services["hosted-metrics-exporter"]["depends_on"]["hosted-reconciler"] == {
        "condition": "service_healthy"
    }
    metrics_health = services["hosted-metrics-exporter"]["healthcheck"]["test"][1]
    assert "9108/readyz" in metrics_health
    assert "timeout=10" in metrics_health


def test_production_env_example_has_required_deployment_gates():
    env_text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    for required in (
        "OMNICLAW_HOSTED_FACILITATOR_MODE=production",
        "OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS=1",
        "sslmode=require",
        "OMNICLAW_HOSTED_API_KEY_HASH_PEPPER=replace-with-32-plus-character-random-pepper",
        "OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL=rediss://",
        "OMNICLAW_HOSTED_OTEL_REQUIRED=true",
        "OMNICLAW_HOSTED_JSON_LOGS=true",
        "OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID=",
        "OTEL_EXPORTER_OTLP_ENDPOINT=https://",
        "OMNICLAW_HOSTED_INTERNAL_READY_ALLOW_PRIVATE_NETWORKS=true",
    ):
        assert required in env_text

    assert "OMNICLAW_HOSTED_DEFAULT_API_KEY" not in env_text
    assert "OMNICLAW_HOSTED_OPERATIONS_TOKEN" not in env_text


def test_production_compose_splits_secret_bearing_env_files():
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    services = data["services"]

    assert services["hosted-facilitator"]["env_file"] == [
        "${OMNICLAW_HOSTED_API_ENV_FILE:?set OMNICLAW_HOSTED_API_ENV_FILE}"
    ]
    assert services["hosted-reconciler"]["env_file"] == [
        "${OMNICLAW_HOSTED_API_ENV_FILE:?set OMNICLAW_HOSTED_API_ENV_FILE}"
    ]
    assert services["hosted-metrics-exporter"]["env_file"] == [
        "${OMNICLAW_HOSTED_METRICS_ENV_FILE:?set OMNICLAW_HOSTED_METRICS_ENV_FILE}"
    ]
    assert services["ops-console"]["env_file"] == [
        "${OMNICLAW_OPS_ENV_FILE:?set OMNICLAW_OPS_ENV_FILE}"
    ]

    forbidden = (
        "PRIVATE_KEY",
        "SIGNER_PRIVATE_KEY",
        "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY",
        "OMNICLAW_X402_FACILITATOR_PRIVATE_KEY",
    )
    for service_name in ("hosted-metrics-exporter", "ops-console"):
        environment = services[service_name].get("environment") or {}
        if isinstance(environment, dict):
            flattened = "\n".join(f"{key}={value}" for key, value in environment.items())
        else:
            flattened = "\n".join(str(item) for item in environment)
        for secret_name in forbidden:
            assert secret_name not in flattened


def test_non_signing_env_examples_do_not_receive_signer_secrets():
    metrics_env = METRICS_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    ops_env = OPS_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    compose_env = COMPOSE_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "OMNICLAW_HOSTED_API_ENV_FILE=production.env.example" in compose_env
    assert "OMNICLAW_HOSTED_METRICS_ENV_FILE=production.metrics.env.example" in compose_env
    assert "OMNICLAW_OPS_ENV_FILE=production.ops.env.example" in compose_env

    for env_text in (metrics_env, ops_env):
        assert "PRIVATE_KEY" not in env_text
        assert "SIGNER_PRIVATE_KEY" not in env_text
        assert "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY" not in env_text
        assert "OMNICLAW_X402_FACILITATOR_PRIVATE_KEY" not in env_text
        assert "OMNICLAW_HOSTED_OPERATIONS_TOKEN" not in env_text

    assert "OMNICLAW_OPS_SESSION_SECRET" not in metrics_env
    assert "OMNICLAW_OPS_SESSION_SECRET=" in ops_env
    assert "OMNICLAW_OPS_OIDC_CLIENT_ID=" in ops_env
    assert "OMNICLAW_OPS_OIDC_REDIRECT_URI=" in ops_env
    assert "OMNICLAW_HOSTED_OIDC_CLIENT_ID" not in ops_env
    assert "OMNICLAW_OPS_API_BASE_URL=https://" in ops_env


def test_ops_console_runtime_validates_env_before_starting():
    dockerfile = OPS_DOCKERFILE_PATH.read_text(encoding="utf-8")
    ops_env = OPS_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "npm run validate:runtime && npm run start" in dockerfile
    assert "OMNICLAW_OPS_SESSION_SECRET=replace-with-32-byte-random-secret" in ops_env


def test_production_observability_uses_external_otel_and_local_metrics_only():
    env_text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    metrics_env_text = METRICS_ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    prometheus_text = PROMETHEUS_PRODUCTION_PATH.read_text(encoding="utf-8")
    alerts_text = ALERTS_PRODUCTION_PATH.read_text(encoding="utf-8")

    assert "OTEL_EXPORTER_OTLP_ENDPOINT=https://" in env_text
    assert "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL=https://" in env_text
    assert "OMNICLAW_HOSTED_GATEWAY_CANARY_TIMEOUT_SECONDS=5" in metrics_env_text
    assert "OMNICLAW_HOSTED_METRICS_STRICT_READY=true" in metrics_env_text
    assert (
        "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS=0xreplaceWithFundedHostedSignerAddress"
        in metrics_env_text
    )
    assert (
        "OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS=0xreplaceWithFundedGatewayCanaryAddress"
        in metrics_env_text
    )
    assert "otel-collector" not in prometheus_text
    assert "scrape_timeout: 10s" in prometheus_text
    assert "OmniClawHostedOtelCollectorUnavailable" not in alerts_text
    assert "OmniClawHostedProviderErrors" in alerts_text
    assert "omniclaw_hosted_settlement_recent_failures_total" in alerts_text
    assert "omniclaw_hosted_settle_requests_total" not in alerts_text
    assert "OmniClawHostedUnknownSettlementSpike" in alerts_text
    assert "omniclaw_hosted_unknown_settlements_recent_total" in alerts_text
    assert "omniclaw_hosted_settle_unknown_outcomes_total" not in alerts_text
    assert "OmniClawHostedDuplicateSettleSpike" in alerts_text
    assert "omniclaw_hosted_duplicate_settle_claims_recent_total" in alerts_text
    assert "OmniClawHostedExactSignerGasReserveLow" in alerts_text
    assert "omniclaw_hosted_exact_signer_native_wei" in alerts_text
    assert "OmniClawHostedGatewayCanaryReserveLow" in alerts_text
    assert "omniclaw_hosted_gateway_canary_available_atomic" in alerts_text


def test_deployment_docs_are_compose_only_and_mark_developer_scripts_compose_only():
    doc = DEPLOYMENT_DOC_PATH.read_text(encoding="utf-8")
    runbook = OPS_RUNBOOK_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")

    assert "Docker Compose" in doc
    assert "Kubernetes" not in doc
    assert "kubectl" not in doc
    assert "helm" not in doc.lower()
    assert "Do not scale `hosted-facilitator`" in doc
    assert (
        "`scripts/hosted_facilitator_readiness.sh` is for the repository Compose stack only" in doc
    )
    assert (
        "`scripts/hosted_facilitator_tunnel_smoke.sh` is for the repository Compose stack only"
        in doc
    )
    assert "## Developer Production-Layout Smoke" in doc
    assert "developer verification mode, not a production deployment" in doc
    assert ".local-production.override.yml" in doc
    assert "It does not replace production checks against external TLS Postgres" in doc
    assert (
        "For developer Compose only, the `buyerGateway` section of `scripts/hosted_facilitator_readiness.sh`"
        in runbook
    )
    assert "For developer Compose, `scripts/hosted_facilitator_tunnel_smoke.sh`" in runbook
    assert "run an HTTPS smoke through the deployed reverse proxy" in runbook
    assert "run `python -m hosted_facilitator.hosted.production_env` before serving" in doc
    assert "rejects `OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS` when it does not match" in doc
    assert "OmniClawHostedExactSignerGasReserveLow" in runbook
    assert "OmniClawHostedGatewayCanaryReserveLow" in runbook
    assert "readiness scripts verify the local Compose stack" in readme
    assert "production-layout smoke mode" in readme
