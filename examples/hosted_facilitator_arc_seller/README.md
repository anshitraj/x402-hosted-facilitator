# Hosted Facilitator Arc Seller Example

External seller app for the hosted facilitator.

This app is a local canary harness and is not included in the facilitator production image. It does not import the hosted facilitator package and does not share runtime code with the facilitator. It only uses:

- hosted facilitator URL
- hosted facilitator seller API key
- official `x402` Python SDK

## Run

Start the hosted facilitator stack first:

```bash
cp ./hosted.env.example hosted.env
# edit hosted.env with the hosted facilitator signer private key and provider config
docker compose -f docker-compose.yml up -d --build
```

Then start this seller app:

```bash
export X402_FACILITATOR_URL="http://127.0.0.1:4022"
export X402_FACILITATOR_API_KEY="omck_seller_scoped_from_control_plane"
export X402_SELLER_PAY_TO="0xYourSellerAddress"
export X402_PRICE="$0.001"
export X402_ACCEPT_MODE="all" # all, exact, or gateway

uv run uvicorn examples.hosted_facilitator_arc_seller.app:app --host 127.0.0.1 --port 4023
```

Paid route:

```text
GET http://127.0.0.1:4023/compute
```

Debug routes:

```text
GET http://127.0.0.1:4023/health
GET http://127.0.0.1:4023/rails
```

What the seller needs:

- `x402[fastapi,evm]`
- `HTTPFacilitatorClient` pointed at the hosted facilitator URL
- server-side auth headers for `supported`, `verify`, and `settle`
- `x402ResourceServer`
- `ExactEvmServerScheme`
- route config with `PaymentOption` entries
- a paid handler that returns the product after middleware settles payment

Buyer payment stays outside this example. Use OmniClaw core or `omniclaw-cli` to inspect and pay the seller URL.

Create the seller and copy its scoped API key from the OmniClaw control plane before
starting this example.
