# Hosted Facilitator Seller Domain Design

This document defines the target seller onboarding and policy model for the hosted OmniClaw x402 facilitator.

The goal is to keep seller integration simple while keeping money movement tightly scoped, auditable, and safe.

## Decision

The hosted facilitator uses this hierarchy:

```text
Seller account
  -> Payment profile
       -> API keys
```

The seller-facing integration remains:

```text
Facilitator URL
Seller API key
```

Sellers do not send seller account IDs, payment profile IDs, route keys, or project IDs in x402 requests.

## Why This Shape

A seller account is a business identity. It should not also be the complete payment policy because one seller can run multiple APIs, products, departments, environments, or revenue destinations.

A payment profile is the money policy boundary. It defines where payments may go and which rails may be used.

An API key is the credential for one seller integration. It is scoped to one payment profile. Key rotation is done by issuing another key for the same profile. A new app or destination gets a new profile and a new key.

This avoids overloading one account-wide allowlist while keeping the seller integration dead simple.

## Domain Objects

## Reference Glossary

The system uses three kinds of identifiers. They must not be blurred in UI or docs.

```text
Database id
  Internal primary key. Never sent to sellers and never used in protocol requests.

Operator ref
  Stable slug used by OmniClaw operators in the control plane, for example sellerRef
  or profileRef. It may appear in internal audit/support workflows, but it is not a
  credential and is not accepted by public x402 routes.

Seller-safe display name
  Human label that may be shared in support context, for example "Docs API".
```

The protocol credential is always the API key. A profile name/ref may help humans talk about an integration, but it is not part of `/supported`, `/verify`, or `/settle`.

### Seller Account

Represents the seller organization or business.

Fields:

```text
id
tenant_id
name
contact_email
environment
status
created_at
updated_at
```

`id` is internal. It is not sent by sellers in protocol calls.

Status values:

```text
active
disabled
manual_review
```

Runtime must treat every non-`active` seller status as deny-settlement.
Operational pauses are stored in control-plane pause state, not seller account status.

### Payment Profile

Represents one allowed payment configuration for a seller.

Fields:

```text
id
seller_account_id
name
status
enabled_providers_json
enabled_networks_json
enabled_schemes_json
allowed_assets_json
allowed_pay_to_json
rate_limits_json
created_at
updated_at
```

Status values:

```text
active
disabled
manual_review
```

Runtime must treat every non-`active` payment profile status as deny-settlement.
Operational provider/network/global pauses are stored in control-plane pause state, not payment profile status.

The profile owns:

- provider allowlist, for example `exact_evm`, `circle_gateway`.
- network allowlist, for example `eip155:5042002`.
- scheme allowlist, for example `exact`.
- asset allowlist.
- `payTo` allowlist.
- rate-limit policy.

### API Key

Represents one server-side credential for a seller integration.

Fields:

```text
id
key_hash
seller_account_id
payment_profile_id
key_prefix
name
status
created_at
revoked_at
last_used_at
```

`id` is an internal stable key identifier for operator actions. API-key revocation uses this internal key id, not the visible prefix. Prefixes are for support display only and must be globally unique or collision-checked.

Raw API keys are shown once. Only keyed lookup hash and prefix are stored.

Status values:

```text
active
revoked
expired
```

Runtime must treat every non-`active` API-key status as deny-settlement.

## API Key Storage

API keys must be generated with at least 256 bits of randomness.

The database lookup hash must be keyed:

```text
HMAC-SHA256(server_pepper, raw_api_key)
```

Requirements:

- the pepper lives in secret storage, not the database.
- comparison is constant-time.
- raw keys are never logged, traced, stored, or returned after issuance.
- key prefixes are long enough for support usefulness but not used as authentication.
- pepper rotation requires dual-read or planned key rotation.

## Runtime Resolution

Every protocol request resolves through the API key.

```text
Authorization: Bearer omck_...
        |
        v
HMAC-SHA256(server_pepper, api_key)
        |
        v
hosted_seller_api_keys
        |
        v
active seller account + active payment profile
        |
        v
RequestContext(seller_account, payment_profile)
```

Missing, revoked, expired, disabled, or mismatched records fail closed before provider routing.

## Public x402 Contract

Public routes stay protocol-only:

```text
GET  /v2/x402/supported
POST /v2/x402/verify
POST /v2/x402/settle
```

Required header:

```text
Authorization: Bearer <seller_api_key>
```

No seller account ID, payment profile ID, route key, or project ID is accepted in the URL, query string, or request body.

## `/supported`

The facilitator:

1. authenticates the API key.
2. loads the resolved payment profile.
3. asks enabled providers for supported payment requirements.
4. filters by profile policy.
5. returns only requirements the API key is allowed to use.

