import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { NextRequest } from "next/server";

import { GET as getOverview } from "../app/api/ops/overview/route";
import { POST as postPauses } from "../app/api/ops/pauses/route";
import { GET as getReconciliation } from "../app/api/ops/reconciliation/route";
import { POST as postReconciliationClaim } from "../app/api/ops/reconciliation/[recordId]/claim/route";
import { POST as postReconciliationManualReview } from "../app/api/ops/reconciliation/[recordId]/manual-review/route";
import { POST as postReconciliationRelease } from "../app/api/ops/reconciliation/[recordId]/release/route";
import { GET as getSettlement } from "../app/api/ops/settlements/[recordId]/route";
import { POST as postRevokeSellerApiKey } from "../app/api/ops/sellers/[sellerRef]/api-keys/[keyId]/revoke/route";
import { POST as postSellerApiKeys } from "../app/api/ops/sellers/[sellerRef]/api-keys/route";
import { GET as getSeller } from "../app/api/ops/sellers/[sellerRef]/route";
import { GET as getSellers, POST as postSellers } from "../app/api/ops/sellers/route";
import { encryptedSessionCookieForTest } from "../lib/ops-session";
import {
  filteredCookieHeader,
  isSameOriginMutation,
  proxyOpsRequest,
  proxyTimeoutMs,
  validateOpsApiBaseUrl
} from "../lib/ops-proxy";

const originalEnv = { ...process.env };
const originalFetch = globalThis.fetch;

afterEach(() => {
  process.env = { ...originalEnv };
  globalThis.fetch = originalFetch;
});

test("validates backend base URL before forwarding credentials", () => {
  assert.deepEqual(validateOpsApiBaseUrl(undefined, "production"), {
    ok: false,
    detail: "OMNICLAW_OPS_API_BASE_URL is not configured"
  });
  assert.equal(validateOpsApiBaseUrl("https://user:pass@example.test", "production").ok, false);
  assert.equal(validateOpsApiBaseUrl("http://api.example.test", "production").ok, false);
  assert.equal(validateOpsApiBaseUrl("http://127.0.0.1:8080", "development").ok, true);
  assert.equal(
    validateOpsApiBaseUrl("http://hosted-facilitator:4022", "development", "true").ok,
    true
  );
  assert.equal(
    validateOpsApiBaseUrl("http://hosted-facilitator:4022", "production", "true").ok,
    false
  );
  assert.equal(
    validateOpsApiBaseUrl(
      "http://hosted-facilitator:4022",
      "production",
      "true",
      "local-compose"
    ).ok,
    true
  );
  assert.equal(validateOpsApiBaseUrl("https://api.example.test", "production").ok, true);
});

test("same-origin mutation check compares full origin", () => {
  assert.equal(isSameOriginMutation("GET", null, "https://ops.example.test"), true);
  assert.equal(
    isSameOriginMutation("POST", "https://ops.example.test", "https://ops.example.test"),
    true
  );
  assert.equal(
    isSameOriginMutation("POST", "http://ops.example.test", "https://ops.example.test"),
    false
  );
  assert.equal(
    isSameOriginMutation("POST", "https://evil.example.test", "https://ops.example.test"),
    false
  );
  assert.equal(isSameOriginMutation("POST", null, "https://ops.example.test"), false);
  assert.equal(isSameOriginMutation("POST", "not a url", "https://ops.example.test"), false);
});

test("bounds proxy timeout configuration", () => {
  assert.equal(proxyTimeoutMs(undefined), 5_000);
  assert.equal(proxyTimeoutMs("1500"), 1_500);
  assert.equal(proxyTimeoutMs("100"), 5_000);
  assert.equal(proxyTimeoutMs("60000"), 5_000);
  assert.equal(proxyTimeoutMs("not-number"), 5_000);
});

test("filters cookies when a cookie allowlist is configured", () => {
  assert.equal(
    filteredCookieHeader("sid=one; theme=dark; csrf=two", undefined, "development"),
    "sid=one; theme=dark; csrf=two"
  );
  assert.equal(filteredCookieHeader("sid=one; theme=dark; csrf=two", undefined, "production"), null);
  assert.equal(filteredCookieHeader("sid=one; theme=dark; csrf=two", "sid,csrf"), "sid=one; csrf=two");
  assert.equal(filteredCookieHeader("sid=one; theme=dark", "missing"), null);
  assert.equal(filteredCookieHeader("sid=one", ""), "sid=one");
});

test("proxy returns stable JSON 503 when upstream fetch fails", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  globalThis.fetch = async () => {
    throw new Error("network down");
  };

  const response = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/pauses", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ paused: true })
    }),
    "/ops/api/pauses"
  );

  assert.equal(response.status, 503);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), { detail: "Operations backend is unavailable" });
});

test("proxy no-stores early error responses", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return new Response("{}");
  };

  const unknown = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/unknown", { method: "GET" }),
    "/ops/api/unknown"
  );
  const badOrigin = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/pauses", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://evil.example.test"
      },
      body: JSON.stringify({ paused: true })
    }),
    "/ops/api/pauses"
  );

  assert.equal(unknown.status, 404);
  assert.equal(unknown.headers.get("cache-control"), "no-store");
  assert.equal(badOrigin.status, 403);
  assert.equal(badOrigin.headers.get("cache-control"), "no-store");
  assert.equal(fetchCalled, false);
});

