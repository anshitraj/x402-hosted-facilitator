# Hosted Facilitator Ops Runbook

This runbook is for the hosted OmniClaw x402 facilitator control plane, settlement API, and reconciler.

## First Checks

1. Open the ops console and confirm `/internal/readyz` is healthy on the facilitator container.
2. Check active pauses before changing traffic. A global, provider, or network pause may be intentional.
3. Inspect the latest control-plane audit events and correlate by request ID or trace ID.
4. Do not retry `/settle` manually against a live payment payload unless the settlement record is confirmed safe to retry.

Developer Compose readiness command for `docker-compose.yml`:

```bash
bash scripts/hosted_facilitator_readiness.sh
```

Observability first checks:

```bash
curl -fsS http://127.0.0.1:9108/healthz
curl -fsS http://127.0.0.1:9108/readyz
curl -fsS http://127.0.0.1:9108/metrics
curl -fsS http://127.0.0.1:9090/api/v1/targets
curl -fsS http://127.0.0.1:9090/api/v1/rules
```

Developer Compose money canary commands for `docker-compose.yml`:

```bash
X402_SELLER_PAY_TO="0xSellerPayTo" BUYER_ENV_FILE=.env bash scripts/hosted_facilitator_smoke.sh
X402_FACILITATOR_API_KEY="omck_short_lived_alpha_key" X402_SELLER_PAY_TO="0xSellerPayTo" BUYER_ENV_FILE=.env bash scripts/hosted_facilitator_tunnel_smoke.sh
```

The tunnel canary requires `cloudflared` and verifies the real external shape:
the facilitator is exposed over temporary HTTPS, the seller is configured with
that public facilitator URL plus its seller API key, and the buyer pays the
seller through a separate public HTTPS URL. The example harness can enable
test-only DNS-over-HTTPS resolution for `trycloudflare.com` hostnames when the
local resolver lags Cloudflare quick-tunnel DNS propagation.
The tunnel canary also requires local Compose Postgres and verifies persisted
`settled` settlement record/attempt rows for both exact and Gateway providers.

Gateway canaries require `OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED=true`,
`OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS=eip155:5042002`, buyer EOA credentials,
and enough buyer Gateway balance for the canary amount. Hosted facilitator
Gateway x402 verify/settle calls do not require a Circle account API key. The
public tunnel canary requires a seller-scoped API key issued from the control
plane. Set `CURL_DOH_URL=` to disable shell DNS-over-HTTPS when the local
resolver already handles quick `trycloudflare.com` hostnames.

## Metrics Exporter Down

Impact: Prometheus alerts and alpha operational gauges are unavailable. The x402 API may still be serving traffic, but operators lose the fast signal for signer gas, Gateway canary balance, settlement backlog, and reconciler heartbeat.

Actions:

1. Check `hosted-metrics-exporter` container health, `http://127.0.0.1:9108/healthz`, and `http://127.0.0.1:9108/readyz`.
2. Scrape `http://127.0.0.1:9108/metrics` and confirm `omniclaw_hosted_metrics_exporter_scrape_success 1`.
3. Confirm Postgres, reconciler heartbeat volume, Arc RPC, signer address config, and Circle Gateway canary calls are reachable.
4. Do not promote a deploy while this alert is firing; run readiness after the exporter recovers.

## Metrics Exporter Scrape Failure

Impact: the exporter is reachable, but one or more dependency collectors failed. Prometheus may have partial data only.

Actions:

1. Scrape `http://127.0.0.1:9108/metrics` and inspect `omniclaw_hosted_metrics_collector_success`.
2. If `collector="settlement_store"` failed, check Postgres health and migrations.
3. If `collector="reconciler_heartbeat"` failed, check the shared heartbeat volume and reconciler health.
4. If `collector="exact_signer"` failed, check Arc RPC and the configured signer address/network.
5. If `collector="gateway_canary"` failed, check Circle Gateway testnet status and the canary address/network.
6. Re-run readiness before clearing the incident.

## Facilitator Down

Impact: sellers cannot verify or settle hosted x402 payments.

Actions:

1. Confirm container health and recent deploy status.
2. Check Postgres, Redis, OIDC, OpenFGA, and OTEL collector readiness.
3. If storage is unhealthy, pause affected provider/network before restarting app instances.
4. Keep the reconciler online if there are submitted or unknown settlements.

## Reconciler Down

Impact: unknown/submitted settlements may not reach terminal state.

