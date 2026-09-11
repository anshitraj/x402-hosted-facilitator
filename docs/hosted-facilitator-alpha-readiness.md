# Hosted Facilitator Alpha Readiness

This document is the release gate for giving external sellers access to the OmniClaw hosted facilitator alpha.

Alpha-ready means a small number of trusted sellers can integrate with the hosted facilitator URL and seller API key, receive paid x402 requests, and have OmniClaw operators observe, pause, reconcile, and audit the system without using shell-only operational paths.

## Alpha Definition

The alpha release is complete when these flows are proven from three separate perspectives:

- Facilitator operator: deploys and operates the hosted facilitator through Docker Compose and the ops control plane.
- Seller: runs its own resource server configured only with OmniClaw facilitator URL, seller API key, network, asset, and `payTo`.
- Buyer: pays through the public seller URL using x402-compatible buyer tooling or OmniClaw buyer infrastructure.

The seller and buyer are external actors. They must not import private facilitator code or depend on OmniClaw core internals.

## Current Built Surface

### Runtime

- Standalone private hosted facilitator package under `src/hosted_facilitator`.
- Developer Docker Compose stack with Postgres, Redis, Keycloak, OpenFGA, OTEL collector, Prometheus, facilitator API, reconciler, metrics exporter, and ops console.
- Production-oriented Docker Compose topology under `infra/compose/docker-compose.production.yml`.
- Production env validator for hosted mode.
- Developer and production-layout deployment docs.

### Seller-Facing API

- `/supported`, `/verify`, `/settle`.
- `/v1/x402/*` and `/v2/x402/*` compatibility paths.
- Seller API-key auth.
- Compatibility paths are guarded so URL shape alone cannot authorize money movement.
- Seller policy checks for providers, networks, assets, and `payTo`.

### Providers

- `exact_evm` provider for EVM exact settlement.
- Circle Gateway provider for Gateway-batched nanopayments.
- Provider-neutral routing based on scheme, network, and Gateway metadata.
- Supported-network config loaded from environment.
- Hosted signer guard with concurrency cap.

### Settlement Safety

- Payment fingerprinting.
- Durable Postgres settlement records and attempts.
- Atomic seller/API-key/audit creation path.
- Duplicate settlement handling for in-flight, terminal, unknown, and manual-review states.
- Reconciliation worker for submitted/unknown outcomes.
- Manual-review terminal state.
- Rate limiting and invalid-request buckets.

### Control Plane

- OIDC sign-in through the Next.js ops console.
- Backend OpenFGA authorization for ops API routes.
- Seller onboarding and one-time API-key display.
- Provider/network/global pause controls.
- Audit tail.
- Provider health, signer runway, settlement status, provider/network counts, and oldest settlement age.
- Restrained dark operations UI design documented in `docs/control-plane-ui-design.md`.

### Observability

- JSON logs.
- OTEL required mode.
- OTEL collector required by Compose and production deployments.
- Prometheus metrics exporter.
- Alert rules for facilitator health, metrics scrape, reconciler, signer gas, Gateway canary balance, and settlement backlog.
- Ops runbook.

## Alpha Release Gates

### Gate 1: Repository Separation

Status: closed.

Requirements:

- Hosted facilitator lives in this private repo.
- Public OmniClaw core remains buyer/agent infrastructure only.
- Seller example stays outside the facilitator runtime boundary.
- Seller apps use official x402 middleware or seller-owned server code; this service ships no seller runtime.

Evidence:

- `README.md` defines the runtime boundary.
- Public core was cleaned and merged separately.

### Gate 2: Local Compose Boot

Status: closed for the current Compose stack.

Requirements:

- `docker compose -f docker-compose.yml up --build` starts all repository Compose services.
- Facilitator `/internal/readyz` is healthy.
- Ops console `/healthz` is healthy.
- Reconciler and metrics exporter are healthy.
- Prometheus loads alert rules.

Evidence to capture before external alpha:

