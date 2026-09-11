#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILES="${COMPOSE_FILES:-${COMPOSE_FILE:-docker-compose.yml}}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
FACILITATOR_URL="${X402_FACILITATOR_URL:-http://127.0.0.1:4022}"
OPS_URL="${OMNICLAW_OPS_PUBLIC_ORIGIN:-http://127.0.0.1:3001}"
METRICS_URL="${OMNICLAW_HOSTED_METRICS_URL:-http://127.0.0.1:9108}"
PROMETHEUS_URL="${OMNICLAW_PROMETHEUS_URL:-http://127.0.0.1:9090}"
: "${X402_FACILITATOR_API_KEY:?Set X402_FACILITATOR_API_KEY to a seller-scoped facilitator API key}"
API_KEY="${X402_FACILITATOR_API_KEY}"
BUYER_ENV_FILE="${BUYER_ENV_FILE:-.env}"
HOSTED_ENV_FILE="${HOSTED_ENV_FILE:-hosted.env}"
GATEWAY_MIN_ATOMIC="${X402_GATEWAY_CANARY_MIN_ATOMIC:-1000}"
REQUIRE_BUYER_ENV="${OMNICLAW_REQUIRE_BUYER_ENV:-false}"
export BUYER_ENV_FILE
export HOSTED_ENV_FILE
export X402_GATEWAY_CANARY_MIN_ATOMIC="${GATEWAY_MIN_ATOMIC}"
REQUIRED_SERVICES=(
  hosted-facilitator
  hosted-reconciler
  hosted-metrics-exporter
  ops-console
  keycloak
  openfga
  otel-collector
  prometheus
  postgres
  redis
)

IFS=':' read -r -a COMPOSE_FILE_LIST <<<"${COMPOSE_FILES}"
COMPOSE_ARGS=()
if [[ -n "${COMPOSE_PROJECT_NAME}" ]]; then
  COMPOSE_ARGS+=("-p" "${COMPOSE_PROJECT_NAME}")
fi
for compose_file in "${COMPOSE_FILE_LIST[@]}"; do
  COMPOSE_ARGS+=("-f" "${compose_file}")
done

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

section() {
  printf '\n== %s ==\n' "$1"
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Missing required file: $1" >&2
    return 1
  fi
}

wait_for_service() {
  local service="$1"
  local service_json=""
  local state=""
  local health=""
  for _ in $(seq 1 60); do
    service_json="$(compose ps --format json "${service}")"
    state="$(jq -r '.State // ""' <<<"${service_json}")"
    health="$(jq -r '.Health // ""' <<<"${service_json}")"
    if [[ "${state}" == "running" && ( -z "${health}" || "${health}" == "healthy" ) ]]; then
      return 0
    fi
    sleep 1
  done
  echo "Service ${service} is not ready: state=${state} health=${health}" >&2
  return 1
}

section "Required files"
for compose_file in "${COMPOSE_FILE_LIST[@]}"; do
  require_file "${compose_file}"
done
require_file "${HOSTED_ENV_FILE}"
if [[ "${REQUIRE_BUYER_ENV}" == "true" ]]; then
  require_file "${BUYER_ENV_FILE}"
fi
echo "compose=${COMPOSE_FILES}"
if [[ -n "${COMPOSE_PROJECT_NAME}" ]]; then
  echo "compose_project=${COMPOSE_PROJECT_NAME}"
fi
echo "hosted_env=${HOSTED_ENV_FILE}"
if [[ -f "${BUYER_ENV_FILE}" ]]; then
  echo "buyer_env=${BUYER_ENV_FILE}"
else
  echo "buyer_env=${BUYER_ENV_FILE} (missing; balance canary skipped)"
fi

section "Compose services"
compose ps "${REQUIRED_SERVICES[@]}"
for service in "${REQUIRED_SERVICES[@]}"; do
  wait_for_service "${service}"
done

section "HTTP health"
curl -fsS "${OPS_URL}/healthz" | jq .
curl -fsS "${METRICS_URL}/healthz"
compose exec -T hosted-facilitator python - <<'PY' | jq -e '.status == "ok"'
import json
import os
import urllib.request

url = os.environ.get("OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL", "")
with urllib.request.urlopen(url, timeout=3) as response:
    print(json.dumps({"status": "ok" if response.status == 200 else "error"}))
