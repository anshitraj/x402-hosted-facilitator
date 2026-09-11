# Hosted Facilitator End-to-End Specification

This specification defines the alpha contract for OmniClaw hosted facilitator from three separate perspectives: facilitator operator, seller, and buyer.

## Non-Negotiables

- Seller handoff is facilitator URL plus seller API key.
- The seller API key identifies the seller account and payment profile server-side.
- Sellers do not send a separate seller identifier, payment profile identifier, route key, or project ID.
- Seller API keys are never placed in browser bundles, buyer clients, URLs, logs, traces, metrics, or tickets.
- Exact EVM and Circle Gateway are separate provider paths with separate network config.
- The facilitator pays Exact EVM gas from the hosted signer.
- Gateway x402 verify/settle does not require seller Circle accounts or Circle admin API keys.
- Every settlement is idempotent before provider submission.
- The same canonical buyer authorization cannot be submitted twice across payment profiles.
- Ambiguous money movement goes to reconciliation or manual review, never blind retry.

## Roles

Operator:

- deploys the hosted facilitator.
- signs in to the control plane.
- creates seller accounts.
- creates payment profiles.
- issues and rotates seller API keys scoped to payment profiles.
- pauses and resumes settlement paths.
- observes settlement, reconciliation, signer, and provider health.

Seller:

- runs its own resource server.
- configures official x402 middleware or equivalent server-side integration.
- sends the seller API key to the facilitator only from server-side code.
- chooses which advertised payment requirement to accept.

Buyer:

- pays the seller through x402-compatible buyer tooling or OmniClaw buyer infrastructure.
- never receives the seller API key.
- owns the buyer EOA and buyer Gateway balance required for its payment.

## Seller Handoff

The alpha handoff must contain only:

```text
Facilitator origin: https://<hosted-facilitator-domain>
Full x402 endpoint: https://<hosted-facilitator-domain>/v2/x402
One-time API key: <omck_alpha_... shown once>
Network: eip155:5042002
Scheme: exact
Rails: EVM exact, Circle Gateway
x402 Gateway metadata: extra.name=GatewayWalletBatched
Asset: <Arc Testnet USDC address>
PayTo: <seller payTo address>
```

No separate seller identifier is included. OmniClaw resolves the account from the API-key hash.

The one-time API key must be delivered through an encrypted secret link, password-manager share, or live call. Email/DM may include non-secret handoff values only.

The API key resolves to exactly one active payment profile. The payment profile owns the allowed providers, networks, schemes, assets, `payTo` addresses, and rate limits for that seller integration.

## Public Protocol Routes

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

Required header:

```text
Authorization: Bearer <seller_api_key>
```

## Operator Routes

The operator console uses:

```text
GET  /api/ops/overview
POST /api/ops/pauses
POST /api/ops/sellers
```

Those browser routes proxy to fixed FastAPI backend routes under `/ops/api/*`.

Writes require an authenticated OIDC session and OpenFGA authorization. There is no ops-token fallback.

## `/supported`

Input:

- seller API key in the authorization header.

Output:

- enabled payment requirements for the resolved payment profile.
- `exact_evm` requirements for configured Exact EVM networks.
- `circle_gateway` requirements only when Gateway is enabled for the payment profile and network.
- Gateway requirements include `extra.name == "GatewayWalletBatched"`.

Failure:

- invalid or missing API key returns unauthorized.
- disabled seller account returns denied.
- paused provider/network excludes or blocks the affected route according to the pause policy.

## `/verify`

Validation order:

1. Authenticate seller API key.
2. Resolve seller account and payment profile policy.
3. Parse x402 payload.
4. Validate scheme, network, asset, and `payTo`.
5. Select provider from payment requirement.
6. Run provider verification.
7. Return protocol-compatible verification response.

Provider verification must not submit money movement.

## `/settle`

Validation order:

1. Authenticate seller API key.
2. Resolve seller account and payment profile policy.
3. Parse x402 payload.
4. Validate scheme, network, asset, and `payTo`.
5. Compute payment fingerprint.
6. Claim settlement record atomically.
7. Return existing terminal result for duplicate retries.
8. Submit provider settlement only for a newly claimed record.
9. Persist transaction hash or Gateway transfer UUID.
10. Mark final, unknown, or manual review.

Duplicate settlement attempts must increment duplicate counters and must not submit a second provider transaction.

In-flight duplicate settlement attempts return the current submitted/in-progress state with `duplicate=true` and retry guidance. They must not return a 5xx solely because another request already owns the settlement claim.

## Exact EVM Acceptance

A settled Exact EVM canary must prove:

```text
provider=exact_evm
network=eip155:5042002
status=settled
transaction=0x<64-hex>
```

The record and attempt must match:

- payer derived from the buyer key.
- seller `payTo`.
- configured asset.
- expected atomic amount.
- raw requirement network.
- provider `exact_evm`.

## Circle Gateway Acceptance

A settled Gateway canary must prove:

```text
provider=circle_gateway
network=eip155:5042002
extra.name=GatewayWalletBatched
status=settled
transaction=<gateway-transfer-uuid>
```

The record and attempt must match:

- payer derived from the buyer key.
- seller `payTo`.
- configured asset.
- expected atomic amount.
- raw requirement network.
- Gateway `extra.name`.

## Persistence Contract

The database must persist:

- seller account identity.
- payment profile policy.
- seller API-key hash and prefix.
- settlement record.
- settlement attempt.
- canonical payer, `payTo`, asset, amount, network, scheme, provider, and authorization id.
- provider transaction hash or Gateway transfer UUID.
- audit event for operator writes.
- duplicate claim counters.
- reconciliation status and heartbeat.

Uniqueness must be enforced on resolved seller account plus payment profile plus payment fingerprint. Provider-level replay uniqueness must also be enforced on the canonical buyer authorization.

## Reconciliation

The reconciler owns submitted and unknown outcomes:

- claim eligible records with a lease.
- check provider state.
- move records to settled, failed, unknown, or manual review.
- never submit a second provider transaction for the same fingerprint.
- expose heartbeat and backlog metrics.

## Pause Controls

Operators can pause:

- global settlement writes.
- a provider.
- a network.

Pauses are audited and enforced before provider submission.

## Rate Limits

Rate limits are scoped by:

- resolved seller account.
- resolved payment profile.
- endpoint.
- invalid request penalty bucket.
- provider/network where applicable.

Invalid payloads, unsupported routes, auth failures, and seller policy denials must consume invalid-request capacity.

## Observability

Every money-flow request must have:

- correlation ID.
- trace ID when OTEL is active.
- structured log fields for provider, network, status, and redacted fingerprint.
- no raw API keys.
- no private keys.
- no raw payment payloads.

Metrics must include:

- facilitator up.
- ready state.
- settlement status counts.
- provider/network counts.
- oldest submitted/unknown/manual-review age.
- duplicate settle claim count.
- reconciler heartbeat.
- signer gas.
- Gateway canary balance.
- provider error count.

## External Alpha Gate

External alpha is blocked until:

1. Docker Compose services build and run.
2. OIDC/OpenFGA control-plane write path works.
3. seller account and payment profile creation issues one-time profile-scoped API keys.
4. `/v2/x402/supported` returns both Exact EVM and Gateway for the alpha seller.
5. Exact EVM buyer-to-seller payment settles through the hosted facilitator.
6. Gateway buyer-to-seller payment settles through the hosted facilitator.
7. duplicate buyer retry does not cause duplicate settlement.
8. ops console settlement and audit views update after canaries.
9. Prometheus alert rules load.
10. runbook has actions for each critical alert.