```bash
docker compose -f docker-compose.yml ps
curl -fsS http://127.0.0.1:4022/internal/readyz
curl -fsS http://127.0.0.1:3001/healthz
curl -fsS http://127.0.0.1:9108/metrics | rg 'omniclaw_hosted'
curl -fsS http://127.0.0.1:9090/api/v1/rules
```

### Gate 3: Ops Control Plane

Status: closed for the current Compose build.

Requirements:

- OIDC login works.
- OpenFGA authorizes backend ops routes.
- No ops token fallback exists.
- Overview uses four operational KPIs maximum.
- Operators can create seller accounts and copy one-time API keys.
- Operators can pause/resume global, provider, and network targets.
- Audit tail updates after writes.
- Control plane exposes provider/network settlement counts and oldest settlement age.

Evidence:

- Ops console typecheck, lint, unit tests, Playwright, and Docker production build pass.
- Playwright signs in through Keycloak, opens settlements, creates a seller, verifies one-time API-key/profile visibility, pauses/resumes a provider, and verifies audit rows.
- Generated seller keys have been used for mixed, exact-only, and Gateway-only external seller canaries.
- Backend overview tests cover clean and dirty/sanitized data.
- The UI test asserts removed public concepts such as project ID and route key are not rendered.

### Gate 4: Seller Onboarding

Status: closed for the current Compose build.

Closed:

- Internal operator-created seller flow exists.
- API key is returned once.
- Audit event records key prefix only.
- Alpha onboarding handoff format exists.

Evidence:

- Sellers have been created through the live ops console, including the current external test seller `abiorh-mpfb41t2`.
- The raw API key was shown once, then cleared from the UI.
- `/v2/x402/supported` over HTTPS returned both `exact_evm` and `GatewayWalletBatched` for that key.
- The handoff template is defined in `docs/hosted-facilitator-alpha-onboarding.md`.

### Gate 5: Exact EVM Payment Canary

Status: closed for the current Compose build.

Closed:

- Exact provider exists.
- Exact payment has been tested against Arc testnet from separated buyer, seller, and facilitator perspectives.
- Compose smoke script supports exact payment.
- Current canary settled `exact_evm` on `eip155:5042002` after the Gateway metadata fix.

Current build evidence:

```text
provider=exact_evm
network=eip155:5042002
status=settled
transaction=0x97d9cfcfaad2b0602ffdb46571a9e0de9f7708a70faf4653ece0eeb390b5c6f9
settlement_record_id=3
settlement_attempt_id=3
payer=0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7
payTo=0x95cE2edF56cc05b2634267d55D8AfB7f8630c143
asset=0x3600000000000000000000000000000000000000
amount=1000
```

Latest serial exact-only evidence:

```text
provider=exact_evm
network=eip155:5042002
status=settled
seller_account_id=abiorh-mpfb41t2
payment_profile_id=default
transaction=0xae819bf93941d1447221fb7c05c6f92de5382df5243b912b2916dc16adde9c8c
settlement_record_id=9
payer=0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7
payTo=0x409C1e4cCc1455c2A86C01cBbCc9Ae41f8BBAc76
asset=0x3600000000000000000000000000000000000000
amount=1000
```

The exact canary should run serially when it shares the buyer wallet with a
Gateway canary. A parallel mixed test correctly hit the buyer wallet lock with
`Wallet is busy`; the serial retry settled successfully.

### Gate 6: Circle Gateway Payment Canary

Status: closed for the current Compose build.

Closed:

- Circle Gateway provider exists.
- Gateway route detection uses `paymentRequirements.extra.name == "GatewayWalletBatched"`.
- Gateway-only and mixed seller modes exist in the external seller smoke harness.
- Persistence assertions exist in the HTTPS tunnel smoke script.
- Current canary settled `circle_gateway` on `eip155:5042002` and returned a Gateway transfer UUID after preserving Gateway `extra` metadata in the retry payload.

Current build evidence:

```text
provider=circle_gateway
network=eip155:5042002
extra.name=GatewayWalletBatched
status=settled
transaction=55e4e3a1-f92d-4ff8-b5f8-561fe019abfe
settlement_record_id=4
settlement_attempt_id=4
payer=0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7
payTo=0x95cE2edF56cc05b2634267d55D8AfB7f8630c143
asset=0x3600000000000000000000000000000000000000
amount=1000
```

