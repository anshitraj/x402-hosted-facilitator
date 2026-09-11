import { NextRequest, NextResponse } from "next/server";

import { authorizationHeaderFromSession } from "@/lib/ops-session";

const FORWARDED_HEADERS = [
  "content-type",
  "x-request-id",
  "traceparent"
];

type BaseUrlValidation =
  | {
      ok: true;
      url: URL;
    }
  | {
      ok: false;
      detail: string;
    };

export async function proxyOpsRequest(
  request: NextRequest,
  path: string
): Promise<NextResponse> {
  if (!isAllowedOpsPath(path)) {
    return noStoreJson({ detail: "Unknown operations route" }, 404);
  }
  const backend = validateOpsApiBaseUrl(
    process.env.OMNICLAW_OPS_API_BASE_URL,
    process.env.NODE_ENV,
    process.env.OMNICLAW_OPS_ALLOW_INSECURE_BACKEND,
    process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT
  );
  if (!backend.ok) {
    return noStoreJson({ detail: backend.detail }, 503);
  }
  const expectedOrigin = process.env.OMNICLAW_OPS_PUBLIC_ORIGIN ?? request.nextUrl.origin;
  if (!isSameOriginMutation(request.method, request.headers.get("origin"), expectedOrigin)) {
    return noStoreJson({ detail: "Cross-origin operation rejected" }, 403);
  }

  const upstreamUrl = new URL(path, normalizeBaseUrl(backend.url.toString()));
  const upstreamHeaders = new Headers();
  const authorization = authorizationHeaderFromSession(request);
  if (authorization) {
    upstreamHeaders.set("authorization", authorization);
  }
  for (const headerName of FORWARDED_HEADERS) {
    const value = request.headers.get(headerName);
    if (value) {
      upstreamHeaders.set(headerName, value);
    }
  }
  const cookie = filteredCookieHeader(
    request.headers.get("cookie"),
    process.env.OMNICLAW_OPS_FORWARD_COOKIE_NAMES,
    process.env.NODE_ENV
  );
  if (cookie) {
    upstreamHeaders.set("cookie", cookie);
  }
  upstreamHeaders.set("accept", "application/json");

  let response: Response;
  try {
    response = await fetch(upstreamUrl, {
      method: request.method,
      headers: upstreamHeaders,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.text(),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(proxyTimeoutMs(process.env.OMNICLAW_OPS_PROXY_TIMEOUT_MS))
    });
  } catch {
    return noStoreJson({ detail: "Operations backend is unavailable" }, 503);
  }

  const responseHeaders = new Headers();
  const contentType = response.headers.get("content-type");
  if (contentType) {
    responseHeaders.set("content-type", contentType);
  }
  responseHeaders.set("cache-control", "no-store");

  return new NextResponse(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders
  });
}

function noStoreJson(body: unknown, status: number): NextResponse {
  return NextResponse.json(body, {
    status,
    headers: {
      "cache-control": "no-store"
    }
  });
}

function isAllowedOpsPath(path: string): boolean {
  const pathname = path.split("?", 1)[0];
  if (pathname === "/ops/api/overview" || pathname === "/ops/api/pauses" || pathname === "/ops/api/sellers" || pathname === "/ops/api/reconciliation") {
    return true;
  }
  if (/^\/ops\/api\/settlements\/[0-9]{1,20}$/.test(pathname)) {
    return true;
  }
  if (/^\/ops\/api\/reconciliation\/[0-9]{1,20}\/(claim|release|manual-review)$/.test(pathname)) {
    return true;
  }
  return /^\/ops\/api\/sellers\/[A-Za-z0-9_.-]{3,64}(\/api-keys(\/[A-Za-z0-9_-]{1,80}\/revoke)?)?$/.test(pathname);
}

function normalizeBaseUrl(value: string): string {
  return value.endsWith("/") ? value : `${value}/`;
}

export function validateOpsApiBaseUrl(
  value: string | undefined,
  nodeEnv: string | undefined,
  allowInsecureBackend: string | undefined = undefined,
  insecureBackendContext: string | undefined = undefined
): BaseUrlValidation {
  if (!value) {
    return { ok: false, detail: "OMNICLAW_OPS_API_BASE_URL is not configured" };
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, detail: "OMNICLAW_OPS_API_BASE_URL is invalid" };
  }
  if (url.username || url.password) {
    return { ok: false, detail: "OMNICLAW_OPS_API_BASE_URL must not include credentials" };
  }
  if (url.protocol === "https:") {
    return { ok: true, url };
  }
  if (
    url.protocol === "http:" &&
    (isLoopbackHost(url.hostname) ||
      nodeEnv !== "production" ||
      (allowInsecureBackend === "true" && insecureBackendContext === "local-compose"))
  ) {
    return { ok: true, url };
  }
  return { ok: false, detail: "OMNICLAW_OPS_API_BASE_URL must use HTTPS" };
}

export function isSameOriginMutation(
  method: string,
  origin: string | null,
  expectedOrigin: string
): boolean {
  if (method === "GET" || method === "HEAD") {
    return true;
  }
  if (!origin) {
    return false;
  }
  try {
    return new URL(origin).origin === new URL(expectedOrigin).origin;
  } catch {
    return false;
  }
}

export function filteredCookieHeader(
  cookie: string | null,
  allowlist: string | undefined,
  nodeEnv: string | undefined = process.env.NODE_ENV
): string | null {
  if (!cookie) {
    return null;
  }
  if (!allowlist) {
    return nodeEnv === "production" ? null : cookie;
  }
  const allowedNames = new Set(
    allowlist
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
  );
  if (allowedNames.size === 0) {
    return null;
  }
  const selected = cookie
    .split(";")
    .map((part) => part.trim())
    .filter((part) => allowedNames.has(part.split("=", 1)[0]));
  return selected.length > 0 ? selected.join("; ") : null;
}

function isLoopbackHost(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";
}

export function proxyTimeoutMs(value: string | undefined): number {
  if (!value) {
    return 5_000;
  }
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 500 || parsed > 30_000) {
    return 5_000;
  }
  return parsed;
}
