# OmniClaw Facilitator

OmniClaw Facilitator is a hosted x402 settlement service for sellers that want
to accept machine payments without running their own settlement infrastructure.

Sellers connect their resource server to one endpoint:

```text
https://<facilitator-domain>/v2/x402
Authorization: Bearer <seller-api-key>
```

The facilitator resolves the seller from the API key, enforces the seller payment
policy, verifies x402 payloads, settles supported rails, records settlement state,
and gives OmniClaw operators a control plane for audit, pauses, reconciliation,
signer runway, and live operational health.

## What It Supports

- Arc Testnet x402 `exact` settlement with hosted EVM gas payment.
- Circle Gateway batched nanopayments advertised through x402
  `extra.name=GatewayWalletBatched`.
- Seller API-key authentication with server-side seller/profile resolution.
- Durable Postgres settlement, audit, seller, profile, and API-key state.
- Redis-backed rate limiting and settlement coordination.
- OIDC/OpenFGA-protected operator control plane.
- OpenTelemetry, Prometheus metrics, alert rules, and runbooks.
- Docker Compose deployment for the private alpha.

The seller does not send a project ID, route key, seller ID, or payment profile
ID. The API key is the seller credential and maps server-side to exactly one
seller payment policy for the current alpha.

## Service Boundary

This repository contains only the hosted facilitator system:

```text
apps/ops-console/       Operator control plane
docs/                   Architecture, onboarding, deployment, runbooks
infra/                  Compose, auth, OpenFGA, OTEL, Prometheus, alerts
scripts/                Service entrypoints and verification gates
src/hosted_facilitator/ Facilitator API, providers, storage, reconciliation
tests/                  Service, storage, security, deployment, UI tests
external-seller/        External seller verification harness
external-buyer/         External buyer verification harness
```

The external seller and buyer harnesses are not facilitator runtime code. They
exist to prove the integration boundary from the outside:

- Seller app: facilitator URL, seller API key, network, and `payTo`.
- Buyer app: OmniClaw core buyer agent or buyer CLI.
- Facilitator: verify, settle, reconcile, audit, and operate.

## Alpha Status

The current private alpha stack has been tested from all three perspectives:

- Operator: Compose stack, ops console login, seller creation, API key issue and
  revoke, pause/resume, audit trail, metrics, alerts, and readiness checks.
- Seller: external x402 resource server configured only with facilitator URL,
  seller API key, Arc Testnet, and seller `payTo`.
- Buyer: OmniClaw core buyer agent paying the seller through both Arc exact and
  Circle Gateway batched settlement.

Latest verified live-flow evidence:

- Seller advertised both Arc exact and Gateway batched x402 requirements.
- Gateway payment settled as `circle_gateway`.
- Exact-only seller payment settled as `exact_evm`.
- Gateway-only seller payment settled as `circle_gateway`.
- Postgres settlement records were written with seller and profile scope.
- Signer gas and Gateway canary balances were above configured floors.
- Ops console Playwright flow passed after live payments.

Release gates are tracked in
[docs/hosted-facilitator-alpha-readiness.md](docs/hosted-facilitator-alpha-readiness.md).
The next gate is live deployment through the same HTTPS domains sellers will use.

## Deployment

The private alpha deployment path is Docker Compose:

```text
infra/compose/docker-compose.production.yml
infra/compose/production.compose.env.example
infra/compose/production.env.example
infra/compose/production.metrics.env.example
infra/compose/production.ops.env.example
```

Deployment checklist:

```text
docs/live-deployment-checklist.md
```

Required production inputs:

- Published facilitator and ops-console image tags.
- Host with Docker and Docker Compose.
- TLS reverse proxy for facilitator and ops-console domains.
- Private env files derived from `infra/compose/*.example`.
- Postgres and Redis endpoints.
- OIDC issuer/client and OpenFGA store/model.
- Funded hosted EVM signer for Arc Testnet gas.
- Funded buyer Gateway canary balance for Gateway smoke tests.