Latest Gateway-only evidence:

```text
provider=circle_gateway
network=eip155:5042002
extra.name=GatewayWalletBatched
status=settled
seller_account_id=abiorh-mpfb41t2
payment_profile_id=default
transaction=31ae73ca-c9ba-4700-8d83-d60ab3dae025
settlement_record_id=8
payer=0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7
payTo=0x409C1e4cCc1455c2A86C01cBbCc9Ae41f8BBAc76
asset=0x3600000000000000000000000000000000000000
amount=1000
```

Metrics evidence after canaries:

```text
omniclaw_hosted_gateway_canary_available_atomic 10008000
omniclaw_hosted_gateway_canary_above_min 1
```

### Gate 7: Public HTTPS External Shape

Status: closed for the current Compose build through Cloudflare temporary HTTPS tunnels.

Requirements:

- Facilitator is reachable over HTTPS through the same shape sellers will use.
- Seller is reachable over HTTPS separately.
- Seller is configured only with facilitator URL and seller API key.
- Buyer pays the seller public URL.
- Exact and Gateway payments both pass through the public URLs.
- Database persistence is verified after each payment.

Local evidence command:

```bash
X402_FACILITATOR_API_KEY="<short-lived-seller-key>" \
X402_SELLER_PAY_TO="<seller-payto>" \
BUYER_ENV_FILE=.env \
bash scripts/hosted_facilitator_tunnel_smoke.sh
```

Production evidence must use the deployed HTTPS reverse proxy instead of temporary tunnels.

Current build evidence:

- Public facilitator tunnel returned `/v2/x402/supported` with both rails for the generated seller API key.
- Public seller tunnel advertised both rails in mixed mode.
- Buyer paid the public seller HTTPS URL for Exact EVM and Gateway.
- `scripts/hosted_facilitator_tunnel_smoke.sh` now also verifies exact-only seller mode.
- Persistence was verified in Postgres using high-water settlement and attempt IDs scoped to the API-key hash.

### Gate 8: Reconciliation And Failure Handling

Status: partially closed.

Closed:

- Reconciler exists.
- Unknown/submitted/manual-review states exist.
- Metrics and runbook sections exist.
- Duplicate `unknown` and `manual_review` states block blind resubmission.

Still to close:

- Run an induced provider-timeout or mocked unknown outcome against the running Compose stack.
- Confirm record enters `unknown`.
- Confirm reconciler resolves or moves to `manual_review` according to policy.
- Confirm ops console oldest-age and manual-review indicators update.
- Confirm pause controls can stop affected provider/network before investigation.

### Gate 9: Production Compose Deployability

Status: partially closed.

Closed:

- Production Compose file exists.
- Production env examples exist.
- Production validator exists.
- Deployment docs exist.
- Production Compose config validates with the example compose env.
- Repository Docker images build in the isolated Compose stack.
- Ops console Docker image runs a startup runtime-env validator before serving.
- Metrics exporter has `/readyz` for strict signer/Gateway canary configuration.

Still to close:

- Run local production-layout smoke on alternate ports.
- Confirm production validator fails unsafe local values and accepts intended alpha deploy env.
- Confirm metrics exporter and ops console boot in production-layout mode.

### Gate 10: Security And Secret Hygiene

Status: partially closed.

Closed:

- Seller API keys are hashed at rest.
- One-time raw API-key display.
- Ops console stores OIDC token server-side in encrypted HTTP-only session cookie.
- Production validator rejects any configured default facilitator API key, insecure Redis, unsafe Postgres SSL modes, placeholder signer keys, weak ops session secrets, missing API-key hash pepper, and missing OpenFGA model pinning.
- Ops console runtime validator rejects documented placeholder and low-entropy session secrets before startup.
- Control-plane overview sanitizes provider networks and settlement aggregate fields.
- Cached duplicate settlement now re-checks current payment profile policy and pause state before returning an existing outcome.

Still to close:

