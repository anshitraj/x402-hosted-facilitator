#!/usr/bin/env bash
set -euo pipefail

: "${X402_SELLER_PAY_TO:?Set X402_SELLER_PAY_TO to the seller payTo address}"

LOCAL_FACILITATOR_URL="${X402_FACILITATOR_URL:-http://127.0.0.1:4022}"
API_KEY="${X402_FACILITATOR_API_KEY:-}"
SELLER_PORT="${X402_SELLER_PORT:-4023}"
PRICE="${X402_PRICE:-\$0.001}"
EXPECTED_NETWORK="${X402_NETWORK:-eip155:5042002}"
EXPECTED_ASSET="${X402_ASSET:-0x3600000000000000000000000000000000000000}"
BUYER_ENV_FILE="${BUYER_ENV_FILE:-.env}"
COMPOSE_FILES="${COMPOSE_FILES:-${COMPOSE_FILE:-docker-compose.yml}}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-}"
CURL_DOH_URL="${CURL_DOH_URL:-https://cloudflare-dns.com/dns-query}"
FACILITATOR_TUNNEL_LOG="${FACILITATOR_TUNNEL_LOG:-/tmp/omniclaw-facilitator-cloudflared.log}"
SELLER_TUNNEL_LOG="${SELLER_TUNNEL_LOG:-/tmp/omniclaw-seller-cloudflared.log}"
SELLER_LOG="${SELLER_LOG:-/tmp/omniclaw-tunnel-seller.log}"

seller_pid=""
facilitator_tunnel_pid=""
seller_tunnel_pid=""

cleanup() {
  if [[ -n "${seller_tunnel_pid}" ]]; then
    kill "${seller_tunnel_pid}" >/dev/null 2>&1 || true
    wait "${seller_tunnel_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${facilitator_tunnel_pid}" ]]; then
    kill "${facilitator_tunnel_pid}" >/dev/null 2>&1 || true
    wait "${facilitator_tunnel_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${seller_pid}" ]]; then
    kill "${seller_pid}" >/dev/null 2>&1 || true
    wait "${seller_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

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

if [[ -z "${API_KEY}" ]]; then
  echo "X402_FACILITATOR_API_KEY is required for public tunnel smoke tests" >&2
  exit 1
fi

if [[ "${API_KEY}" == omck_local_* || "${API_KEY}" == omcr_local_* ]]; then
  echo "Refusing to expose a local demo API key through a public tunnel." >&2
  echo "Create a short-lived seller-scoped API key from the control plane." >&2
  exit 1
fi

for required_command in cloudflared curl jq rg uv; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "${required_command} is required for HTTPS tunnel smoke tests" >&2
    exit 1
  fi
done

if [[ -n "${CURL_DOH_URL}" ]] && ! curl --help all | rg -q -- '--doh-url'; then
  echo "This curl build does not support --doh-url; set CURL_DOH_URL= or install curl with DoH support." >&2
  exit 1
fi

extract_tunnel_url() {
  local log_file="$1"
  rg -o 'https://[-a-zA-Z0-9.]+\.trycloudflare\.com' "${log_file}" | head -n 1 || true
}