This means two API keys for the same seller can produce different `/supported` results.

## `/verify`

The facilitator:

1. authenticates the API key.
2. loads the resolved payment profile.
3. parses the x402 envelope.
4. selects provider from payment requirement.
5. checks scheme, network, provider, asset, and `payTo` against the profile.
6. calls provider verification.

Provider verification must not move money.

## `/settle`

The facilitator:

1. authenticates the API key.
2. loads the resolved payment profile.
3. parses the x402 envelope.
4. checks profile policy.
5. computes the payment fingerprint.
6. claims settlement atomically.
7. submits provider settlement only for a newly claimed record.
8. persists attempt and provider transaction.
9. returns terminal, submitted, unknown, or manual-review result.

## Settlement Scope

Settlement records are scoped to the resolved seller account and payment profile, but provider submission safety also needs a provider-level replay guard.

Profile-scoped settlement uniqueness:

```text
seller_account_id + payment_profile_id + fingerprint
```

This gives these properties:

- same payment retry on the same profile is idempotent.
- key rotation for a profile does not create duplicate settlements.
- profile reporting is isolated.
- settlement records can still be reported at seller-account level.

Provider-level replay uniqueness:

```text
provider + network + scheme + canonical_authorization_id
```

`canonical_authorization_id` is derived from the signed payment authorization, not from the API key or profile. If the provider does not expose a nonce/authorization id, derive a canonical authorization fingerprint from the buyer-signed payload fields that uniquely identify the spend.

This prevents the same signed payment authorization from being submitted through two different payment profiles.

Settlement records should store:

```text
seller_account_id
payment_profile_id
seller_api_key_prefix
provider
scheme
network
asset
pay_to
payer
amount
canonical_authorization_id
fingerprint
fingerprint_version
status
transaction_hash or gateway_transfer_id
trace_id
raw_requirements_json
created_at
updated_at
```

## Database Target

Target tables:

```text
hosted_tenants
hosted_seller_accounts
hosted_seller_payment_profiles
hosted_seller_api_keys
settlement_records
settlement_attempts
control_plane_audit_events
control_plane_pause_targets
```

Key constraints:

```text
hosted_seller_payment_profiles.seller_account_id
  -> hosted_seller_accounts.id

hosted_seller_api_keys.seller_account_id
  -> hosted_seller_accounts.id

hosted_seller_api_keys.payment_profile_id
  -> hosted_seller_payment_profiles.id

hosted_seller_payment_profiles UNIQUE(seller_account_id, id)

hosted_seller_api_keys(seller_account_id, payment_profile_id)
  -> hosted_seller_payment_profiles(seller_account_id, id)

settlement_records UNIQUE(seller_account_id, payment_profile_id, fingerprint)

settlement_records UNIQUE(provider, network, scheme, canonical_authorization_id)
```

Health checks must validate every table, index, primary key, foreign key, and unique settlement constraint.

The composite API-key/profile foreign key is mandatory. It makes it impossible to bind Seller A's API key to Seller B's profile.

Status columns must have DB `CHECK` constraints:

```text
seller.status in active, disabled, manual_review
payment_profile.status in active, disabled, manual_review
api_key.status in active, revoked, expired
```

## Money Destination Approval

`allowedPayTo` creation or update is a high-risk money-policy change.

Requirements:

- validate address format for the selected network.
- require seller attestation or proof-of-control before production activation.
- require step-up or two-person approval for production `payTo` changes.
- audit before and after values.
- keep new or changed destinations in `manual_review` until approved.
- fail closed: settlement is denied while seller or profile status is not `active`.

## Control Plane API

There are two route surfaces:

```text
Browser/Next console routes: /api/ops/*
FastAPI backend routes:      /ops/api/*
```

The browser never calls FastAPI directly in production. The Next proxy maps fixed `/api/ops/*` routes to fixed `/ops/api/*` backend routes and injects server-side session authorization.

FastAPI operator routes should be explicit and small:

```text
POST /ops/api/sellers
GET  /ops/api/sellers
GET  /ops/api/sellers/{sellerRef}

POST /ops/api/sellers/{sellerRef}/api-keys
POST /ops/api/sellers/{sellerRef}/api-keys/{apiKeyId}/revoke
```

Matching browser routes:

```text
POST /api/ops/sellers
GET  /api/ops/sellers
GET  /api/ops/sellers/{sellerRef}

POST /api/ops/sellers/{sellerRef}/api-keys
POST /api/ops/sellers/{sellerRef}/api-keys/{apiKeyId}/revoke
```

Current alpha combines seller, default payment profile, initial API key, and
seller audit creation into one atomic endpoint:

```text
POST /ops/api/sellers
```

Payload:

```json
{
  "sellerRef": "corey-labs",
  "tenantRef": "alpha",
  "name": "Corey Labs",
  "environment": "testnet",
  "enabledNetworks": ["eip155:5042002"],
  "allowedAssets": ["0x3600000000000000000000000000000000000000"],
  "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
}
```

Response:

```json
{
  "seller": {
    "sellerRef": "corey-labs",
    "name": "Corey Labs",
    "status": "active",
    "paymentProfiles": [
      {
        "paymentProfileId": "default",
        "status": "active",
        "enabledProviders": ["exact_evm", "circle_gateway"],
        "enabledNetworks": ["eip155:5042002"],
        "allowedAssets": ["0x3600000000000000000000000000000000000000"],
        "allowedPayTo": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]
      }
    ]
  },
  "apiKey": "omck_alpha_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "apiKeyPrefix": "omck_alpha_ab12",
  "facilitatorPath": "/v2/x402",
  "auditEvent": {
    "action": "seller_create"
  }
}
```

The raw API key appears once as `apiKey`. It must never be returned by
list/read endpoints. The key prefix is not a credential.

## Control Plane UX

The operator console should have these flows:

1. Create seller account.
2. Review the default payment profile created for that seller.
3. Issue or revoke API keys for that profile.
4. Review money-policy summary before creation.
5. Run `/supported` preflight for the issued key.
6. Generate seller-safe handoff.
7. Copy one-time API key through a distinct "Copy one-time API key" control.
8. Copy support prefix through a separate "Copy support prefix" control.
9. View profile policy and key prefixes.
10. Revoke key.
11. Disable profile. Future control-plane flow, not current alpha UI.
12. Disable seller account. Future control-plane flow, not current alpha UI.

The final handoff screen must block or warn when providers are unhealthy, active pauses apply, or `/supported` returns no requirements for the issued key.

The console should make the hierarchy visually clear:

```text
Seller
  Profile
    API keys
```

It should not imply that an API key is account-wide unless it is explicitly issued for an account-wide profile.

## Seller Handoff

Seller receives:

```text
Facilitator origin
Full x402 endpoint
API key
Approved network
Approved asset
Approved payTo
Approved rails
```

The seller does not receive internal IDs as credentials. The profile name may be shared for human support, but it is not used in protocol requests.

Secret delivery:

- raw API keys must be delivered through an encrypted secret link, password-manager share, or live call.
- email/DM may contain non-secret handoff values only.
- if a raw key is exposed in a ticket, chat, log, or screenshot, revoke it and issue a replacement.

Terminology for Gateway handoff:

```text
Scheme: exact
Rail: Circle Gateway
x402 extra.name: GatewayWalletBatched
Internal provider: circle_gateway
```

`GatewayWalletBatched` is not a scheme. It is Gateway metadata in `extra.name`.

Seller library examples must distinguish:

```text
Facilitator origin: https://<hosted-facilitator-domain>
Full x402 endpoint: https://<hosted-facilitator-domain>/v2/x402
```

Operators must not improvise which one a seller library expects; each supported library gets an approved example.

## Multiple Apps And Destinations

Use cases:

### Same App, Key Rotation

```text
Seller: corey-labs
Profile: docs-api
Keys: docs-api-2026-05, docs-api-2026-06
```

Both keys map to the same profile. Profile-scoped settlement idempotency is shared, and provider-level replay protection also blocks duplicate provider submission.

### Different Apps, Same Destination

```text
Seller: corey-labs
Profile: docs-api
Profile: agents-api
```

Profiles may share the same `payTo`, asset, and network but have different rate limits or providers.

The same buyer-signed authorization must still not be settleable twice across profiles. Provider-level replay uniqueness enforces that.

### Different Apps, Different Destinations

```text
Seller: corey-labs
Profile: docs-api -> 0xWalletA
Profile: agents-api -> 0xWalletB
```

Each app receives a different API key. A compromised docs API key cannot settle to the agents wallet unless that wallet is also allowed on the docs profile.

## Audit

Audit actions:

```text
seller_created
seller_disabled
payment_profile_created
payment_profile_updated
payment_profile_disabled
api_key_issued
api_key_revoked
pause_set
```

Audit targets:

```text
seller
payment_profile
api_key
provider
network
global
```

Audit reasons may include key prefixes and profile refs, never raw API keys.

Money-policy audit events must include before/after values for provider, network, asset, `payTo`, status, and approval state. Raw API keys are never included.

## Rate Limits

Rate limits are evaluated in this order:

1. client IP invalid-request bucket.
2. payment profile endpoint bucket.
3. payment profile aggregate bucket.
4. seller account aggregate bucket if configured.