- Run a secret-scan over logs, traces, rendered UI payloads, and settlement records after exact and Gateway canaries.
- Confirm no raw API key, private key, bearer token, full payment signature, or provider credential appears.
- Confirm screenshots used for external updates contain no raw API keys.

### Gate 11: Operational Handoff

Status: partially closed.

Closed:

- Runbook exists.
- Alert rules exist.
- Alpha onboarding doc exists.
- Prometheus config validates in the running container and loads 16 hosted facilitator rules.
- Prometheus currently reports no firing hosted facilitator alerts.

Still to close:

- Create first-tester handoff packet from the onboarding template.
- Define alpha support response rules: pause first, inspect by correlation ID, never manually resubmit outside idempotency.
- Create an incident evidence template for exact and Gateway canaries.
- Decide initial fee/billing measurement for transaction-count monetization.

## Next Chunks

### Chunk A: Commit Current Control-Plane And Observability Work

Goal: freeze the current verified control-plane/backend overview work.

Done when:

- Current diff is reviewed.
- Tests pass.
- Commit is created in the private repo.

### Chunk B: Live Local Compose Canary

Goal: prove current build from the three real perspectives.

Steps:

1. Rebuild Docker Compose.
2. Create seller account through ops console.
3. Start external seller with only facilitator URL and API key.
4. Run exact payment.
5. Run Gateway payment.
6. Verify DB persistence and ops console updates.
7. Retry exact duplicate and confirm no second settlement.

### Chunk C: Public HTTPS Canary

Goal: prove external shape using public HTTPS URLs.

Steps:

1. Create short-lived seller API key.
2. Run `scripts/hosted_facilitator_tunnel_smoke.sh`.
3. Capture exact transaction hash and Gateway transfer UUID.
4. Capture settlement and attempt rows.
5. Capture metrics and alert state.

### Chunk D: Production-Layout Compose Smoke

Goal: prove deployability before EC2.

Steps:

1. Build production images.
2. Run local production-layout Compose on alternate ports.
3. Validate production env.
4. Run health, metrics, and payment smoke against production-layout URL.

### Chunk E: EC2 Alpha Deployment

Goal: deploy private alpha for a few testers.

Steps:

1. Provision host, firewall, Docker, Compose, TLS reverse proxy, and private env files.
2. Attach external Postgres/Redis or explicitly document the temporary single-host alpha dependency decision.
3. Start production Compose.
4. Run HTTPS exact and Gateway canaries.
5. Onboard first tester with API key and handoff packet.

## Non-Goals Before First Alpha

- Seller self-service dashboard.
- Broad public developer onboarding.
- Mainnet support.
- Multiple EVM networks beyond the explicitly configured first alpha network.
- Horizontal scaling with one shared EOA signer.
- CLI-only admin workflows.

## First Alpha Go Decision

The first external tester can receive access only after these are true in the same build:

- Gate 1 closed.
- Gate 2 closed.
- Gate 3 closed.
- Gate 4 closed with a real generated seller API key.
- Gate 5 closed on current build.
- Gate 6 closed on current build.
- Gate 7 closed through public HTTPS shape.
- Gate 8 failure path at least manually exercised.
- Gate 10 secret hygiene checked after canaries.
- Gate 11 handoff packet prepared.

Production-layout deployability and EC2 deployment are separate gates for hosting outside the local workstation. They should be closed before inviting anyone who is not directly coordinating with OmniClaw in real time.

## Next Step: Live Deployment

The local stack has proven the control-plane, Exact EVM, Gateway, observability, and external HTTPS shape. The next chunk is not more local feature work; it is live deployment using `docs/live-deployment-checklist.md`.

Live deployment must repeat the same checks against the deployed facilitator and ops console domains:

- production Compose config with private env files.
- production env validator with real secret material.
- OIDC/OpenFGA operator login.
- seller creation through the ops console.
- `/v2/x402/supported` over the deployed facilitator domain with the generated seller API key.
- Exact EVM and Gateway payments through an external seller URL.
- Postgres persistence, Prometheus alerts, signer gas, Gateway canary, and audit verification.