test("proxy maps only fixed ops route, forwards intended headers, and preserves JSON status", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test/base/";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  process.env.OMNICLAW_OPS_FORWARD_COOKIE_NAMES = "sid";
  let capturedUrl: string | undefined;
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(JSON.stringify({ detail: "Forbidden" }), {
      status: 403,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/pauses", {
      method: "POST",
      headers: {
        authorization: "Bearer token",
        cookie: "sid=one; theme=dark",
        "content-type": "application/json",
        origin: "https://ops.example.test",
        traceparent: "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        tracestate: "secret=value",
        "x-request-id": "req-1"
      },
      body: JSON.stringify({ paused: true })
    }),
    "/ops/api/pauses"
  );

  assert.equal(response.status, 403);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.deepEqual(await response.json(), { detail: "Forbidden" });
  assert.equal(capturedUrl, "https://api.example.test/ops/api/pauses");
  assert.equal(capturedInit?.method, "POST");
  assert.equal(capturedInit?.body, JSON.stringify({ paused: true }));
  assert.equal(capturedInit?.signal instanceof AbortSignal, true);
  const headers = capturedInit?.headers as Headers;
  assert.equal(headers.get("authorization"), null);
  assert.equal(headers.get("cookie"), "sid=one");
  assert.equal(headers.get("content-type"), "application/json");
  assert.equal(headers.get("traceparent"), "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01");
  assert.equal(headers.get("tracestate"), null);
  assert.equal(headers.get("x-request-id"), "req-1");
});

test("overview route handler maps to backend overview path without a request body", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  let capturedUrl: string | undefined;
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (input, init) => {
    capturedUrl = String(input);
    capturedInit = init;
    return new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await getOverview(
    new NextRequest("https://ops.example.test/api/ops/overview", {
      method: "GET",
      headers: {
        authorization: "Bearer token",
        cookie: "sid=one"
      }
    })
  );

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: "ok" });
  assert.equal(capturedUrl, "https://api.example.test/ops/api/overview");
  assert.equal(capturedInit?.method, "GET");
  assert.equal(capturedInit?.body, undefined);
});

test("proxy injects bearer token from server-side session cookie", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  process.env.OMNICLAW_OPS_SESSION_SECRET = "test-session-secret";
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => {
    capturedInit = init;
    return new Response(JSON.stringify({ status: "ok" }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };
  const session = encryptedSessionCookieForTest({
    accessToken: "session-token",
    expiresAt: Math.floor(Date.now() / 1000) + 300,
    subject: "ops-alpha-admin"
  });

  const response = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/overview", {
      method: "GET",
      headers: {
        cookie: `__Host-omniclaw_ops_session=${session}`
      }
    }),
    "/ops/api/overview"
  );

  assert.equal(response.status, 200);
  const headers = capturedInit?.headers as Headers;
  assert.equal(headers.get("authorization"), "Bearer session-token");
});

test("pauses route handler maps only to backend pauses path", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  let capturedUrl: string | undefined;
  globalThis.fetch = async (input) => {
    capturedUrl = String(input);
    return new Response(JSON.stringify({ event: {}, pauseState: {} }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await postPauses(
    new NextRequest("https://ops.example.test/api/ops/pauses", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ paused: true })
    })
  );

  assert.equal(response.status, 200);
  assert.equal(capturedUrl, "https://api.example.test/ops/api/pauses");
});

test("sellers route handlers map only to backend seller paths", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  const capturedUrls: string[] = [];
  const capturedMethods: string[] = [];
  globalThis.fetch = async (input, init) => {
    capturedUrls.push(String(input));
    capturedMethods.push(init?.method ?? "GET");
    return new Response(JSON.stringify({ seller: {}, apiKey: "omck_test" }), {
      status: 201,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await postSellers(
    new NextRequest("https://ops.example.test/api/ops/sellers", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ sellerRef: "alpha-seller", allowedPayTo: ["0x0000000000000000000000000000000000000001"] })
    })
  );

  assert.equal(response.status, 201);
  assert.equal(capturedUrls.at(-1), "https://api.example.test/ops/api/sellers");
  assert.equal(capturedMethods.at(-1), "POST");
});

