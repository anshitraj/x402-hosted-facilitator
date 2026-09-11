#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SELLER_ENV_FILE:-${ROOT_DIR}/external-seller/seller.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing seller env file: ${ENV_FILE}" >&2
  echo "Create it from external-seller/seller.env.example" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

SELLER_HOST="${SELLER_HOST:-127.0.0.1}"
SELLER_PORT="${SELLER_PORT:-4023}"

cd "${ROOT_DIR}"
exec uv run uvicorn app:app \
  --app-dir external-seller \
  --host "${SELLER_HOST}" \
  --port "${SELLER_PORT}"
