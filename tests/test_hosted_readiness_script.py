from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
READINESS_SCRIPT = REPO_ROOT / "scripts" / "hosted_facilitator_readiness.sh"
TUNNEL_SMOKE_SCRIPT = REPO_ROOT / "scripts" / "hosted_facilitator_tunnel_smoke.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _script_text() -> str:
    return READINESS_SCRIPT.read_text(encoding="utf-8")


def test_hosted_readiness_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(READINESS_SCRIPT)], check=True)
    subprocess.run(["bash", "-n", str(TUNNEL_SMOKE_SCRIPT)], check=True)


def test_ci_runs_hosted_readiness_script_syntax_check():
    assert CI_WORKFLOW.exists()
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "bash -n scripts/hosted_facilitator_readiness.sh" in workflow
    assert "bash -n scripts/hosted_facilitator_tunnel_smoke.sh" in workflow


def test_tunnel_smoke_validates_sql_assertion_inputs():
    script = TUNNEL_SMOKE_SCRIPT.read_text(encoding="utf-8")

    for required_guard in (
        "require_digits",
        "require_provider",
        "require_transaction_id",
        "require_evm_address",
        "require_network",
        "require_extra_name",
        "derive_expected_buyer_address",
        "derive_expected_amount_atomic",
        "require_same_address",
        "X402_ACCEPT_MODE=exact",
        "^0x[0-9a-fA-F]{64}$",
        "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ):
        assert required_guard in script


def test_tunnel_smoke_pins_persistence_contract():
    script = TUNNEL_SMOKE_SCRIPT.read_text(encoding="utf-8")

    for required_contract in (
        'COMPOSE_FILES="${COMPOSE_FILES:-${COMPOSE_FILE:-docker-compose.yml}}"',
        'COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"',
        'COMPOSE_ARGS+=("-p" "${COMPOSE_PROJECT_NAME}")',
        'COMPOSE_ARGS+=("-f" "${compose_file}")',
        'docker compose "${COMPOSE_ARGS[@]}"',
        "settlement_max_id",
        "attempt_max_id",
        "hosted_seller_api_keys",
        "seller_key_hash",
        "EXPECTED_API_KEY_HASH",
        "k.key_hash = '${EXPECTED_API_KEY_HASH}'",
        "r.id > ${min_id}",
        "a.id > ${min_attempt_id}",
        "r.provider = '${provider}'",
        "r.network = '${EXPECTED_NETWORK}'",
        "r.status = 'settled'",
        "r.transaction_hash = '${transaction}'",
        "coalesce(a.transaction_hash, '') = '${transaction}'",
        "lower(coalesce(r.payer, '')) = lower('${expected_payer}')",
        "lower(r.raw_requirements_json->>'payTo') = lower('${expected_pay_to}')",
        "lower(r.raw_requirements_json->>'asset') = lower('${expected_asset}')",
        "r.raw_requirements_json->>'amount' = '${expected_amount}'",
        "r.raw_requirements_json->>'network' = '${EXPECTED_NETWORK}'",
        "coalesce(r.raw_requirements_json #>> '{extra,name}', '') = '${expected_extra}'",
        'require_same_address "${exact_payer}" "${EXPECTED_BUYER_ADDRESS}"',
        'require_same_address "${exact_pay_to}" "${X402_SELLER_PAY_TO}"',
        'require_same_address "${exact_asset}" "${EXPECTED_ASSET}"',
        "exact amount mismatch",
        'require_same_address "${gateway_payer}" "${EXPECTED_BUYER_ADDRESS}"',
        'require_same_address "${gateway_pay_to}" "${X402_SELLER_PAY_TO}"',
        "Gateway amount mismatch",
        "exact-only amount mismatch",
        'assert_settlement_persisted "exact_evm" "${exact_only_tx}"',
    ):
        assert required_contract in script

    for forbidden_contract in (
        "EXPECTED_SELLER_ID",
        "OMNICLAW_EXPECTED_SELLER_ID",
        "X402_SELLER_ID",
        "resolve_seller_account_id_for_api_key",
        "r.seller_account_id =",
    ):
        assert forbidden_contract not in script


def test_hosted_readiness_script_pins_critical_release_gates():
    script = _script_text()

    for service in (
        "hosted-facilitator",
        "hosted-reconciler",
        "hosted-metrics-exporter",
        "otel-collector",
        "prometheus",
        "postgres",
        "redis",
    ):
        assert service in script

    for metric_gate in (
        "omniclaw_hosted_metrics_exporter_scrape_success 1",
        "omniclaw_hosted_facilitator_up 1",
        "omniclaw_hosted_exact_signer_above_min",
        "omniclaw_hosted_gateway_canary_above_min",
    ):
        assert metric_gate in script

    for prometheus_gate in (
        'select(.labels.job == "hosted-metrics-exporter" and .health == "up")',
        'select(.labels.job == "otel-collector-exported-metrics" and .health == "up")',
        "omniclaw_hosted_metrics_exporter_scrape_success",
        'startswith("OmniClawHosted"))] | length >= 11',
    ):
        assert prometheus_gate in script

    for money_flow_gate in (
        'extra.name? == "GatewayWalletBatched"',
        '(.extra.name? // "") != "GatewayWalletBatched"',
        "enabled_providers_json::jsonb ? 'exact_evm'",
        "enabled_providers_json::jsonb ? 'circle_gateway'",
        "to_regclass('public.hosted_seller_accounts')",
        "Hosted seller-account schema is missing",
        "metrics signer address does not match facilitator signer private key",
        "buyer Gateway balance",
    ):
        assert money_flow_gate in script

    assert "Metrics exporter leaked wallet addresses" in script


def test_seller_facing_docs_do_not_expose_removed_credentials():
    seller_facing_files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "hosted-facilitator-alpha-onboarding.md",
        REPO_ROOT / "docs" / "hosted-facilitator-alpha-readiness.md",
        REPO_ROOT / "docs" / "runbooks" / "hosted-facilitator-ops.md",
        REPO_ROOT / "examples" / "hosted_facilitator_arc_seller" / "README.md",
        REPO_ROOT / "examples" / "hosted_facilitator_buyer" / "README.md",
    ]
    forbidden = (
        "Project ID",
        "X402_SELLER_ID",
        "OMNICLAW_EXPECTED_SELLER_ID",
        "BUYER_CIRCLE_API_KEY",
        "CIRCLE_API_KEY",
        "Circle API key",
    )

    for path in seller_facing_files:
        text = path.read_text(encoding="utf-8")
        for value in forbidden:
            assert value not in text, f"{path.relative_to(REPO_ROOT)} still contains {value!r}"


def test_hosted_readiness_script_can_skip_buyer_env_for_plain_health_checks():
    script = _script_text()

    assert 'REQUIRE_BUYER_ENV="${OMNICLAW_REQUIRE_BUYER_ENV:-false}"' in script
    assert 'if [[ "${REQUIRE_BUYER_ENV}" == "true" ]]; then' in script
    assert "balance canary skipped" in script
    assert "Skipped buyer Gateway balance check" in script
