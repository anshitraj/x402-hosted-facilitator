# OmniClaw Facilitator Live Deployment Checklist

This checklist promotes the verified alpha stack to a small private deployment
using Docker Compose. The deployed facilitator must be tested through the same
HTTPS domains a seller will use.

## Goal

Deploy OmniClaw Facilitator so a trusted seller can use:

```text
https://<facilitator-domain>/v2/x402
Authorization: Bearer <seller-api-key>
```

The seller and buyer remain outside the facilitator runtime.

## Inputs

- Published facilitator image tag.
- Published ops-console image tag.
- Host with Docker and Docker Compose.
- TLS reverse proxy for facilitator and ops console domains.
- Private production env files based on `infra/compose/*.example`.
- Funded hosted EVM signer address for Arc testnet gas.
- Funded buyer/canary Gateway balance for the live canary.
- OIDC issuer/client and OpenFGA store/model.
- Postgres and Redis endpoints.

## Pre-Deploy Gates

1. `uv run pytest -q` passes.
2. Ops console typecheck, lint, unit tests, and Playwright pass.
3. `docker compose --env-file <compose-env> -f infra/compose/docker-compose.production.yml config --quiet` passes.
4. `python -m hosted_facilitator.hosted.production_env` passes with the real production env.
5. Prometheus rules validate.
6. No raw API keys, private keys, bearer tokens, or payment signatures are present in committed files.

## Deploy

1. Build and push images.
2. Install private env files on the host.
3. Start `infra/compose/docker-compose.production.yml`.
4. Confirm:

```bash
curl -fsS http://127.0.0.1:4022/healthz
curl -fsS http://127.0.0.1:4022/internal/readyz
curl -fsS http://127.0.0.1:9108/readyz
curl -fsS http://127.0.0.1:3001/healthz
curl -fsS http://127.0.0.1:9090/api/v1/alerts
```

5. Confirm the public TLS endpoints route to the same services.

## Live Canary

1. Sign in to the ops console through OIDC.
2. Create the first alpha seller and copy the one-time API key.
3. Confirm `/v2/x402/supported` over the deployed facilitator domain returns Exact EVM and Gateway for the seller key.
4. Start the external seller app with only the deployed facilitator URL, seller API key, network, asset, and `payTo`.
5. Run one buyer inspection against the seller URL and confirm both requirements are advertised.
6. Run one Gateway buyer payment against the public seller URL.
7. Run one Exact EVM buyer payment against an exact-only seller configuration.
8. Run one Gateway buyer payment against a Gateway-only seller configuration.
9. Verify new Postgres settlement records and attempts are `settled`, scoped to that API-key hash and payment profile.
10. Verify metrics show facilitator up, signer above minimum, Gateway canary above minimum, reconciler heartbeat current, and no firing alerts.
11. Pause/resume a provider in the ops console and verify audit updates.

Run Exact EVM and Gateway payment canaries serially when they use the same buyer
wallet. The buyer agent correctly locks the wallet during transaction submission,
so parallel canaries can produce a buyer-side `Wallet is busy` failure that is
not a facilitator settlement failure.

## Handoff

Send the seller only the values listed in `docs/hosted-facilitator-alpha-onboarding.md`.
Do not send internal IDs, route keys, project IDs, raw logs, private keys, bearer
tokens, or screenshots containing secrets.

## Rollback

1. Pause the affected provider/network in the ops console.
2. Stop external seller access if a raw API key is exposed.
3. Revoke and reissue the seller API key if any secret handling is uncertain.
4. Keep the reconciler running until submitted or unknown settlements reach terminal state.
