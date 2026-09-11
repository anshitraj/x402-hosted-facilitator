# Docker Compose Deployment

This deployment path is for the standalone hosted facilitator service. It does not include seller or buyer code.

## Components

- `hosted-facilitator`: x402 facilitator API.
- `hosted-reconciler`: settlement reconciliation worker.
- `hosted-metrics-exporter`: Prometheus exporter for settlement, signer, Gateway, and reconciler signals.
- `ops-console`: internal operator control plane frontend.
- `prometheus`: local scrape and alert-rule validation target for hosted metrics.

Postgres, Redis, OIDC, OpenFGA, and the production OTEL backend are external production dependencies. They are not bundled in the production Compose file.

## Files

```text
infra/compose/docker-compose.production.yml
infra/compose/production.compose.env.example
infra/compose/production.env.example
infra/compose/production.metrics.env.example
infra/compose/production.ops.env.example
infra/prometheus/prometheus.production.yml
infra/alerts/hosted-facilitator-alerts.production.yaml
```

## Prepare

Build and publish the two images:

```bash
docker build -t ghcr.io/omnuron-labs/omniclaw-hosted-facilitator:<tag> .
docker build -t ghcr.io/omnuron-labs/omniclaw-hosted-ops-console:<tag> apps/ops-console
docker push ghcr.io/omnuron-labs/omniclaw-hosted-facilitator:<tag>
docker push ghcr.io/omnuron-labs/omniclaw-hosted-ops-console:<tag>
```

Create private env files from the checked-in examples. Do not commit filled copies.

- `production.compose.env.example`: image names, bind addresses, and per-service env-file paths. This file is used by Docker Compose interpolation and is not mounted into containers.
- `production.env.example`: signer-bearing API and reconciler runtime env.
- `production.metrics.env.example`: metrics exporter env. It must not contain signer private keys or ops session secrets.
- `production.ops.env.example`: ops console env. It must not contain signer private keys.

Required production gates:

- Postgres DSNs include `sslmode=require`, `sslmode=verify-ca`, or `sslmode=verify-full`.
- Redis uses `rediss://`.
- OIDC issuer/JWKS and OpenFGA API URLs use HTTPS.
- `OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID` is pinned.
- `OMNICLAW_HOSTED_OTEL_REQUIRED=true`.
- `OMNICLAW_HOSTED_JSON_LOGS=true`.
- `OTEL_EXPORTER_OTLP_ENDPOINT` and `OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL` point to the external HTTPS telemetry backend.
- `OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS=1` until nonce and provider behavior are proven under higher concurrency.
- `OMNICLAW_HOSTED_POSTGRES_POOL_SIZE` and `OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS` are positive.
- `OMNICLAW_HOSTED_API_KEY_HASH_PEPPER` is a real 32+ character random secret. Hosted production must not store API-key hashes with an unpeppered SHA-256 fallback.
- The signer private key is injected from a private secret source, not stored in the repo. The startup validator requires a usable EVM key, rejects low-entropy and repeated-pattern placeholders, and rejects `OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS` when it does not match the derived signer address.
- Demo API-key prefixes are not present in hosted env.
- Ops/OpenFGA secrets are real values, not placeholders. The ops console runs a runtime validator before `next start`, and `/healthz` fails closed when the session secret or OIDC env is unsafe.
- Metrics readiness is stricter than liveness. `/healthz` is process liveness; `/readyz` requires the Postgres DSN and, in alpha/production strict mode, signer and Gateway canary configuration.
- Signer and Gateway canary addresses are valid EVM addresses; Gateway canary minimum is positive.

Do not scale `hosted-facilitator` above one replica with the same signer key. The current signer guard serializes submissions inside one process. Horizontal scaling with one EOA signer needs a distributed nonce/signing lock or an external signer service first.

## Validate Config

```bash
cd infra/compose
docker compose --env-file /secure/path/hosted.compose.env \
  -f docker-compose.production.yml config >/tmp/omniclaw-hosted-compose.yml
```

The `hosted-facilitator` and `hosted-reconciler` containers run `python -m hosted_facilitator.hosted.production_env` before serving when `OMNICLAW_HOSTED_FACILITATOR_MODE=production`. This no-socket validator fails closed before API or reconciliation startup if required hosted gates are missing or unsafe.

Manual preflight:

```bash
docker run --rm --env-file /secure/path/production.env \
  ghcr.io/omnuron-labs/omniclaw-hosted-facilitator:<tag> \
  python -m hosted_facilitator.hosted.production_env
```

## Start

```bash
cd infra/compose
docker compose --env-file /secure/path/hosted.compose.env \
  -f docker-compose.production.yml up -d
```

By default, API, ops, metrics, and Prometheus ports bind to `127.0.0.1`. Put a TLS reverse proxy in front of the API and ops console instead of exposing container ports directly.

If startup validation fails, inspect the API and reconciler logs:

```bash
docker compose --env-file /secure/path/hosted.compose.env \
  -f docker-compose.production.yml logs hosted-facilitator hosted-reconciler
```