wait_for_tunnel_url() {
  local log_file="$1"
  local tunnel_pid="$2"
  local public_url=""
  for _ in $(seq 1 60); do
    if ! kill -0 "${tunnel_pid}" >/dev/null 2>&1; then
      echo "cloudflared exited before creating a tunnel URL" >&2
      cat "${log_file}" >&2 || true
      return 1
    fi
    public_url="$(extract_tunnel_url "${log_file}")"
    if [[ -n "${public_url}" ]]; then
      echo "${public_url}"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_http() {
  local url="$1"
  for _ in $(seq 1 90); do
    if curl_retry "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl_retry "${url}" >/dev/null
}

curl_retry() {
  local args=(
    -fsS
    --connect-timeout 10 \
    --max-time 30 \
    --retry 12 \
    --retry-all-errors \
    --retry-delay 5
  )
  if [[ -n "${CURL_DOH_URL}" ]]; then
    args+=(--doh-url "${CURL_DOH_URL}")
  fi
  curl "${args[@]}" "$@"
}

psql_scalar() {
  compose exec -T postgres \
    psql -U omniclaw -d omniclaw -At -c "$1"
}

settlement_max_id() {
  psql_scalar "select coalesce(max(id), 0) from settlement_records;"
}

attempt_max_id() {
  psql_scalar "select coalesce(max(id), 0) from settlement_attempts;"
}

require_digits() {
  local value="$1"
  local label="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${label} must be digits only: ${value}" >&2
    exit 1
  fi
}

require_provider() {
  local provider="$1"
  if [[ "${provider}" != "exact_evm" && "${provider}" != "circle_gateway" ]]; then
    echo "Unsupported settlement provider in smoke assertion: ${provider}" >&2
    exit 1
  fi
}

require_transaction_id() {
  local provider="$1"
  local transaction="$2"
  if [[ "${provider}" == "exact_evm" ]]; then
    if [[ ! "${transaction}" =~ ^0x[0-9a-fA-F]{64}$ ]]; then
      echo "Exact settlement transaction is not a valid EVM hash: ${transaction}" >&2
      exit 1
    fi
    return
  fi
  if [[ ! "${transaction}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "Gateway settlement transaction is not a valid transfer UUID: ${transaction}" >&2
    exit 1
  fi
}

require_evm_address() {
  local value="$1"
  local label="$2"
  if [[ ! "${value}" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    echo "${label} is not a valid EVM address: ${value}" >&2
    exit 1
  fi
}

require_network() {
  local value="$1"
  if [[ ! "${value}" =~ ^eip155:[0-9]+$ ]]; then
    echo "Network is not a valid eip155 namespace: ${value}" >&2
    exit 1
  fi
}

require_extra_name() {
  local value="$1"
  if [[ "${value}" != "USDC" && "${value}" != "GatewayWalletBatched" ]]; then
    echo "Unexpected payment option extra.name: ${value}" >&2
    exit 1
  fi
}

require_hex_hash() {
  local value="$1"
  local label="$2"
  if [[ ! "${value}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "${label} is not a valid SHA-256 hex hash" >&2
    exit 1
  fi
}

api_key_hash() {
  API_KEY_FOR_HASH="${API_KEY}" PYTHONPATH=src uv run python - <<'PY'
from __future__ import annotations

import os

from hosted_facilitator.hosted.tenancy import seller_key_hash

print(seller_key_hash(os.environ["API_KEY_FOR_HASH"]))
PY
}

derive_expected_buyer_address() {
  BUYER_ENV_FILE_PATH="${BUYER_ENV_FILE}" uv run python - <<'PY'
from __future__ import annotations

import os
from pathlib import Path

from eth_account import Account


def load_env(path: str) -> dict[str, str]:
    values = dict(os.environ)
    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(f"buyer env file not found: {path}")
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in values:
            values[key] = value
    return values


env = load_env(os.environ["BUYER_ENV_FILE_PATH"])
private_key = (env.get("BUYER_OMNICLAW_PRIVATE_KEY") or env.get("OMNICLAW_PRIVATE_KEY") or "").strip()
if not private_key:
    raise RuntimeError("BUYER_OMNICLAW_PRIVATE_KEY or OMNICLAW_PRIVATE_KEY is required to derive the expected buyer")
if not private_key.startswith("0x"):
    private_key = f"0x{private_key}"
print(Account.from_key(private_key).address)
PY
}

derive_expected_amount_atomic() {
  PRICE_VALUE="${PRICE}" uv run python - <<'PY'
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

raw = os.environ["PRICE_VALUE"].strip()
if raw.startswith("$"):
    raw = raw[1:]
try:
    amount = Decimal(raw) * Decimal(1_000_000)
except InvalidOperation as exc:
    raise RuntimeError(f"invalid X402_PRICE: {os.environ['PRICE_VALUE']}") from exc
if amount != amount.to_integral_value():
    raise RuntimeError(f"X402_PRICE does not resolve to an integer atomic USDC amount: {os.environ['PRICE_VALUE']}")
print(int(amount))
PY
}

require_same_address() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  if [[ "${actual,,}" != "${expected,,}" ]]; then
    echo "${label} mismatch: actual=${actual} expected=${expected}" >&2
    exit 1
  fi
}

assert_settlement_persisted() {
  local provider="$1"
  local transaction="$2"
  local min_id="$3"
  local min_attempt_id="$4"
  local label="$5"
  local expected_payer="$6"
  local expected_pay_to="$7"
  local expected_asset="$8"
  local expected_amount="$9"
  local expected_extra="${10}"
  local count=""
  require_provider "${provider}"
  require_transaction_id "${provider}" "${transaction}"
  require_digits "${min_id}" "settlement high-water record ID"
  require_digits "${min_attempt_id}" "settlement high-water attempt ID"
  require_evm_address "${expected_payer}" "expected payer"
  require_evm_address "${expected_pay_to}" "expected payTo"
  require_evm_address "${expected_asset}" "expected asset"
  require_digits "${expected_amount}" "expected amount"
  require_network "${EXPECTED_NETWORK}"
  require_extra_name "${expected_extra}"
  count="$(
    psql_scalar "
      select count(*)
      from settlement_records r
      join settlement_attempts a on a.settlement_record_id = r.id
      join hosted_seller_api_keys k on k.seller_account_id = r.seller_account_id
      where r.id > ${min_id}
        and a.id > ${min_attempt_id}
        and k.key_hash = '${EXPECTED_API_KEY_HASH}'
        and k.status = 'active'
        and k.revoked_at is null
        and r.provider = '${provider}'
        and r.network = '${EXPECTED_NETWORK}'
        and r.status = 'settled'
        and r.transaction_hash = '${transaction}'
        and lower(coalesce(r.payer, '')) = lower('${expected_payer}')
        and lower(r.raw_requirements_json->>'payTo') = lower('${expected_pay_to}')
        and lower(r.raw_requirements_json->>'asset') = lower('${expected_asset}')
        and r.raw_requirements_json->>'amount' = '${expected_amount}'
        and r.raw_requirements_json->>'network' = '${EXPECTED_NETWORK}'
        and coalesce(r.raw_requirements_json #>> '{extra,name}', '') = '${expected_extra}'
        and a.status = 'settled'
        and coalesce(a.transaction_hash, '') = '${transaction}';
    "
  )"
  if [[ "${count}" != "1" ]]; then
    echo "${label} settlement was not durably persisted with a settled attempt" >&2
    echo "provider=${provider} transaction=${transaction} min_record_id=${min_id} min_attempt_id=${min_attempt_id} count=${count}" >&2
    echo "expected_payer=${expected_payer} expected_pay_to=${expected_pay_to} expected_asset=${expected_asset} expected_amount=${expected_amount} expected_extra=${expected_extra}" >&2
    compose exec -T postgres \
      psql -U omniclaw -d omniclaw -c "
        select r.id, k.key_prefix, r.provider, r.network, r.status, r.payer,
               r.raw_requirements_json->>'payTo' as pay_to,
               r.raw_requirements_json->>'asset' as asset,
               r.raw_requirements_json->>'amount' as amount,
               r.raw_requirements_json #>> '{extra,name}' as extra_name,
               r.transaction_hash,
               a.id as attempt_id, a.status as attempt_status,
               a.transaction_hash as attempt_transaction, r.created_at
        from settlement_records r
        left join settlement_attempts a on a.settlement_record_id = r.id
        left join hosted_seller_api_keys k on k.seller_account_id = r.seller_account_id
        where r.id > ${min_id} or a.id > ${min_attempt_id}
        order by r.id, a.id;
      " >&2 || true
    return 1
  fi
}

wait_for_facilitator_supported() {
  local base_url="$1"
  for _ in $(seq 1 180); do
    if curl_retry \
      -H "Authorization: Bearer ${API_KEY}" \
      "${base_url}/v2/x402/supported" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  curl_retry \
    -H "Authorization: Bearer ${API_KEY}" \
    "${base_url}/v2/x402/supported" >/dev/null
}

wait_for_facilitator_supported "${LOCAL_FACILITATOR_URL}"
require_network "${EXPECTED_NETWORK}"
require_evm_address "${EXPECTED_ASSET}" "expected asset"
require_evm_address "${X402_SELLER_PAY_TO}" "seller payTo"
EXPECTED_API_KEY_HASH="$(api_key_hash)"
require_hex_hash "${EXPECTED_API_KEY_HASH}" "API key hash"
EXPECTED_BUYER_ADDRESS="${X402_EXPECTED_BUYER_ADDRESS:-$(derive_expected_buyer_address)}"
EXPECTED_AMOUNT_ATOMIC="${X402_EXPECTED_AMOUNT_ATOMIC:-$(derive_expected_amount_atomic)}"
require_evm_address "${EXPECTED_BUYER_ADDRESS}" "expected buyer"
require_digits "${EXPECTED_AMOUNT_ATOMIC}" "expected atomic amount"
rm -f "${FACILITATOR_TUNNEL_LOG}"
cloudflared tunnel --url "${LOCAL_FACILITATOR_URL}" --no-autoupdate \
  >"${FACILITATOR_TUNNEL_LOG}" 2>&1 &
facilitator_tunnel_pid="$!"

facilitator_public_url="$(wait_for_tunnel_url "${FACILITATOR_TUNNEL_LOG}" "${facilitator_tunnel_pid}" || true)"
if [[ -z "${facilitator_public_url}" ]]; then
  echo "Cloudflare facilitator tunnel URL was not created" >&2
  cat "${FACILITATOR_TUNNEL_LOG}" >&2 || true
  exit 1
fi
wait_for_facilitator_supported "${facilitator_public_url}"
echo "Public facilitator URL: ${facilitator_public_url}"
curl_retry \
  -H "Authorization: Bearer ${API_KEY}" \
  "${facilitator_public_url}/v2/x402/supported" \
  | jq '{kindCount:(.kinds|length), kinds:[.kinds[]|{scheme,network,extra:.extra.name}]}'

port_pid="$(lsof -tiTCP:"${SELLER_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${port_pid}" ]]; then
  echo "Seller port ${SELLER_PORT} is already in use by PID(s): ${port_pid}" >&2
  echo "Stop that process or set X402_SELLER_PORT to a free port." >&2
  exit 1
fi

X402_FACILITATOR_URL="${facilitator_public_url}" \
  X402_FACILITATOR_API_KEY="${API_KEY}" \
  X402_SELLER_PAY_TO="${X402_SELLER_PAY_TO}" \
  X402_PRICE="${PRICE}" \
  X402_ACCEPT_MODE=all \
  OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true \
  uv run uvicorn examples.hosted_facilitator_arc_seller.app:app \
    --host 127.0.0.1 \
    --port "${SELLER_PORT}" \
    >"${SELLER_LOG}" 2>&1 &
seller_pid="$!"

for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${SELLER_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${SELLER_PORT}/health" >/dev/null

rm -f "${SELLER_TUNNEL_LOG}"
cloudflared tunnel --url "http://127.0.0.1:${SELLER_PORT}" --no-autoupdate \
  >"${SELLER_TUNNEL_LOG}" 2>&1 &
seller_tunnel_pid="$!"

public_url="$(wait_for_tunnel_url "${SELLER_TUNNEL_LOG}" "${seller_tunnel_pid}" || true)"

if [[ -z "${public_url}" ]]; then
  echo "Cloudflare seller tunnel URL was not created" >&2
  cat "${SELLER_TUNNEL_LOG}" >&2 || true
  exit 1
fi

echo "Public seller URL: ${public_url}"
wait_for_http "${public_url}/health"
rails_json="$(curl_retry "${public_url}/rails")"
jq '{acceptMode, options:[.advertisedPaymentOptions[]|{provider,network,extra:.extra.name}]}' \
  <<<"${rails_json}"
jq -e '
  any(.advertisedPaymentOptions[]?; .provider == "exact_evm")
  and any(.advertisedPaymentOptions[]?; .provider == "circle_gateway")
' <<<"${rails_json}" >/dev/null

settlement_before_id="$(settlement_max_id)"
attempt_before_id="$(attempt_max_id)"
require_digits "${settlement_before_id}" "settlement high-water record ID"
require_digits "${attempt_before_id}" "settlement high-water attempt ID"
echo "Settlement high-water mark before canary: records=${settlement_before_id} attempts=${attempt_before_id}"
echo "Settlement assertions will be scoped to the supplied API key."

echo "Running public HTTPS exact payment against mixed advertised rails..."
exact_result="$(
  OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true \
  uv run python examples/hosted_facilitator_buyer/pay_seller.py \
    --env-file "${BUYER_ENV_FILE}" \
    --mode exact \
    --url "${public_url}/compute?size=20"
)"
echo "${exact_result}"
exact_tx="$(jq -er '.settlement.transaction' <<<"${exact_result}")"
jq -e '.success == true and .paymentRequired == true and (.settlement.transaction | length > 0)' \
  <<<"${exact_result}" >/dev/null
require_transaction_id "exact_evm" "${exact_tx}"
exact_payer="$(jq -er '.settlement.payer' <<<"${exact_result}")"
exact_pay_to="$(jq -er '.selected.payTo' <<<"${exact_result}")"
exact_asset="$(jq -er '.selected.asset' <<<"${exact_result}")"
exact_amount="$(jq -er '.selected.amount' <<<"${exact_result}")"
exact_extra="$(jq -er '.selected.extra.name // "USDC"' <<<"${exact_result}")"
require_evm_address "${exact_payer}" "exact payer"
require_evm_address "${exact_pay_to}" "exact payTo"
require_evm_address "${exact_asset}" "exact asset"
require_digits "${exact_amount}" "exact amount"
require_extra_name "${exact_extra}"
require_same_address "${exact_payer}" "${EXPECTED_BUYER_ADDRESS}" "exact payer"
require_same_address "${exact_pay_to}" "${X402_SELLER_PAY_TO}" "exact payTo"
require_same_address "${exact_asset}" "${EXPECTED_ASSET}" "exact asset"
if [[ "${exact_amount}" != "${EXPECTED_AMOUNT_ATOMIC}" ]]; then
  echo "exact amount mismatch: actual=${exact_amount} expected=${EXPECTED_AMOUNT_ATOMIC}" >&2
  exit 1
fi

echo "Running public HTTPS Gateway payment against mixed advertised rails..."
gateway_result="$(
  OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true \
  uv run python examples/hosted_facilitator_buyer/pay_seller.py \
    --env-file "${BUYER_ENV_FILE}" \
    --mode gateway \
    --url "${public_url}/compute?size=20"
)"
echo "${gateway_result}"
gateway_tx="$(jq -er '.transaction' <<<"${gateway_result}")"
jq -e '.success == true and .paymentRequired == true and .isNanopayment == true and (.transaction | length > 0)' \
  <<<"${gateway_result}" >/dev/null
require_transaction_id "circle_gateway" "${gateway_tx}"
gateway_payer="$(jq -er '.payer' <<<"${gateway_result}")"
gateway_pay_to="$(jq -er '.seller' <<<"${gateway_result}")"
gateway_amount="$(jq -er '.amountAtomic' <<<"${gateway_result}")"
gateway_network="$(jq -er '.network' <<<"${gateway_result}")"
require_evm_address "${gateway_payer}" "Gateway payer"
require_evm_address "${gateway_pay_to}" "Gateway payTo"
require_digits "${gateway_amount}" "Gateway amount"
require_network "${gateway_network}"
require_same_address "${gateway_payer}" "${EXPECTED_BUYER_ADDRESS}" "Gateway payer"
require_same_address "${gateway_pay_to}" "${X402_SELLER_PAY_TO}" "Gateway payTo"
if [[ "${gateway_amount}" != "${EXPECTED_AMOUNT_ATOMIC}" ]]; then
  echo "Gateway amount mismatch: actual=${gateway_amount} expected=${EXPECTED_AMOUNT_ATOMIC}" >&2
  exit 1
fi
if [[ "${gateway_network}" != "${EXPECTED_NETWORK}" ]]; then
  echo "Gateway result network ${gateway_network} did not match expected ${EXPECTED_NETWORK}" >&2
  exit 1
fi

echo "Running public HTTPS exact-only seller payment..."
if [[ -n "${seller_pid}" ]]; then
  kill "${seller_pid}" >/dev/null 2>&1 || true
  wait "${seller_pid}" >/dev/null 2>&1 || true
fi
X402_FACILITATOR_URL="${facilitator_public_url}" \
  X402_FACILITATOR_API_KEY="${API_KEY}" \
  X402_SELLER_PAY_TO="${X402_SELLER_PAY_TO}" \
  X402_PRICE="${PRICE}" \
  X402_ACCEPT_MODE=exact \
  OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true \
  uv run uvicorn examples.hosted_facilitator_arc_seller.app:app \
    --host 127.0.0.1 \
    --port "${SELLER_PORT}" \
    >"${SELLER_LOG}" 2>&1 &
seller_pid="$!"
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${SELLER_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
wait_for_http "${public_url}/health"
exact_only_rails_json="$(curl_retry "${public_url}/rails")"
jq -e '.acceptMode == "exact" and ([.advertisedPaymentOptions[].provider] == ["exact_evm"])' \
  <<<"${exact_only_rails_json}" >/dev/null
exact_only_result="$(
  OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true \
  uv run python examples/hosted_facilitator_buyer/pay_seller.py \
    --env-file "${BUYER_ENV_FILE}" \
    --mode exact \
    --url "${public_url}/compute?size=20"
)"
echo "${exact_only_result}"
exact_only_tx="$(jq -er '.settlement.transaction' <<<"${exact_only_result}")"
jq -e '.success == true and .paymentRequired == true and (.settlement.transaction | length > 0)' \
  <<<"${exact_only_result}" >/dev/null
require_transaction_id "exact_evm" "${exact_only_tx}"
exact_only_payer="$(jq -er '.settlement.payer' <<<"${exact_only_result}")"
exact_only_pay_to="$(jq -er '.selected.payTo' <<<"${exact_only_result}")"
exact_only_asset="$(jq -er '.selected.asset' <<<"${exact_only_result}")"
exact_only_amount="$(jq -er '.selected.amount' <<<"${exact_only_result}")"
exact_only_extra="$(jq -er '.selected.extra.name // "USDC"' <<<"${exact_only_result}")"
require_same_address "${exact_only_payer}" "${EXPECTED_BUYER_ADDRESS}" "exact-only payer"
require_same_address "${exact_only_pay_to}" "${X402_SELLER_PAY_TO}" "exact-only payTo"
require_same_address "${exact_only_asset}" "${EXPECTED_ASSET}" "exact-only asset"
if [[ "${exact_only_amount}" != "${EXPECTED_AMOUNT_ATOMIC}" ]]; then
  echo "exact-only amount mismatch: actual=${exact_only_amount} expected=${EXPECTED_AMOUNT_ATOMIC}" >&2
  exit 1
fi

echo "Verifying persisted settlement records..."
assert_settlement_persisted "exact_evm" "${exact_tx}" "${settlement_before_id}" "${attempt_before_id}" "exact" \
  "${exact_payer}" "${exact_pay_to}" "${exact_asset}" "${exact_amount}" "${exact_extra}"
assert_settlement_persisted "circle_gateway" "${gateway_tx}" "${settlement_before_id}" "${attempt_before_id}" "Gateway" \
  "${gateway_payer}" "${gateway_pay_to}" "${EXPECTED_ASSET}" "${gateway_amount}" "GatewayWalletBatched"
assert_settlement_persisted "exact_evm" "${exact_only_tx}" "${settlement_before_id}" "${attempt_before_id}" "exact-only" \
  "${exact_only_payer}" "${exact_only_pay_to}" "${exact_only_asset}" "${exact_only_amount}" "${exact_only_extra}"

echo "Cloudflare tunnel smoke test completed."