test("seller detail and API key route handlers preserve seller scoping", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  const capturedUrls: string[] = [];
  const capturedMethods: string[] = [];
  globalThis.fetch = async (input, init) => {
    capturedUrls.push(String(input));
    capturedMethods.push(init?.method ?? "GET");
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  await getSellers(new NextRequest("https://ops.example.test/api/ops/sellers", { method: "GET" }));
  await getSeller(new NextRequest("https://ops.example.test/api/ops/sellers/alpha-seller", { method: "GET" }), {
    params: Promise.resolve({ sellerRef: "alpha-seller" })
  });
  await postSellerApiKeys(
    new NextRequest("https://ops.example.test/api/ops/sellers/alpha-seller/api-keys", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ paymentProfileId: "default" })
    }),
    { params: Promise.resolve({ sellerRef: "alpha-seller" }) }
  );
  await postRevokeSellerApiKey(
    new NextRequest("https://ops.example.test/api/ops/sellers/alpha-seller/api-keys/key-1/revoke", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ reason: "operator requested key rotation" })
    }),
    { params: Promise.resolve({ sellerRef: "alpha-seller", keyId: "key-1" }) }
  );

  assert.deepEqual(capturedUrls, [
    "https://api.example.test/ops/api/sellers",
    "https://api.example.test/ops/api/sellers/alpha-seller",
    "https://api.example.test/ops/api/sellers/alpha-seller/api-keys",
    "https://api.example.test/ops/api/sellers/alpha-seller/api-keys/key-1/revoke"
  ]);
  assert.deepEqual(capturedMethods, ["GET", "GET", "POST", "POST"]);
});

test("settlement detail route handler preserves record scoping", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  const capturedUrls: string[] = [];
  globalThis.fetch = async (input) => {
    capturedUrls.push(String(input));
    return new Response(JSON.stringify({ record: { recordId: 42 }, attempts: [] }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await getSettlement(
    new NextRequest("https://ops.example.test/api/ops/settlements/42", { method: "GET" }),
    { params: Promise.resolve({ recordId: "42" }) }
  );

  assert.equal(response.status, 200);
  assert.deepEqual(capturedUrls, ["https://api.example.test/ops/api/settlements/42"]);
});

test("reconciliation route handler preserves queue filters", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  const capturedUrls: string[] = [];
  globalThis.fetch = async (input) => {
    capturedUrls.push(String(input));
    return new Response(JSON.stringify({ items: [], counts: {} }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };

  const response = await getReconciliation(
    new NextRequest(
      "https://ops.example.test/api/ops/reconciliation?status=unknown&sellerRef=alpha-seller&minAgeSeconds=300",
      { method: "GET" }
    )
  );

  assert.equal(response.status, 200);
  assert.deepEqual(capturedUrls, [
    "https://api.example.test/ops/api/reconciliation?status=unknown&sellerRef=alpha-seller&minAgeSeconds=300"
  ]);
});

test("reconciliation action route handlers preserve record scoping", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  const capturedUrls: string[] = [];
  const capturedMethods: string[] = [];
  globalThis.fetch = async (input, init) => {
    capturedUrls.push(String(input));
    capturedMethods.push(init?.method ?? "GET");
    return new Response(JSON.stringify({ record: { recordId: 42 }, attempts: [] }), {
      status: 200,
      headers: { "content-type": "application/json" }
    });
  };
  const requestInit = {
    method: "POST",
    headers: {
      "content-type": "application/json",
      origin: "https://ops.example.test"
    },
    body: JSON.stringify({ reason: "operator triage" })
  };

  await postReconciliationClaim(
    new NextRequest("https://ops.example.test/api/ops/reconciliation/42/claim", requestInit),
    { params: Promise.resolve({ recordId: "42" }) }
  );
  await postReconciliationRelease(
    new NextRequest("https://ops.example.test/api/ops/reconciliation/42/release", requestInit),
    { params: Promise.resolve({ recordId: "42" }) }
  );
  await postReconciliationManualReview(
    new NextRequest("https://ops.example.test/api/ops/reconciliation/42/manual-review", requestInit),
    { params: Promise.resolve({ recordId: "42" }) }
  );

  assert.deepEqual(capturedUrls, [
    "https://api.example.test/ops/api/reconciliation/42/claim",
    "https://api.example.test/ops/api/reconciliation/42/release",
    "https://api.example.test/ops/api/reconciliation/42/manual-review"
  ]);
  assert.deepEqual(capturedMethods, ["POST", "POST", "POST"]);
});

test("seller creation forwards the exact request body", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  let capturedInit: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => {
    capturedInit = init;
    return new Response(JSON.stringify({ seller: {}, apiKey: "omck_test" }), {
      status: 201,
      headers: { "content-type": "application/json" }
    });
  };

  await postSellers(
    new NextRequest("https://ops.example.test/api/ops/sellers", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "https://ops.example.test"
      },
      body: JSON.stringify({ sellerRef: "alpha-seller", allowedPayTo: ["0x0000000000000000000000000000000000000001"] })
    })
  );

  assert.equal(
    capturedInit?.body,
    JSON.stringify({ sellerRef: "alpha-seller", allowedPayTo: ["0x0000000000000000000000000000000000000001"] })
  );
});

test("proxy rejects cross-origin mutation before fetch", async () => {
  process.env.OMNICLAW_OPS_API_BASE_URL = "https://api.example.test";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
  let fetchCalled = false;
  globalThis.fetch = async () => {
    fetchCalled = true;
    return new Response("{}");
  };

  const response = await proxyOpsRequest(
    new NextRequest("https://ops.example.test/api/ops/pauses", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        origin: "http://ops.example.test"
      },
      body: JSON.stringify({ paused: true })
    }),
    "/ops/api/pauses"
  );

  assert.equal(response.status, 403);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(fetchCalled, false);
});
