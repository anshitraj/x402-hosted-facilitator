# Hosted Facilitator Architecture

This service is the standalone OmniClaw hosted x402 facilitator. It is separate from OmniClaw core, seller applications, and buyer applications.

## Product Boundary

The hosted facilitator owns:

- x402 `/supported`, `/verify`, and `/settle` APIs.
- Seller account policy and seller API-key authentication.
- Exact EVM settlement submission.
- Circle Gateway batched nanopayment settlement.
- Durable settlement records and settlement attempts.
- Reconciliation for submitted and unknown outcomes.
- Operator control plane, audit trail, pause controls, and operational telemetry.

The hosted facilitator does not own:

- Seller resource servers.
- Buyer payment orchestration.
- OmniClaw core buyer infrastructure.
- Seller runtime code.
- Buyer runtime code.
- Browser-side payment credentials.

Sellers integrate by configuring their own server with:

```text
Facilitator base URL
Facilitator x402 path
Seller API key
Network
Asset
payTo address
```

The seller API key is the only seller credential in the alpha handoff. OmniClaw resolves the seller account and payment profile server-side from the API-key hash. Sellers do not send a separate seller identifier, payment profile identifier, route key, or project ID.

The domain model is defined in [hosted-facilitator-seller-domain-design.md](./hosted-facilitator-seller-domain-design.md):

```text
Seller account
  -> Payment profile
       -> API keys
```

## Runtime Topology

The Docker Compose deployment is intentionally simple:

- `hosted-facilitator`: FastAPI x402 facilitator API.
- `hosted-reconciler`: reconciliation worker for ambiguous or submitted settlements.
- `hosted-metrics-exporter`: operational metrics derived from Postgres, provider checks, signer checks, and canary checks.
- `ops-console`: Next.js operator control plane.
- Postgres: settlement, attempts, seller accounts, API-key hashes, audit, and control-plane state.
- Redis: rate limits and short-lived coordination.
- OIDC provider: operator authentication.
- OpenFGA: operator authorization checks.
- OTEL collector or external OTEL endpoint: required trace/metrics/log export.
- Prometheus: deployment metrics and alert rules.

Production deployments use external hardened Postgres, Redis, OIDC, OpenFGA, and telemetry endpoints. The repository Compose stack is for developer verification and canary testing only.

## Public x402 API

The public facilitator API is protocol-only:

```text
GET  /supported
POST /verify
POST /settle

GET  /v1/x402/supported
POST /v1/x402/verify
POST /v1/x402/settle

GET  /v2/x402/supported
POST /v2/x402/verify
POST /v2/x402/settle
```

All money-moving calls require:

```text
Authorization: Bearer <seller_api_key>
```

`/supported` returns only the rails enabled for the resolved payment profile. `/verify` and `/settle` enforce the same payment profile policy before any provider call.

## Seller Domain Model

A seller account stores the business identity:

- tenant/cohort reference for operator grouping.
- human-readable seller name.
- technical contact.
- status: active, disabled, or manual_review.

Operational pauses are stored separately in control-plane pause state. Any non-`active` seller status denies settlement.

A payment profile stores the money policy for one seller app/API:

- enabled providers.
- enabled networks.
- enabled x402 schemes.
- allowed asset list.
- allowed `payTo` list.
- rate-limit policy.

An API key is scoped to one payment profile. Raw API keys are shown once at issuance. Only hashes and prefixes are stored.

API-key storage uses a keyed lookup hash. The key row must be constrained so it cannot point to a payment profile owned by another seller. Payment profile changes to `payTo` are high-risk money-policy changes and require validation, audit, and approval before production activation.

## Provider Routing

Provider selection is driven by the seller policy and x402 payment requirement:

| Provider | Scheme | Network | Selector |
| --- | --- | --- | --- |
| `exact_evm` | `exact` | configured EVM networks | no Gateway batched marker |
| `circle_gateway` | `exact` | configured Gateway networks | `extra.name == "GatewayWalletBatched"` |

Exact EVM and Circle Gateway are separate provider paths. They do not inherit network config from each other. Each supported provider/network pair is configured explicitly from environment.

## Exact EVM Settlement

Exact EVM uses the hosted facilitator signer to submit settlement on the selected EVM network. The signer pays gas. The buyer signs the x402 payment authorization; the facilitator submits the provider transaction after verifying seller policy, payment shape, and idempotency.

The same hosted EOA may be used across EVM networks if it is funded on each supported chain. Gas balance and runway are tracked per network.

## Circle Gateway Settlement

Circle Gateway batched nanopayments are x402 payments using Gateway metadata. The facilitator verifies and settles Gateway transfer intents through the Gateway x402 path. The hosted facilitator does not require seller Circle accounts or Circle admin API keys for this x402 verify/settle flow.

Gateway canaries require a funded buyer Gateway balance so the buyer can produce a valid payment. That balance is buyer-side test evidence, not facilitator custody.

## Settlement Safety

Every `/settle` request is guarded by:

- payment fingerprinting before provider submission.
- durable uniqueness scoped to the resolved seller account, payment profile, and fingerprint.
- provider-level replay protection scoped to the canonical buyer authorization.
- claim-and-start semantics for concurrent requests.
- terminal-state reuse for duplicate retries.
- unknown/manual-review states for ambiguous outcomes.
- provider transaction/transfer persistence.
- reconciliation leases for submitted and unknown records.

Provider submission must happen only after the settlement record is claimed. Duplicate requests must not create a second provider transaction.

## Control Plane

The control plane is for OmniClaw operators, not sellers. It must support:

- OIDC sign-in.
- OpenFGA permission checks for every write.
- seller account creation.
- payment profile creation.
- one-time API-key issuance scoped to a payment profile.
- global, provider, and network pause/resume.
- settlement overview and status breakdown.
- signer and Gateway canary health.
- reconciliation status.
- audit tail with redacted identifiers only.

Frontend-facing operator routes use seller-account language:

```text
GET  /api/ops/overview
POST /api/ops/pauses
POST /api/ops/sellers
```

The seller dashboard will be a separate frontend later. It must not be mixed into the operator console.

## Observability

Required signals:

- JSON structured logs.
- OTEL traces with correlation IDs.
- Prometheus deployment gauges.
- signer gas and runway metrics.
- Gateway canary balance metrics.
- settlement backlog metrics.
- duplicate settlement claim metrics.
- reconciliation heartbeat metrics.
- provider error metrics.

Telemetry must not leak raw API keys, private keys, full payment payloads, or wallet addresses where metrics labels would create cardinality and privacy risk.

## Deployment Rules

Production and staging startup must fail closed when:

- Postgres DSNs are missing TLS requirements.
- Redis is plaintext.
- hosted URL is not HTTPS.
- OTEL is disabled.
- JSON logging is disabled.
- signer private key is placeholder or does not match configured signer address.
- OIDC/OpenFGA endpoints are insecure or unpinned.
- weak operator session secrets are configured.
- demo API-key prefixes are configured.

## Alpha Readiness

Alpha access is allowed only after these are proven:

1. Compose deployment boots cleanly.
2. Operator sign-in and OpenFGA authorization work.
3. Seller account and payment profile creation returns a one-time profile-scoped API key.
4. `/v2/x402/supported` works with that API key.
5. Exact EVM canary settles on Arc Testnet.
6. Gateway batched canary settles on Arc Testnet.
7. Duplicate buyer retry does not submit a second provider settlement.
8. Ops console counters and audit update after canaries.
9. Reconciler handles submitted/unknown records.
10. Alerts and runbooks cover signer gas, Gateway canary balance, provider errors, backlog, duplicate settles, and telemetry failure.
