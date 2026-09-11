#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BUYER_ENV_FILE:-${SCRIPT_DIR}/buyer.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy external-buyer/buyer.env.example to buyer.env and fill it." >&2
  exit 1
fi

set -a
source "${ENV_FILE}"
set +a

CORE_REPO="${OMNICLAW_CORE_REPO:?OMNICLAW_CORE_REPO is required}"
PORT="${OMNICLAW_AGENT_PORT:-18080}"

cd "${CORE_REPO}"
exec uv run uvicorn omniclaw.agent.server:app --host 127.0.0.1 --port "${PORT}"