Production startup validates the hosted env before serving. Unsafe placeholder
secrets, mismatched control-plane databases, insecure Redis in hosted mode, and
missing OIDC/OpenFGA fail closed.

## Seller Handoff

For the first alpha, send a seller only:

```text
Facilitator origin: https://<facilitator-domain>
Full x402 endpoint: https://<facilitator-domain>/v2/x402
Seller API key: <shown once in the ops console>
Network: Arc Testnet
PayTo: <seller EVM address>
Rails: Arc exact, Circle Gateway batched
```

Do not send internal seller IDs, profile IDs, route keys, project IDs, raw logs,
private keys, bearer tokens, or screenshots containing secrets.

Full onboarding process:

```text
docs/hosted-facilitator-alpha-onboarding.md
```

## Operator Console

The ops console is the internal control plane for OmniClaw operators. It is used
to:

- create seller accounts and one-time API keys;
- inspect seller profiles and active/revoked keys;
- pause or resume a provider/network/global target;
- watch settlement status, provider health, signer runway, and Gateway canary;
- inspect audit events and control-plane changes.

The console is not a seller dashboard. Seller self-service comes after the hosted
facilitator alpha path is deployed and stable.

## Local Operation

Use local Compose to run the same facilitator, control plane, Postgres, Redis,
OIDC, OpenFGA, telemetry, and metrics services used by the alpha stack:

```bash
cp hosted.env.example hosted.env
docker compose -f docker-compose.yml up --build
```

Facilitator:

```text
http://127.0.0.1:4022
```

Ops console:

```text
http://127.0.0.1:3001
```

Readiness:

```bash
bash scripts/hosted_facilitator_readiness.sh
```

Local Compose does not create seller API keys through environment variables.
Sign in to the ops console, create a seller, and copy the one-time API key for
seller integration tests.

The readiness scripts verify the local Compose stack. They do not replace
deployed HTTPS checks, external database persistence checks, or the production
env validator.

The production-layout smoke mode is documented in
[docs/deployment-docker-compose.md](docs/deployment-docker-compose.md). It boots
the production container topology for developer verification, but it is not a
production deployment gate by itself.

Paid canary evidence should include buyer credentials explicitly:

```bash
BUYER_ENV_FILE=/path/to/buyer.env \
OMNICLAW_REQUIRE_BUYER_ENV=true \
bash scripts/hosted_facilitator_readiness.sh
```

External seller and buyer harnesses:

```bash
bash external-seller/run.sh
bash external-buyer/inspect.sh
bash external-buyer/pay.sh
```

These harnesses intentionally run outside the facilitator service boundary.

## Tests

Backend:

```bash
docker compose -f docker-compose.yml -f infra/compose/docker-compose.e2e-ports.yml up -d postgres
OMNICLAW_TEST_POSTGRES_DSN=postgresql://omniclaw:omniclaw@127.0.0.1:15432/omniclaw \
OMNICLAW_HOSTED_FACILITATOR_MODE=test \
uv run --extra dev pytest
uv run ruff check src tests external-seller/app.py
uv run ruff format --check src tests external-seller/app.py
```

The live Postgres suite is part of the normal backend gate. If the database is
not reachable, the suite fails instead of skipping. CI provisions Postgres and
sets `OMNICLAW_TEST_POSTGRES_DSN` for every Python test run.

Ops console:

```bash
cd apps/ops-console
npm run typecheck
npm run lint
npm test
npm run test:e2e:ui
```

Run the full real-stack ops-console flow against Compose before accepting
deployment evidence:

```bash
OMNICLAW_OPS_E2E_BASE_URL=http://127.0.0.1:13001 npm run test:e2e -- --project=chromium
```

Compose config:

```bash
docker compose --env-file infra/compose/production.compose.env.example \
  -f infra/compose/docker-compose.production.yml config --quiet
```

## Operational Rule

This service moves money. Treat every change as settlement-critical: preserve
idempotency, verify durable state, keep logs and traces free of secrets, and
pause the affected provider/network before debugging ambiguous settlement
failures.
