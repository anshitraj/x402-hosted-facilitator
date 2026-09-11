# Hosted Facilitator Buyer Canary

Private buyer canary runner for the hosted facilitator seller example.

This does not run inside the facilitator image. It is an external canary used to
prove the seller and facilitator boundary from a buyer's perspective.

It can pay the external seller example through:

- Circle Gateway batch settlement, using the local copy of OmniClaw's buyer-side nanopayment signer
- standard x402 exact EVM, using the official x402 Python SDK

## Run

Start the hosted facilitator and the external seller first.

```bash
cp ./hosted.env.example hosted.env
# edit hosted.env with the hosted facilitator signer private key and provider config
# set OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED=true for Gateway payment tests
docker compose -f docker-compose.yml up -d --build

export X402_FACILITATOR_URL="http://127.0.0.1:4022"
export X402_FACILITATOR_API_KEY="omck_seller_scoped_from_control_plane"
export X402_SELLER_PAY_TO="0xYourSellerAddress"
export X402_ACCEPT_MODE="all" # all, gateway, or exact
uv run uvicorn examples.hosted_facilitator_arc_seller.app:app --host 127.0.0.1 --port 4023
```

Then pay:

Gateway mode requires `OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED=true`,
`OMNICLAW_HOSTED_CIRCLE_GATEWAY_NETWORKS=eip155:5042002`, and a buyer address
with funded Circle Gateway balance on Arc.

```bash
uv run python examples/hosted_facilitator_buyer/pay_seller.py \
  --env-file .env \
  --mode gateway \
  --url http://127.0.0.1:4023/compute
```

`--mode gateway` and `--mode exact` are QA controls for rail isolation. The normal
buyer path must use OmniClaw core or `omniclaw-cli`; these flags exist so tests can prove
Gateway and exact settlement independently when a seller advertises both. Before external
alpha handoff, the same smoke must be repeated from the public buyer/core path instead of
this private canary harness.

```bash
uv run python examples/hosted_facilitator_buyer/pay_seller.py \
  --env-file .env \
  --mode exact \
  --url http://127.0.0.1:4023/compute
```

## Smoke Test

After Compose is up and the buyer has Arc testnet funds, run both rails:

```bash
X402_SELLER_PAY_TO="0xYourSellerAddress" \
BUYER_ENV_FILE=.env \
bash scripts/hosted_facilitator_smoke.sh
```

To test the external HTTPS shape, use Cloudflare Tunnel. This exposes both the
facilitator and seller over temporary HTTPS, starts the seller with the public
facilitator URL plus seller API key, and then pays the public seller URL through
both exact and Gateway rails.

```bash
X402_FACILITATOR_API_KEY="omck_short_lived_alpha_key" \
X402_SELLER_PAY_TO="0xYourSellerAddress" \
BUYER_ENV_FILE=.env \
bash scripts/hosted_facilitator_tunnel_smoke.sh
```

The tunnel smoke may enable `OMNICLAW_EXAMPLE_TRYCLOUDFLARE_DOH=true` for the
example harness when the local resolver cannot resolve quick `trycloudflare.com`
hostnames. That resolver is test-only and is not used by the hosted facilitator.
The tunnel smoke also requires `X402_FACILITATOR_API_KEY`, buyer EOA credentials,
and funded buyer Gateway balance.
