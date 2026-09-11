import crypto from "node:crypto";

import { NextRequest, NextResponse } from "next/server";

const PRODUCTION_SESSION_COOKIE = "__Host-omniclaw_ops_session";
const PRODUCTION_OIDC_STATE_COOKIE = "__Host-omniclaw_ops_oidc";
const LOCAL_SESSION_COOKIE = "omniclaw_ops_session";
const LOCAL_OIDC_STATE_COOKIE = "omniclaw_ops_oidc";
const COOKIE_MAX_AGE_SECONDS = 60 * 60;
const LOCAL_COMPOSE_SESSION_SECRET = "local-ops-console-session-secret-change-before-production";
const PLACEHOLDER_SESSION_SECRETS = new Set([
  "replace-with-32-byte-random-secret",
  "replace-with-32-byte-minimum-random-secret"
]);
const DEFAULT_OIDC_TOKEN_TIMEOUT_MS = 5_000;

type OidcStateCookie = {
  state: string;
  nonce: string;
  codeVerifier: string;
  returnTo: string;
};

export type OpsSession = {
  accessToken: string;
  expiresAt: number;
  subject?: string;
  issuer?: string;
  operatorKey?: string;
  email?: string;
};

export type PublicOpsSession = {
  authenticated: boolean;
  subject?: string;
  issuer?: string;
  operatorKey?: string;
  email?: string;
  expiresAt?: number;
};

export function authorizationHeaderFromSession(request: NextRequest): string | null {
  const session = readOpsSession(request);
  return session?.accessToken ? `Bearer ${session.accessToken}` : null;
}

export function readPublicOpsSession(request: NextRequest): PublicOpsSession {
  const session = readOpsSession(request);
  if (!session) {
    return { authenticated: false };
  }
  return {
    authenticated: true,
    subject: session.subject,
    issuer: session.issuer,
    operatorKey: session.operatorKey,
    email: session.email,
    expiresAt: session.expiresAt
  };
}

export function readOpsSession(request: NextRequest): OpsSession | null {
  const raw = request.cookies.get(sessionCookieName())?.value;
  if (!raw) {
    return null;
  }
  const session = decryptCookie<OpsSession>(raw);
  if (!session || !session.accessToken || session.expiresAt <= epochSeconds()) {
    return null;
  }
  return session;
}

export function createLoginResponse(request: NextRequest): NextResponse {
  const config = oidcConfig();
  const state = randomToken();
  const nonce = randomToken();
  const codeVerifier = randomToken(64);
  const codeChallenge = base64url(crypto.createHash("sha256").update(codeVerifier).digest());
  const returnTo = safeReturnTo(request.nextUrl.searchParams.get("returnTo"));
  const authorizationUrl = new URL(config.authorizationUrl);
  authorizationUrl.searchParams.set("response_type", "code");
  authorizationUrl.searchParams.set("client_id", config.clientId);
  authorizationUrl.searchParams.set("redirect_uri", config.redirectUri);
  authorizationUrl.searchParams.set("scope", "openid email profile");
  authorizationUrl.searchParams.set("state", state);
  authorizationUrl.searchParams.set("nonce", nonce);
  authorizationUrl.searchParams.set("code_challenge", codeChallenge);
  authorizationUrl.searchParams.set("code_challenge_method", "S256");

  const response = noStoreRedirect(authorizationUrl);
  setEncryptedCookie(response, oidcStateCookieName(), {
    state,
    nonce,
    codeVerifier,
    returnTo
  } satisfies OidcStateCookie, 300);
  return response;
}

