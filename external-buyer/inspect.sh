#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BUYER_ENV_FILE:-${SCRIPT_DIR}/buyer.env}"
TARGET_OVERRIDE="${BUYER_TARGET_URL:-}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy external-buyer/buyer.env.example to buyer.env and fill it." >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

TARGET="${TARGET_OVERRIDE:-${BUYER_TARGET_URL:-http://127.0.0.1:4023/compute}}"

cd "${OMNICLAW_CORE_REPO:?OMNICLAW_CORE_REPO is required}"
exec uv run omniclaw-cli inspect-x402 --recipient "${TARGET}" --method GET
