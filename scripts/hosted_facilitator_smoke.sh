#!/usr/bin/env bash
set -euo pipefail

: "${X402_SELLER_PAY_TO:?Set X402_SELLER_PAY_TO to the seller payTo address}"

FACILITATOR_URL="${X402_FACILITATOR_URL:-http://127.0.0.1:4022}"
: "${X402_FACILITATOR_API_KEY:?Set X402_FACILITATOR_API_KEY to a seller-scoped facilitator API key}"
API_KEY="${X402_FACILITATOR_API_KEY}"
SELLER_PORT="${X402_SELLER_PORT:-4023}"
PRICE="${X402_PRICE:-\$0.001}"
BUYER_ENV_FILE="${BUYER_ENV_FILE:-.env}"
SELLER_URL="http://127.0.0.1:${SELLER_PORT}/compute?size=20"

seller_pid=""
cleanup() {
  if [[ -n "${seller_pid}" ]]; then
    kill "${seller_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

start_seller() {
  local mode="$1"
  if [[ -n "${seller_pid}" ]]; then
    kill "${seller_pid}" >/dev/null 2>&1 || true
    wait "${seller_pid}" >/dev/null 2>&1 || true
  fi
  local port_pid
  port_pid="$(lsof -tiTCP:"${SELLER_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${port_pid}" ]]; then
    kill ${port_pid} >/dev/null 2>&1 || true
    sleep 0.5
  fi
  X402_FACILITATOR_URL="${FACILITATOR_URL}" \
    X402_FACILITATOR_API_KEY="${API_KEY}" \
    X402_SELLER_PAY_TO="${X402_SELLER_PAY_TO}" \
    X402_PRICE="${PRICE}" \
    X402_ACCEPT_MODE="${mode}" \
    uv run uvicorn examples.hosted_facilitator_arc_seller.app:app \
      --host 127.0.0.1 \
      --port "${SELLER_PORT}" \
      >/tmp/omniclaw-hosted-smoke-seller.log 2>&1 &
  seller_pid="$!"
  for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:${SELLER_PORT}/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  cat /tmp/omniclaw-hosted-smoke-seller.log >&2 || true
  return 1
}

echo "Checking hosted facilitator /supported with the seller API key..."
curl -fsS \
  -H "Authorization: Bearer ${API_KEY}" \
  "${FACILITATOR_URL}/v2/x402/supported" \
  | jq '{kindCount:(.kinds|length), kinds:[.kinds[]|{scheme,network,extra:.extra.name}]}'

echo "Running mixed-advertisement exact payment..."
start_seller all
curl -fsS "http://127.0.0.1:${SELLER_PORT}/rails" \
  | jq '{acceptMode, options:[.advertisedPaymentOptions[]|{provider,network,extra:.extra.name}]}'
uv run python examples/hosted_facilitator_buyer/pay_seller.py \
  --env-file "${BUYER_ENV_FILE}" \
  --mode exact \
  --url "${SELLER_URL}"

echo "Running exact-only payment..."
start_seller exact
curl -fsS "http://127.0.0.1:${SELLER_PORT}/rails" \
  | jq -e '.acceptMode == "exact" and ([.advertisedPaymentOptions[].provider] == ["exact_evm"])' >/dev/null
uv run python examples/hosted_facilitator_buyer/pay_seller.py \
  --env-file "${BUYER_ENV_FILE}" \
  --mode exact \
  --url "${SELLER_URL}"

echo "Running Gateway-only payment..."
start_seller gateway
curl -fsS "http://127.0.0.1:${SELLER_PORT}/rails" \
  | jq -e '.acceptMode == "gateway" and ([.advertisedPaymentOptions[].provider] == ["circle_gateway"])' >/dev/null
uv run python examples/hosted_facilitator_buyer/pay_seller.py \
  --env-file "${BUYER_ENV_FILE}" \
  --mode gateway \
  --url "${SELLER_URL}"

echo "Hosted facilitator smoke test completed."
