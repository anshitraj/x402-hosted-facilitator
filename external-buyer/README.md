# External Buyer

This directory is a local test harness for a buyer running outside the hosted
facilitator repo. It uses the OmniClaw core agent/CLI from a sibling checkout
and pays a seller URL through the normal x402 flow.

Tracked files contain no secrets. Copy runtime credentials into `buyer.env`
from the core repo `.env`; `buyer.env` is ignored by git.

## Files

- `buyer.env.example` documents the required runtime values.
- `buyer.policy.json` scopes the buyer token to localhost test sellers and a
  small per-transaction limit.
- `run-agent.sh` starts the OmniClaw core agent server.
- `inspect.sh` asks the buyer agent which x402 route it would use.
- `pay.sh` executes the x402 payment through the buyer agent.

## Run

Start the hosted facilitator stack and the external seller first, then:

```bash
bash external-buyer/run-agent.sh
```

In another shell:

```bash
bash external-buyer/inspect.sh
bash external-buyer/pay.sh
```

The default target is `http://127.0.0.1:4023/compute`. Override it with
`BUYER_TARGET_URL` when running the command:

```bash
BUYER_TARGET_URL=http://127.0.0.1:4024/compute bash external-buyer/inspect.sh
BUYER_TARGET_URL=http://127.0.0.1:4024/compute bash external-buyer/pay.sh
```

The command-line override wins over the value stored in `buyer.env`, which lets
the same buyer env test exact-only, Gateway-only, and both-rail seller instances.
