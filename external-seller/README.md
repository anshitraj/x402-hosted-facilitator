# External Seller Harness

This directory is intentionally separate from the hosted facilitator runtime. It
represents an outside seller app using only:

- hosted facilitator x402 endpoint
- seller API key
- seller `payTo` address
- selected network

The app does not import hosted facilitator internals, does not share
`hosted.env`, and does not use buyer secrets.

## Run

Create a private seller env file:

```bash
cp external-seller/seller.env.example external-seller/seller.env
```

Fill `external-seller/seller.env` with the API key issued by the control plane
and the seller `payTo` address. Then run:

```bash
bash external-seller/run.sh
```

Inspect the seller rails:

```bash
curl -fsS http://127.0.0.1:4023/rails | jq
```

The paid resource is:

```text
GET http://127.0.0.1:4023/compute
```

Use the separate buyer harness or OmniClaw core buyer flow to pay that endpoint.