export async function createCallbackResponse(request: NextRequest): Promise<NextResponse> {
  const config = oidcConfig();
  const expected = decryptCookie<OidcStateCookie>(request.cookies.get(oidcStateCookieName())?.value ?? "");
  if (!expected) {
    return authErrorRedirect(request, "OIDC state is missing");
  }
  const state = request.nextUrl.searchParams.get("state") ?? "";
  const code = request.nextUrl.searchParams.get("code") ?? "";
  if (!code || state !== expected.state) {
    return authErrorRedirect(request, "OIDC state is invalid");
  }

  const tokenResponse = await fetch(config.tokenUrl, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.clientId,
      code,
      redirect_uri: config.redirectUri,
      code_verifier: expected.codeVerifier
    }),
    cache: "no-store",
    signal: AbortSignal.timeout(oidcTokenTimeoutMs(process.env.OMNICLAW_OPS_OIDC_TOKEN_TIMEOUT_MS))
  });
  if (!tokenResponse.ok) {
    return authErrorRedirect(request, "OIDC token exchange failed");
  }
  const body = (await tokenResponse.json()) as {
    access_token?: string;
    expires_in?: number;
  };
  if (!body.access_token) {
    return authErrorRedirect(request, "OIDC token response did not include an access token");
  }
  const claims = jwtPayload(body.access_token);
  const subject = typeof claims.sub === "string" ? claims.sub : undefined;
  const issuer = typeof claims.iss === "string" ? claims.iss : undefined;
  const session: OpsSession = {
    accessToken: body.access_token,
    expiresAt: epochSeconds() + Math.max(1, Number(body.expires_in ?? COOKIE_MAX_AGE_SECONDS)),
    subject,
    issuer,
    operatorKey: subject ? operatorLeaseOwner(subject, issuer ?? "") : undefined,
    email: typeof claims.email === "string" ? claims.email : undefined
  };
  const response = noStoreRedirect(new URL(expected.returnTo, publicOrigin(request)));
  setEncryptedCookie(response, sessionCookieName(), session, COOKIE_MAX_AGE_SECONDS);
  clearCookie(response, oidcStateCookieName());
  return response;
}

export function createLogoutResponse(request: NextRequest): NextResponse {
  const config = oidcConfig();
  const origin = publicOrigin(request);
  const logoutUrl = config.logoutUrl ? new URL(config.logoutUrl) : new URL("/", origin);
  if (config.logoutUrl) {
    logoutUrl.searchParams.set("client_id", config.clientId);
    logoutUrl.searchParams.set("post_logout_redirect_uri", origin);
  }
  const response = noStoreRedirect(logoutUrl);
  clearCookie(response, sessionCookieName());
  clearCookie(response, oidcStateCookieName());
  return response;
}

export function encryptedSessionCookieForTest(session: OpsSession): string {
  return encryptCookie(session);
}

export function validateOpsRuntimeEnv(): { ok: true } | { ok: false; error: string } {
  try {
    assertSessionSecretSafe(requiredEnv("OMNICLAW_OPS_SESSION_SECRET"));
    oidcConfig();
    return { ok: true };
  } catch (caught) {
    return { ok: false, error: caught instanceof Error ? caught.message : "invalid ops runtime env" };
  }
}

function oidcConfig() {
  return {
    clientId: requiredEnv("OMNICLAW_OPS_OIDC_CLIENT_ID"),
    authorizationUrl: requiredEnv("OMNICLAW_OPS_OIDC_AUTHORIZATION_URL"),
    tokenUrl: requiredEnv("OMNICLAW_OPS_OIDC_TOKEN_URL"),
    redirectUri: requiredEnv("OMNICLAW_OPS_OIDC_REDIRECT_URI"),
    logoutUrl: process.env.OMNICLAW_OPS_OIDC_LOGOUT_URL
  };
}

function publicOrigin(request: NextRequest): string {
  const configured = process.env.OMNICLAW_OPS_PUBLIC_ORIGIN?.trim();
  return configured ? new URL(configured).origin : request.nextUrl.origin;
}

function sessionCookieName(): string {
  return usesSecureCookies() ? PRODUCTION_SESSION_COOKIE : LOCAL_SESSION_COOKIE;
}

function oidcStateCookieName(): string {
  return usesSecureCookies() ? PRODUCTION_OIDC_STATE_COOKIE : LOCAL_OIDC_STATE_COOKIE;
}

function setEncryptedCookie(response: NextResponse, name: string, value: unknown, maxAge: number) {
  response.cookies.set(name, encryptCookie(value), cookieOptions(maxAge));
}

function clearCookie(response: NextResponse, name: string) {
  response.cookies.set(name, "", { ...cookieOptions(0), maxAge: 0 });
}

function cookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    secure: usesSecureCookies(),
    sameSite: "lax" as const,
    path: "/",
    maxAge
  };
}