## Verify

Production Compose verification should exercise the running containers and the external dependencies directly:

```bash
cd infra/compose
docker compose --env-file /secure/path/hosted.compose.env -f docker-compose.production.yml ps
curl -fsS http://127.0.0.1:4022/healthz
curl -fsS http://127.0.0.1:9108/readyz
curl -fsS http://127.0.0.1:9108/metrics | rg 'omniclaw_hosted'
curl -fsS http://127.0.0.1:9090/api/v1/rules
```

Confirm the following before external alpha traffic:

- `/internal/readyz` is reachable only from the metrics exporter or trusted private network path.
- Prometheus shows the hosted facilitator targets as up and the alert rules loaded.
- New verify/settle attempts create settlement and audit records in the external Postgres databases.
- The signer gas and Gateway canary metrics are present.
- A seller integration uses only the public facilitator URL and seller API key.

`scripts/hosted_facilitator_readiness.sh` is for the repository Compose stack only. It assumes the bundled services from `docker-compose.yml`.

`scripts/hosted_facilitator_tunnel_smoke.sh` is for the repository Compose stack only. For production Compose, run a dedicated HTTPS smoke through the deployed reverse proxy and verify persistence in the external databases.

## Developer Production-Layout Smoke

This is a developer verification mode, not a production deployment and not external-alpha evidence. It uses the production container topology from `docker-compose.production.yml`, but connects those containers to local insecure dependencies from `docker-compose.yml`.

Use it only to prove image build, Compose wiring, healthchecks, metrics, and Prometheus rules before running the real seller/buyer payment smoke.

Developer production-layout smoke must use ignored files named like:

```text
infra/compose/.local-production.compose.env
infra/compose/.local-production.api.env
infra/compose/.local-production.metrics.env
infra/compose/.local-production.ops.env
infra/compose/.local-production.override.yml
```

The developer override may attach the production-layout services to the repository Compose dependency network so they can reach `postgres`, `redis`, `keycloak`, `openfga`, and the bundled OTEL collector. Those dependencies are test-only: Postgres is not TLS, Redis is not `rediss://`, OIDC/OpenFGA are HTTP, and OTEL is HTTP. Do not copy this override or these env files into hosted deployment.

The repository Compose Keycloak service still runs the Keycloak production server command, uses Postgres storage, pins the hostname, and accepts forwarded headers from the reverse proxy. The developer limitation is the dependency boundary: the bundled Postgres/Redis/OpenFGA/OTEL services are for local and alpha smoke testing, not managed production services.

Run production-layout smoke on alternate localhost ports so it does not replace the default Compose stack:

```bash
cd infra/compose
docker compose --env-file .local-production.compose.env \
  -f docker-compose.production.yml \
  -f .local-production.override.yml \
  up -d
```

Expected checks:

```bash
curl -fsS http://127.0.0.1:4122/healthz
curl -fsS http://127.0.0.1:4122/internal/readyz
curl -fsS http://127.0.0.1:9208/readyz
curl -fsS http://127.0.0.1:9208/metrics | rg 'omniclaw_hosted'
curl -fsS http://127.0.0.1:3101/healthz
curl -fsS http://127.0.0.1:9190/api/v1/targets
```

Passing this mode means the production-layout containers can boot locally. It does not replace production checks against external TLS Postgres, `rediss://` Redis, HTTPS OIDC/OpenFGA, HTTPS OTEL, deployed reverse proxy, or external database persistence.

After the boot checks pass, run the seller/buyer payment smoke against the production-layout facilitator URL. Boot health alone is not enough.

For developer verification, `scripts/hosted_facilitator_tunnel_smoke.sh` may be pointed at the production-layout facilitator port. This works only when the production-layout containers are attached to the `docker-compose.yml` dependency network and share that Postgres database. If the stack uses an external database, run the same persistence checks against that database instead.

Required external smoke evidence:

- `/v2/x402/supported` returns both `exact_evm` and `circle_gateway` for `eip155:5042002`.
- Mixed seller `/rails` advertises both rails, and the Gateway option has `extra.name == "GatewayWalletBatched"`.
- Exact-only seller mode advertises only `exact_evm`.
- One exact payment settles and returns a valid EVM transaction hash.
- One Gateway payment settles and returns a valid Gateway transfer UUID.
- Before the canary payments, capture high-water IDs for `settlement_records` and `settlement_attempts`.
- After payment, require new rows strictly above those IDs scoped to the supplied API key hash, provider `exact_evm` or `circle_gateway`, `network='eip155:5042002'`, `status='settled'`, matching record/attempt transaction IDs, and persisted payment shape matching independent expected values: buyer address derived from the buyer key, configured seller `payTo`, configured asset, atomic amount derived from price, raw requirement network, and `extra.name`.
- Metrics still report `omniclaw_hosted_facilitator_up 1`, `omniclaw_hosted_metrics_exporter_scrape_success 1`, signer gas above minimum, Gateway canary above minimum, and no active production alerts.
