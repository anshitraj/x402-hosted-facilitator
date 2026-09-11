# Control Plane Operations Roadmap

This roadmap turns the control plane into a money-operations console. Each chunk must ship with backend contracts, UI behavior, tests, and review from security/backend, UI/UX, and QA/SRE perspectives before the next chunk starts.

## Acceptance Standard

- No shell-only operator path for normal alpha operations.
- Every money-moving record is traceable from metric to seller to settlement to transaction evidence.
- Sensitive fields are allowlisted and permission-gated.
- Every async UI interaction clears stale state, shows loading state, has hover/focus behavior, and is keyboard reachable.
- Every new backend read has a typed frontend contract and route proxy test.
- Every production data path has direct store-level coverage or a live Postgres test that fails when the database is unavailable.
- Browser interaction tests run in CI for UI behavior. Full real-stack Playwright
  flows against Compose remain required deployment evidence and must not rely on
  browser route mocks.

## Chunk 1: Seller Ops Detail

Goal: make a seller click answer whether that seller is healthy.

Status: implemented in this branch with seller-scoped settlement health, reconciliation-risk summary, clickable recent money movement, API key visibility, typed UI contract, focused backend tests, and live Postgres coverage for seller settlement reads.

Deliver:

- Seller-level settlement metrics: total records, attempts, settled, submitted, unknown, failed, manual review, and total USDC volume.
- Seller-level recent settlements with clickable record IDs.
- Seller-level reconciliation risk summary.
- Last activity timestamps: last settlement update, newest API key, newest revoked key when present.
- UI sections for seller health, payment profiles, API keys, and recent money movement.
- Tests for seller-specific settlement aggregation and UI typing/proxy behavior.

Review agents:

- Backend/security: auth scope, no secret leakage, seller isolation, store query correctness.
- UI/UX: clickability, stale-state handling, density, keyboard/focus behavior.
- QA/SRE: tests cover Postgres path, operational usefulness, regression risk.

## Chunk 2: Reconciliation Queue

Goal: make stale/unknown/manual-review settlements an actionable work queue.

Status: implemented in this branch with review-agent findings fixed.

Deliver:

- Reconciliation queue API with filters by status, provider, network, seller, and age.
- Queue cards for unknown, submitted, stale in-progress, and manual review.
- Detail handoff from queue row to settlement detail.
- Empty, loading, and failed states.
- Tests for filtering, permissions, stale age handling, and no secret leakage.

Implemented contract:

- Backend route: `GET /ops/api/reconciliation`.
- Next proxy route: `GET /api/ops/reconciliation`.
- Query parameters: `status`, `minAgeSeconds`, `sellerRef`, `provider`, `network`, `limit`.
- Default statuses: `unknown`, `submitted`, `settle_in_progress`, `manual_review`.
- Permission: `ops:settlements`.
- Row handoff: reconciliation queue rows open the settlement detail panel by record ID.
- Backend responses include `Cache-Control: no-store`; queue counts are computed across the full filtered backlog, not only the visible page.
- Reconciliation status filters reject terminal settlement states; secret-shaped error reasons are redacted before serialization.

## Chunk 3: Global Search And Filters

Goal: operators can find records without guessing which page contains them.

Deliver:

- Search API over settlement record ID, transaction hash, gateway transfer ID, trace ID, seller ref, API key prefix, payer, and payTo.
- UI global search with keyboard focus and result categories.
- Settlement table filters by seller, status, provider, network, rail, date range, and amount range.
- Tests for exact-match behavior and permission boundaries.

## Chunk 4: Provider And Network Health

Goal: distinguish seller issues from rail/provider/network issues.

Deliver:

- Provider latency and error-rate summaries.
- Last successful settlement per provider/network.
- Gas runway and signer lock visibility for EVM providers.
- Gateway availability/balance visibility where applicable.
- Pause state and recent provider errors in one page.
- Tests for health serialization and UI rendering.

## Chunk 5: Alert Center

Goal: convert metrics into actionable operational incidents.

Deliver:

- Alert objects for signer gas, unknown/manual-review backlog, stale reconciliation, provider errors, auth failures, and duplicate claim spikes.
- Alert detail links to affected sellers/settlements/providers.
- Runbook links and acknowledgement state.
- Tests for threshold logic and alert lifecycle.

## Chunk 6: Exports And Reports

Goal: support finance/reconciliation workflows outside the dashboard.

Deliver:

- CSV export for settlements, seller activity, provider counts, and reconciliation queue.
- Daily seller settlement report.
- Operator audit event for exports.
- Tests for export filters, redaction, and permission checks.

## Chunk 7: Manual Review Workflow

Goal: safely move ambiguous records through a controlled operational process.

Deliver:

- Manual-review assignment and status transitions.
- Operator notes with audit events.
- Guardrails for terminal state changes.
- Tests for OpenFGA permission, audit, and invalid transition rejection.