Redis keys should use `seller_account` and `payment_profile`, never old project terminology.

## Observability

Structured logs and traces may include:

```text
seller_account_id
payment_profile_id
api_key_prefix
provider
network
status
trace_id
fingerprint_hash
canonical_authorization_id_hash
```

Metrics labels should avoid raw wallet addresses and unbounded profile names. Prefer provider, network, status, and environment. Seller/profile identifiers can be used in logs and traces, not high-cardinality metrics.

## Migration From Current Alpha Schema

Current alpha stores policy on `hosted_seller_accounts`.

Target migration:

1. Create `hosted_seller_payment_profiles`.
2. For each existing seller account, create a default profile.
3. Move existing enabled providers, networks, schemes, assets, payTo, and rate limits into that default profile.
4. Add `payment_profile_id` to `hosted_seller_api_keys`.
5. Backfill existing keys to the default profile.
6. Add `payment_profile_id` to `settlement_records`.
7. Backfill existing settlement records from the API key prefix when available, otherwise use seller default profile.
8. Add canonical parsed money fields and canonical authorization id.
9. Set `payment_profile_id` and canonical money fields `NOT NULL` after backfill validation.
10. Add the new unique constraint on `(seller_account_id, payment_profile_id, fingerprint)`.
11. Add provider-level replay uniqueness on `(provider, network, scheme, canonical_authorization_id)`.
12. Update claim/get SQL to include `payment_profile_id`.
13. Drop the old `(seller_account_id, fingerprint)` unique constraint.
14. Update runtime resolver to return seller account plus payment profile.
15. Remove account-level policy reads from runtime after backfill is proven.

Because this repository is still pre-alpha/private, we can implement the target schema directly if no external production data must be preserved.

## Implementation Chunks

Chunk 1: domain and docs.

- finalize this design.
- update architecture, end-to-end spec, onboarding, and control-plane design.
- reviewer signoff.

Chunk 2: backend domain/schema.

- add payment profile config type.
- update request context.
- update Postgres schema and health checks.
- make API-key lookup join profile.
- update static resolver.
- tests for auth, policy, ownership constraints, non-active states, and settlement scope.

Chunk 3: control-plane API.

- update create seller endpoint payload/response.
- add profile and key issuance endpoints or implement the atomic create endpoint first.
- tests for duplicate refs, one-time key response, policy persistence, and audit.

Chunk 4: provider and settlement flow.

- make `/supported`, `/verify`, and `/settle` enforce profile policy.
- update settlement uniqueness.
- add provider-level replay uniqueness.
- update reconciliation queries and metrics.
- duplicate-settle tests across rotated keys and profiles.
- migration tests proving same seller + same profile + same fingerprint is blocked.
- migration tests proving same seller + different profiles + distinct canonical authorizations can coexist.
- replay tests proving same canonical authorization across profiles cannot submit twice.

## Duplicate `/settle` Contract

Duplicate requests must never submit a second provider settlement.

Same profile and same fingerprint:

```text
terminal record -> return cached terminal result with duplicate=true
submitted/unknown/manual_review -> return current state with duplicate=true and retry guidance
in_progress -> return duplicate=true, settlementStatus=submitted, retryAfterSeconds set
```

The API should use protocol-compatible response bodies. If an HTTP status distinction is needed for an in-flight duplicate, use `202 Accepted`; do not use a 5xx because the seller should retry rather than treat it as an infrastructure failure.

Same canonical authorization across different profiles:

```text
provider replay guard hit -> do not submit provider settlement
```

The response should return a deterministic failure or manual-review outcome with audit context, depending on provider semantics. It must not silently report a successful settlement for the wrong profile.

Chunk 5: ops console.

- update seller form to include profile.
- add profile/key visual hierarchy.
- remove any account-wide API key implication.
- Playwright/manual UI checks.

Chunk 6: canary and readiness.

- update scripts to create/use one profile-scoped API key.
- run exact canary.
- run Gateway canary.
- verify ops console and audit updates.

## Design Invariants

- One API key resolves to exactly one active payment profile.
- One payment profile belongs to exactly one seller account.
- API-key/profile/seller mismatches are impossible at the database constraint level.
- x402 protocol routes never accept public seller/profile identifiers.
- Provider settlement is never attempted before profile policy passes.
- Duplicate settlement is impossible within the same profile and fingerprint.
- Duplicate provider submission is impossible for the same canonical payment authorization, even across profiles.
- Key rotation for a profile does not reset idempotency.
- Different profiles can have different `payTo` without sharing credentials.
- Raw API keys are stored nowhere and returned once.
- Every operator write is OIDC/OpenFGA authorized and audited.