function usesSecureCookies(): boolean {
  const configured = process.env.OMNICLAW_OPS_PUBLIC_ORIGIN?.trim();
  if (configured) {
    return new URL(configured).protocol === "https:";
  }
  return process.env.NODE_ENV === "production";
}

function operatorLeaseOwner(subject: string, issuer: string): string {
  const safeSubject = subject.replace(/[^A-Za-z0-9_.@-]/g, "").slice(0, 48) || "unknown";
  const digest = crypto
    .createHash("sha256")
    .update(`${issuer}\0${subject}`)
    .digest("hex")
    .slice(0, 24);
  return `${safeSubject}.${digest}`;
}

function encryptCookie(value: unknown): string {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", sessionKey(), iv);
  const ciphertext = Buffer.concat([cipher.update(Buffer.from(JSON.stringify(value))), cipher.final()]);
  return `${base64url(iv)}.${base64url(cipher.getAuthTag())}.${base64url(ciphertext)}`;
}

function decryptCookie<T>(value: string): T | null {
  const parts = value.split(".");
  if (parts.length !== 3) {
    return null;
  }
  try {
    const [iv, tag, ciphertext] = parts.map(base64urlDecode);
    const decipher = crypto.createDecipheriv("aes-256-gcm", sessionKey(), iv);
    decipher.setAuthTag(tag);
    const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    return JSON.parse(plaintext.toString("utf-8")) as T;
  } catch {
    return null;
  }
}

function sessionKey(): Buffer {
  const secret = requiredEnv("OMNICLAW_OPS_SESSION_SECRET");
  assertSessionSecretSafe(secret);
  return crypto.createHash("sha256").update(secret).digest();
}

function assertSessionSecretSafe(secret: string) {
  if (process.env.NODE_ENV !== "production") {
    return;
  }
  if (process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT === "local-compose") {
    return;
  }
  if (
    secret === LOCAL_COMPOSE_SESSION_SECRET ||
    PLACEHOLDER_SESSION_SECRETS.has(secret) ||
    secret.length < 32 ||
    isLowEntropySecret(secret)
  ) {
    throw new Error(
      "OMNICLAW_OPS_SESSION_SECRET must be a production-only random secret with at least 32 characters"
    );
  }
}

function isLowEntropySecret(secret: string): boolean {
  if (new Set(secret).size < 8) {
    return true;
  }
  for (const chunkSize of [1, 2, 4, 8, 16]) {
    if (secret.length % chunkSize !== 0) {
      continue;
    }
    const chunk = secret.slice(0, chunkSize);
    if (chunk.repeat(secret.length / chunkSize) === secret) {
      return true;
    }
  }
  return false;
}

function jwtPayload(token: string): Record<string, unknown> {
  const payload = token.split(".")[1];
  if (!payload) {
    return {};
  }
  try {
    return JSON.parse(base64urlDecode(payload).toString("utf-8")) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function safeReturnTo(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return "/";
  }
  return value;
}

function randomToken(bytes = 32): string {
  return base64url(crypto.randomBytes(bytes));
}

function base64url(value: Buffer): string {
  return value.toString("base64url");
}

function base64urlDecode(value: string): Buffer {
  return Buffer.from(value, "base64url");
}

function epochSeconds(): number {
  return Math.floor(Date.now() / 1000);
}

function oidcTokenTimeoutMs(value: string | undefined): number {
  if (!value) {
    return DEFAULT_OIDC_TOKEN_TIMEOUT_MS;
  }
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 500 || parsed > 30_000) {
    return DEFAULT_OIDC_TOKEN_TIMEOUT_MS;
  }
  return parsed;
}

function requiredEnv(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function authErrorRedirect(request: NextRequest, detail: string): NextResponse {
  const url = new URL("/", publicOrigin(request));
  url.searchParams.set("auth_error", detail);
  const response = noStoreRedirect(url);
  clearCookie(response, sessionCookieName());
  clearCookie(response, oidcStateCookieName());
  return response;
}

function noStoreRedirect(url: URL): NextResponse {
  const response = NextResponse.redirect(url);
  response.headers.set("cache-control", "no-store");
  return response;
}
