# OmniClaw Facilitator Alpha Seller Onboarding

This is the controlled onboarding process for private OmniClaw Facilitator alpha
sellers.

## Alpha Scope

- Rails: EVM exact and Circle Gateway batched settlement
- First network: Arc Testnet, `eip155:5042002`
- Facilitator path: `/v2/x402`
- Auth: seller API key
- Seller dashboard: not live yet. OmniClaw operators create seller records through the internal ops console.

## Operator Intake

Collect these fields from the seller before creating the seller record:

| Field | Required | Notes |
| --- | --- | --- |
| Seller name | yes | Human-readable seller name |
| Network | yes | Select `Arc Testnet` in the console for first alpha |
| Seller `payTo` address | yes | EVM address receiving the settled USDC |

Keep support contact details in the private operator tracker for now. The alpha
ops console does not yet store contact email, key labels, or separate profile
names.

## Create Seller Account

1. Sign in to the OmniClaw ops console.
2. Open `Seller Onboarding`.
3. Enter seller name.
4. Select `Arc Testnet`.
5. Enter seller `payTo`.
6. Click `Create seller`.
7. Copy the raw API key immediately. It is shown once.
8. Confirm the seller appears in `Manage Sellers` with one active API key.
9. Confirm the audit trail shows `Seller Create`.
10. Run a non-money `/supported` check with the API key before sending credentials.

## Seller Handoff

Send the seller only this minimum handoff:

```text
OmniClaw Facilitator private alpha

Facilitator origin: https://<hosted-facilitator-domain>
Full x402 endpoint: https://<hosted-facilitator-domain>/v2/x402
One-time API key: <omck_alpha_... shown once>

Use the API key only server-side. Do not put it in a browser bundle, public repo, or client-side x402 buyer code. Use the approved seller-library example for whether the library expects the origin or the full x402 endpoint.
```

Deliver the one-time API key through an encrypted secret link, password-manager share, or live call. Email/DM may contain only non-secret handoff values.

## Approved Payment Policy

Share the approved payment policy separately from the credential handoff:

```text
Network: Arc Testnet
Scheme: exact
Rails: EVM exact, Circle Gateway
x402 Gateway metadata: extra.name=GatewayWalletBatched
PayTo: <seller payTo address>
```

Seller integration must stay outside the facilitator runtime. Seller apps use
official x402 middleware or seller-owned server code. This repository ships no
seller runtime. API keys stay in server-side authorization headers.

The API key is the seller credential. The seller does not send any separate ID; OmniClaw resolves the seller account and payment profile server-side from the API key hash and uses those resolved records for policy, settlement, reconciliation, and audit tracking.

In the current alpha console, each created seller has one default payment
profile and API keys are issued against that profile. If a seller needs a
different `payTo` or network policy during alpha, create a separate seller entry
for that policy. Key rotation for the same policy should use `Issue key` on the
existing seller and `Revoke` the old key with an operator reason.

Gateway is advertised through x402 `extra.name=GatewayWalletBatched` when the resolved payment profile enables the Circle Gateway rail. `GatewayWalletBatched` is not a scheme. Sellers choose which advertised payment requirements to accept in their own x402 middleware; the hosted facilitator verifies and settles the selected requirement.

For the external seller example, set:

```text
X402_FACILITATOR_URL=http://127.0.0.1:4022
X402_FACILITATOR_API_KEY=<seller API key>
X402_SELLER_PAY_TO=<seller payTo address>
```

For a deployed alpha, replace the example origin with the public facilitator
origin and use HTTPS. The seller app should not receive any Postgres, Redis,
OIDC, OpenFGA, signer, or Circle configuration.

## Preflight Before First Paid Request

1. Ops console provider health is `ok`.
2. Signer balance is above the configured gas floor on Arc Testnet.
3. Redis rate limiter is healthy.
4. Postgres settlement store, seller resolver, and control-plane state are healthy.
5. OTEL collector health is healthy and traces/metrics are flowing.
6. No global, `exact_evm`, or `eip155:5042002` pause is active.
7. Reconciler heartbeat is current.

## First Payment Canary

1. Start the seller resource server with the OmniClaw Facilitator URL and seller API key.
2. Run one buyer inspection and confirm the seller advertises Arc exact and Gateway batched requirements.
3. Run one Gateway buyer payment with a funded Gateway balance.
4. Run one Exact EVM buyer payment with a funded Arc Testnet buyer key.
5. Confirm seller returns the paid resource for both rails.
6. Confirm ops console settlement counts increase.
7. Confirm both records reach `settled`; if either reaches `unknown`, let the reconciler resolve it.
8. Retry the same buyer request once and confirm idempotency prevents a second provider settlement.
9. Repeat the smoke from the public OmniClaw buyer/core path before external alpha access; the repository-only buyer script is only a private canary harness.

Run the Exact EVM and Gateway payment canaries serially when they use the same
buyer wallet. The buyer wallet lock is expected to reject concurrent transaction
submission.

## Incident Rules

- Pause the network before debugging any ambiguous settlement failure.
- Never manually resubmit a live payment payload outside the facilitator idempotency path.
- Share only key prefixes, redacted fingerprints, transaction hashes, and network in incident notes.
- Raw API keys and private keys do not belong in tickets, logs, traces, screenshots, or DMs.
- If a raw API key is exposed in chat, email, a ticket, logs, traces, or screenshots, revoke it and issue a replacement immediately.
