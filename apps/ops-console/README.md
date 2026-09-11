# OmniClaw Ops Console

Internal operator frontend for the hosted facilitator control plane.

This app is intentionally separate from the Python facilitator API process and from the future seller dashboard. It talks to the backend through local Next.js route handlers:

- `GET /api/ops/overview` -> backend `GET /ops/api/overview`
- `POST /api/ops/pauses` -> backend `POST /ops/api/pauses`
- `POST /api/ops/sellers` -> backend seller-account creation

The browser never receives an OmniClaw operations token. The Next.js server owns the OIDC callback, stores the access token in an encrypted HTTP-only session cookie, and forwards bearer authorization plus trace/correlation headers to the hosted facilitator backend.

## Configuration

```env
OMNICLAW_OPS_API_BASE_URL=http://127.0.0.1:8080
OMNICLAW_OPS_PUBLIC_ORIGIN=http://127.0.0.1:3001
OMNICLAW_OPS_FORWARD_COOKIE_NAMES=
OMNICLAW_OPS_PROXY_TIMEOUT_MS=5000
OMNICLAW_OPS_ALLOW_INSECURE_BACKEND=
OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT=
OMNICLAW_OPS_SESSION_SECRET=replace-with-32-byte-minimum-random-secret
OMNICLAW_OPS_OIDC_CLIENT_ID=omniclaw-ops-console
OMNICLAW_OPS_OIDC_ISSUER=https://idp.example.com/realms/omniclaw
OMNICLAW_OPS_OIDC_AUTHORIZATION_URL=https://idp.example.com/realms/omniclaw/protocol/openid-connect/auth
OMNICLAW_OPS_OIDC_TOKEN_URL=https://idp.example.com/realms/omniclaw/protocol/openid-connect/token
OMNICLAW_OPS_OIDC_REDIRECT_URI=https://ops.example.com/api/auth/callback
OMNICLAW_OPS_OIDC_LOGOUT_URL=https://idp.example.com/realms/omniclaw/protocol/openid-connect/logout
OMNICLAW_OPS_OIDC_TOKEN_TIMEOUT_MS=5000
```

In production `OMNICLAW_OPS_API_BASE_URL` must be HTTPS and should point at the private hosted facilitator API origin reachable by the ops-console server. Local development may use loopback HTTP.

`OMNICLAW_OPS_ALLOW_INSECURE_BACKEND=true` is only accepted for local Docker Compose when `OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT=local-compose`, where the Next.js server reaches the facilitator through an internal service name such as `http://hosted-facilitator:4022`. Do not set these values in a hosted deployment.

`OMNICLAW_OPS_PUBLIC_ORIGIN` pins the browser-facing console origin for same-origin mutation checks. If omitted, the app uses the request origin seen by Next.js.

`OMNICLAW_OPS_FORWARD_COOKIE_NAMES` optionally lists the session cookies forwarded to the backend, separated by commas. In production, unset means no cookies are forwarded; use bearer-token forwarding or set an explicit cookie allowlist.

`OMNICLAW_OPS_PROXY_TIMEOUT_MS` bounds backend proxy calls. Values outside `500` to `30000` milliseconds fall back to `5000`.

`OMNICLAW_OPS_SESSION_SECRET` encrypts the HTTP-only session cookie and must be a high-entropy secret in every non-local environment.

The `OMNICLAW_OPS_OIDC_*` values configure the operator sign-in flow. `OMNICLAW_OPS_OIDC_TOKEN_TIMEOUT_MS` bounds the server-side authorization-code token exchange.

Human authentication is OIDC, and backend authorization is OpenFGA. The unauthenticated UI renders only the access gate; operational metrics and controls are not loaded until a valid session exists. This frontend does not implement password auth, self-signup, or static admin secrets.

## Commands

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run test:e2e:ui
npm run build
npm run dev
```

The development server uses port `3001`.

End-to-end control-plane verification expects a running repository Compose stack. For an isolated stack that can run beside the default ports:

```bash
cd ../..
COMPOSE_PROJECT_NAME=omniclaw-hosted-e2e \
COMPOSE_FILES='docker-compose.yml:infra/compose/docker-compose.e2e-ports.yml' \
docker compose -p omniclaw-hosted-e2e \
  -f docker-compose.yml \
  -f infra/compose/docker-compose.e2e-ports.yml \
  up --build

cd apps/ops-console
OMNICLAW_OPS_E2E_BASE_URL=http://127.0.0.1:13001 npm run test:e2e -- --project=chromium
```

`npm run test:e2e:ui` runs the CI-safe browser interaction test with mocked
browser API responses. The Compose command above is the real-stack operator
flow and should be run before alpha deployment evidence is accepted.

## Local Docker Stack

From the repo root:

```bash
cp ./hosted.env.example hosted.env
docker compose -f docker-compose.yml up --build
```

The console is served at `http://127.0.0.1:3001` and proxies to the hosted facilitator service on the private Compose network.

The Compose stack includes Keycloak and OpenFGA. Use:

```text
ops@omniclaw.ai
omniclaw-alpha-admin
```

The Next.js server stores the OIDC access token in an encrypted, HTTP-only session cookie and forwards it to the hosted facilitator backend. The browser does not receive backend service credentials or static operations tokens.

For local HTTP, the session cookies intentionally omit the `__Host-` prefix because browsers require `Secure` for that prefix. HTTPS origins configured through `OMNICLAW_OPS_PUBLIC_ORIGIN` use host-prefixed secure cookies.