PY
curl -fsS "${PROMETHEUS_URL}/-/ready"
compose exec -T hosted-facilitator python - <<'PY' | jq -e '.status == "ok"'
import urllib.request

print(
    urllib.request.urlopen(
        "http://127.0.0.1:4022/internal/readyz",
        timeout=3,
    ).read().decode("utf-8")
)
PY

section "Prometheus and metrics exporter"
metrics_body="$(curl -fsS "${METRICS_URL}/metrics")"
rg -q '^omniclaw_hosted_metrics_exporter_scrape_success 1$' <<<"${metrics_body}"
rg -q '^omniclaw_hosted_facilitator_up 1$' <<<"${metrics_body}"
rg -q '^omniclaw_hosted_exact_signer_above_min\{.*\} 1$' <<<"${metrics_body}"
rg -q '^omniclaw_hosted_gateway_canary_above_min\{.*\} 1$' <<<"${metrics_body}"
if rg -q '0x4cfdD69a2A89B91f3c12588085f098C268Ea8631|0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7' <<<"${metrics_body}"; then
  echo "Metrics exporter leaked wallet addresses into Prometheus output" >&2
  exit 1
fi
curl -fsS "${PROMETHEUS_URL}/api/v1/targets" \
  | jq -e '[.data.activeTargets[] | select(.health != "up")] | length == 0'
curl -fsS "${PROMETHEUS_URL}/api/v1/targets" \
  | jq -e '[.data.activeTargets[] | select(.labels.job == "hosted-metrics-exporter" and .health == "up")] | length == 1'
curl -fsS "${PROMETHEUS_URL}/api/v1/targets" \
  | jq -e '[.data.activeTargets[] | select(.labels.job == "otel-collector-exported-metrics" and .health == "up")] | length == 1'
curl -fsS "${PROMETHEUS_URL}/api/v1/query?query=omniclaw_hosted_metrics_exporter_scrape_success" \
  | jq -e '.data.result | length > 0 and all(.[]; .value[1] == "1")'
curl -fsS "${PROMETHEUS_URL}/api/v1/rules" \
  | jq -e '[.data.groups[].rules[] | select(.name | startswith("OmniClawHosted"))] | length >= 11'

section "Facilitator supported rails"
supported_json="$(curl -fsS \
  -H "Authorization: Bearer ${API_KEY}" \
  "${FACILITATOR_URL}/v2/x402/supported")"
jq '{kindCount:(.kinds|length), kinds:[.kinds[]|{scheme,network,asset,extra:.extra.name}]}' <<<"${supported_json}"
jq -e '.kinds[] | select(.scheme == "exact" and .network == "eip155:5042002" and ((.extra.name? // "") != "GatewayWalletBatched"))' <<<"${supported_json}" >/dev/null
jq -e '.kinds[] | select(.scheme == "exact" and .network == "eip155:5042002" and (.extra.name? == "GatewayWalletBatched"))' <<<"${supported_json}" >/dev/null

section "Durable seller API key"
compose exec -T postgres psql -U omniclaw -d omniclaw -tAc \
  "select to_regclass('public.hosted_seller_accounts') is not null and to_regclass('public.hosted_seller_payment_profiles') is not null and to_regclass('public.hosted_seller_api_keys') is not null;" \
  | rg -q '^t$' || {
    echo "Hosted seller-account schema is missing. Rebuild the Compose stack from the current worktree and rerun migrations before using this stack as alpha evidence." >&2
    exit 1
  }
compose exec -T postgres psql -U omniclaw -d omniclaw -c \
  "select s.id as seller_ref, p.id as payment_profile, p.enabled_providers_json, p.enabled_networks_json, k.key_prefix, k.status from hosted_seller_accounts s join hosted_seller_payment_profiles p on p.seller_account_id = s.id join hosted_seller_api_keys k on k.seller_account_id = p.seller_account_id and k.payment_profile_id = p.id where s.id = 'default' order by k.created_at desc limit 5;"