Actions:

1. Check the reconciler heartbeat file and container logs.
2. Confirm Postgres migration health and database connectivity.
3. Restart only the reconciler service if the API is healthy and settlement traffic can continue.
4. If unknown/submitted backlog is growing, pause settlement for the affected provider or network.

## Unknown Settlement Backlog

Impact: payment outcome is ambiguous and duplicate settlement risk is elevated.

Actions:

1. Inspect records by status and age from the ops console.
2. Confirm whether provider calls timed out, errored, or returned unknown transaction state.
3. Let the reconciler claim records; do not submit a second settlement from an operator shell.
4. If records exceed manual-review threshold, pause the affected provider/network and escalate with transaction hash, network, provider, key prefix, and redacted fingerprint only.

## Manual Review Backlog

Impact: settlement state could not be safely resolved automatically.

Actions:

1. Keep the affected records terminal as `manual_review` until chain/provider state is verified.
2. Cross-check provider receipts and seller-reported access grants.
3. Record the operator decision in the control-plane audit path.
4. Resume provider/network only after backlog cause is understood.

## Submitted Settlement Backlog

Impact: settlements were submitted to a provider or chain but have not reached a final state.

Actions:

1. Check provider/network status and compare the settlement transaction or transfer ID with chain/provider state.
2. Confirm the reconciler heartbeat is current and reconciliation leases are not stuck.
3. Pause the affected provider or network if finality is delayed or ambiguous.
4. Resolve through reconciliation; do not submit a second provider transaction for the same payment fingerprint.

## Settle In Progress Backlog

Impact: API workers claimed settlement fingerprints but did not durably record a submitted or final outcome.

Actions:

1. Inspect recent facilitator logs by correlation ID and check for API restarts, provider timeouts, or process crashes.
2. Confirm the reconciler is marking stale in-progress records `unknown` instead of retrying provider settlement blindly.
3. Keep duplicate `/settle` requests blocked by the existing idempotency record.
4. Escalate to manual review if provider state cannot prove whether money moved.

## Provider Error Spike

Impact: exact settlement or verification is failing for a provider/network.

Actions:

1. Compare provider health with RPC and chain explorer status.
2. Check signer health, signer status, nonce errors, and gas balance.
3. Pause the provider or network if failure is money-moving or ambiguous.
4. Resume only after a money canary and duplicate-settle canary pass.

## Duplicate Settle Spike

Impact: sellers or clients may be retrying aggressively.

Actions:

1. Confirm duplicate attempts are being classified before provider submission.
2. Check seller integration for client retry loops and timeout settings.
3. Keep settlement idempotency enabled. Do not disable fingerprinting or claim checks.
4. If duplicates become provider submissions, pause the affected provider immediately.

## Signer Low Gas

Impact: the hosted facilitator cannot pay settlement gas for the affected network.

`OmniClawHostedExactSignerGasReserveLow` is a ticket-level early warning. It means the signer is still above the hard floor but below 2x the configured minimum. Top up during the current operations window; do not wait for the page-level hard-floor alert.

Actions:

1. Confirm signer ID, network, balance, and configured minimum in the ops console provider view.
2. Top up the hosted signer on the affected chain with the chain-native gas asset.
3. If top-up cannot be completed immediately, pause the network before new settlements fail.
4. After top-up, run `/internal/readyz`, provider health, and one money canary before resuming.

## Gateway Buyer Balance Low

Impact: Gateway batch payment canaries fail before facilitator settlement because the buyer has no usable Gateway balance.

`OmniClawHostedGatewayCanaryReserveLow` is a ticket-level early warning. It means the canary still has usable Gateway balance but is below 2x the configured minimum. Top up through the Gateway deposit path before canaries begin failing.

Actions:

1. In production Compose, check the metrics exporter Gateway canary metrics and the configured canary address/network from `production.metrics.env`.
2. Deposit USDC into Circle Gateway using the Gateway Wallet deposit path, not a raw ERC-20 transfer.
3. Re-check the Circle Gateway balance for the canary address and confirm the metrics exporter reports the canary above threshold.
4. If production buyers report this, confirm their own Gateway balance; the hosted facilitator does not custody buyer Gateway funds.

For developer Compose only, the `buyerGateway` section of `scripts/hosted_facilitator_readiness.sh` can validate the bundled `docker-compose.yml` stack.

## Public HTTPS Smoke Failure