compose exec -T postgres psql -U omniclaw -d omniclaw -tAc \
  "select ((p.enabled_providers_json::jsonb ? 'exact_evm') and (p.enabled_providers_json::jsonb ? 'circle_gateway') and (p.enabled_networks_json::jsonb ? 'eip155:5042002') and exists (select 1 from hosted_seller_api_keys k where k.seller_account_id = p.seller_account_id and k.payment_profile_id = p.id and k.status = 'active')) from hosted_seller_payment_profiles p where p.seller_account_id = 'default' and p.id = 'default';" \
  | rg -q '^t$'

section "Settlement tail"
compose exec -T postgres psql -U omniclaw -d omniclaw -c \
  "select id, provider, status, transaction_hash, raw_requirements_json->'extra'->>'name' as extra_name, created_at from settlement_records order by id desc limit 8;"

if [[ -f "${BUYER_ENV_FILE}" ]]; then
  section "Signer and Gateway balances"
  PYTHONPATH=src uv run python - <<'PY'
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from eth_account import Account
from examples.hosted_facilitator_buyer.pay_seller import _buyer_private_key, _load_env_file
from hosted_facilitator.nanopayments.client import NanopaymentClient
from hosted_facilitator.nanopayments.signing import EIP3009Signer
from web3 import Web3


def load_env_if_exists(path: str) -> None:
    if Path(path).exists():
        _load_env_file(path)


async def main() -> None:
    load_env_if_exists(os.environ.get("BUYER_ENV_FILE", ".env"))
    load_env_if_exists(os.environ.get("HOSTED_ENV_FILE", "hosted.env"))
    network = "eip155:5042002"
    rpc = os.environ.get("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "")
    signer_key = os.environ.get("OMNICLAW_X402_FACILITATOR_PRIVATE_KEY", "")
    buyer_key = _buyer_private_key()
    result: dict[str, object] = {}
    errors: list[str] = []
    if not rpc:
        errors.append("missing OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002")
    if not signer_key:
        errors.append("missing OMNICLAW_X402_FACILITATOR_PRIVATE_KEY")
    if rpc and signer_key:
        w3 = Web3(Web3.HTTPProvider(rpc))
        signer_address = Account.from_key(signer_key).address
        expected_signer_address = os.environ.get(
            "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS",
            "0x4cfdD69a2A89B91f3c12588085f098C268Ea8631",
        )
        if Web3.to_checksum_address(signer_address) != Web3.to_checksum_address(expected_signer_address):
            errors.append(
                "metrics signer address does not match facilitator signer private key"
            )
        balance_wei = w3.eth.get_balance(Web3.to_checksum_address(signer_address))
        min_balance = int(os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "0") or "0")
        above_floor = balance_wei >= min_balance
        result["exactSigner"] = {
            "address": signer_address,
            "network": network,
            "nativeWei": balance_wei,
            "minNativeWei": min_balance,
            "aboveFloor": above_floor,
        }
        if not above_floor:
            errors.append(
                f"exact signer native balance {balance_wei} is below floor {min_balance}"
            )
    buyer_address = EIP3009Signer(buyer_key).address
    gateway_balance = await NanopaymentClient(environment="testnet").check_balance(
        buyer_address,
        network,
    )
    gateway_min_atomic = int(os.environ.get("X402_GATEWAY_CANARY_MIN_ATOMIC", "1000") or "0")
    gateway_above_floor = gateway_balance.available >= gateway_min_atomic
    result["buyerGateway"] = {
        "address": buyer_address,
        "network": network,
        "availableAtomic": gateway_balance.available,
        "formattedAvailable": gateway_balance.formatted_available,
        "minAtomic": gateway_min_atomic,
        "aboveFloor": gateway_above_floor,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if not gateway_above_floor:
        errors.append(
            f"buyer Gateway balance {gateway_balance.available} is below canary floor {gateway_min_atomic}"
        )
    if errors:
        raise SystemExit("; ".join(errors))


asyncio.run(main())
PY
else
  section "Signer and Gateway balances"
  echo "Skipped buyer Gateway balance check because BUYER_ENV_FILE does not exist."
  echo "Set BUYER_ENV_FILE=/path/to/.env or OMNICLAW_REQUIRE_BUYER_ENV=true for paid canary readiness."
fi

section "Recent backend errors"
compose logs --tail=160 hosted-facilitator hosted-reconciler \
  | rg -i "error|exception|traceback|timeout|manual_review|unknown" || true

section "Readiness check complete"