Impact: external sellers or buyers may fail even though localhost tests pass.

Actions:

1. In production Compose, run an HTTPS smoke through the deployed reverse proxy and capture the facilitator URL, exact transaction, Gateway transfer ID, external Postgres settlement/audit records, and backend trace IDs.
2. If `/rails` is correct but payment fails, inspect seller logs first, then facilitator logs by correlation ID.
3. If the public facilitator URL fails before seller startup, verify the TLS reverse proxy route, upstream container health, and `/healthz`.
4. If the seller can reach `/rails` but payments fail, verify API-key auth, route selection, settlement records, and reconciler attempts in the external databases.
5. Do not treat localhost-only smoke as deploy-ready; the production smoke must pass through the same HTTPS entrypoint sellers will use.

Acceptance criteria:

- `/v2/x402/supported` returns `exact_evm` and `circle_gateway` on `eip155:5042002`.
- Mixed seller `/rails` returns both rails, with Gateway advertised as `GatewayWalletBatched`.
- Exact-only seller mode returns only `exact_evm`.
- Exact payment returns a valid EVM transaction hash and Gateway payment returns a valid transfer UUID.
- Capture `settlement_records` and `settlement_attempts` high-water IDs before the payments. After the payments, require new records strictly above those IDs scoped to the supplied API key hash, provider `exact_evm` or `circle_gateway`, `network='eip155:5042002'`, `status='settled'`, matching record/attempt transaction IDs, and persisted payment shape matching independent expected values: buyer address derived from the buyer key, configured seller `payTo`, configured asset, atomic amount derived from price, raw requirement network, and `extra.name`.
- Prometheus has no active hosted facilitator alerts, signer gas and Gateway canary metrics are above minimum, and service logs have no new error tracebacks.

For developer Compose, `scripts/hosted_facilitator_tunnel_smoke.sh` exercises a temporary Cloudflare Tunnel around the bundled `docker-compose.yml` stack. When pointed at production-layout Compose ports, those containers must be attached to the same dependency network and share that Postgres database; otherwise, run the same persistence checks against the actual external Postgres database.

## Postgres Pool Saturation

Impact: settlement API latency rises and claims may time out.

Actions:

1. Check database CPU, locks, active connections, and slow queries.
2. Confirm app pool size and checkout timeout match deploy capacity.
3. Scale app instances only if the database has connection budget.
4. If settlement claims are timing out, pause money-moving routes before retries create pressure.

## Rate Limiter Unavailable

Impact: hosted safety controls are degraded.

Actions:

1. Check Redis health and network path from facilitator containers.
2. Keep hosted traffic fail-closed; do not bypass rate limits for external alpha.
3. Restore Redis, then verify `/internal/readyz` before resuming paused traffic.

## OTEL Collector Unavailable

Impact: traces and metrics are unavailable or degraded.

Actions:

1. In production Compose, check the external HTTPS OTEL health URL configured by `OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL`.
2. Confirm API and reconciler containers still have `OMNICLAW_HOSTED_OTEL_REQUIRED=true` and are exporting to the configured `OTEL_EXPORTER_OTLP_ENDPOINT`.
3. Inspect the external telemetry backend for ingestion/exporter errors and dropped spans/metrics.
4. Existing money-moving traffic may continue only if the API and reconciler are healthy and telemetry loss is explicitly accepted by incident command.

For developer Compose, check the bundled collector health at `:13133` and exported metrics at `:8889`.

## Prometheus Config Reload

Impact: alert changes are not active until Prometheus reloads or restarts.

Actions:

1. Validate config and rules before reload:

```bash
docker run --rm --entrypoint promtool \
  -v "$PWD/infra/prometheus/prometheus.production.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/infra/alerts/hosted-facilitator-alerts.production.yaml:/etc/prometheus/rules/hosted-facilitator-alerts.yaml:ro" \
  prom/prometheus:v3.1.0 check config /etc/prometheus/prometheus.yml
```

For developer Compose, use `infra/prometheus/prometheus.yml` and `infra/alerts/hosted-facilitator-alerts.yaml` so the bundled OTEL collector target is included.

2. Restart Prometheus or post to `http://127.0.0.1:9090/-/reload` when lifecycle reload is enabled.
3. Confirm targets and rules:

```bash
curl -fsS http://127.0.0.1:9090/api/v1/targets
curl -fsS http://127.0.0.1:9090/api/v1/rules
```
