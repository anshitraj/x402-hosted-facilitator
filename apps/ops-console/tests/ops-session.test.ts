import assert from "node:assert/strict";
import test from "node:test";

import { NextRequest } from "next/server";

import {
  createCallbackResponse,
  encryptedSessionCookieForTest,
  validateOpsRuntimeEnv
} from "../lib/ops-session";

const LOCAL_SECRET = "local-ops-console-session-secret-change-before-production";

function setRequiredOidcEnv() {
  process.env.OMNICLAW_OPS_SESSION_SECRET = "test-session-secret-with-enough-entropy-123";
  process.env.OMNICLAW_OPS_OIDC_CLIENT_ID = "omniclaw-ops-console";
  process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL = "https://idp.example.test/auth";
  process.env.OMNICLAW_OPS_OIDC_TOKEN_URL = "https://idp.example.test/token";
  process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI = "https://ops.example.test/api/auth/callback";
  process.env.OMNICLAW_OPS_PUBLIC_ORIGIN = "https://ops.example.test";
}

test("production session secret rejects local default outside local compose", () => {
  const previousNodeEnv = process.env.NODE_ENV;
  const previousSecret = process.env.OMNICLAW_OPS_SESSION_SECRET;
  const previousContext = process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
  const env = process.env as Record<string, string | undefined>;
  try {
    env.NODE_ENV = "production";
    process.env.OMNICLAW_OPS_SESSION_SECRET = LOCAL_SECRET;
    delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;

    assert.throws(
      () =>
        encryptedSessionCookieForTest({
          accessToken: "token",
          expiresAt: Math.floor(Date.now() / 1000) + 60
        }),
      /OMNICLAW_OPS_SESSION_SECRET/
    );
  } finally {
    if (previousNodeEnv === undefined) {
      delete env.NODE_ENV;
    } else {
      env.NODE_ENV = previousNodeEnv;
    }
    if (previousSecret === undefined) {
      delete process.env.OMNICLAW_OPS_SESSION_SECRET;
    } else {
      process.env.OMNICLAW_OPS_SESSION_SECRET = previousSecret;
    }
    if (previousContext === undefined) {
      delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
    } else {
      process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT = previousContext;
    }
  }
});

test("production session secret rejects documented placeholders and low entropy values", () => {
  const previousNodeEnv = process.env.NODE_ENV;
  const previousSecret = process.env.OMNICLAW_OPS_SESSION_SECRET;
  const previousContext = process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
  const previousClientId = process.env.OMNICLAW_OPS_OIDC_CLIENT_ID;
  const previousAuthUrl = process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL;
  const previousTokenUrl = process.env.OMNICLAW_OPS_OIDC_TOKEN_URL;
  const previousRedirect = process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI;
  const env = process.env as Record<string, string | undefined>;
  try {
    env.NODE_ENV = "production";
    delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
    process.env.OMNICLAW_OPS_OIDC_CLIENT_ID = "omniclaw-ops-console";
    process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL = "https://idp.example.test/auth";
    process.env.OMNICLAW_OPS_OIDC_TOKEN_URL = "https://idp.example.test/token";
    process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI = "https://ops.example.test/api/auth/callback";

    for (const secret of [
      "replace-with-32-byte-random-secret",
      "replace-with-32-byte-minimum-random-secret",
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]) {
      process.env.OMNICLAW_OPS_SESSION_SECRET = secret;
      assert.deepEqual(validateOpsRuntimeEnv().ok, false);
    }
  } finally {
    if (previousNodeEnv === undefined) {
      delete env.NODE_ENV;
    } else {
      env.NODE_ENV = previousNodeEnv;
    }
    if (previousSecret === undefined) {
      delete process.env.OMNICLAW_OPS_SESSION_SECRET;
    } else {
      process.env.OMNICLAW_OPS_SESSION_SECRET = previousSecret;
    }
    if (previousContext === undefined) {
      delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
    } else {
      process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT = previousContext;
    }
    if (previousClientId === undefined) {
      delete process.env.OMNICLAW_OPS_OIDC_CLIENT_ID;
    } else {
      process.env.OMNICLAW_OPS_OIDC_CLIENT_ID = previousClientId;
    }
    if (previousAuthUrl === undefined) {
      delete process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL;
    } else {
      process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL = previousAuthUrl;
    }
    if (previousTokenUrl === undefined) {
      delete process.env.OMNICLAW_OPS_OIDC_TOKEN_URL;
    } else {
      process.env.OMNICLAW_OPS_OIDC_TOKEN_URL = previousTokenUrl;
    }
    if (previousRedirect === undefined) {
      delete process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI;
    } else {
      process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI = previousRedirect;
    }
  }
});

test("local compose may use the checked-in development session secret", () => {
  const previousNodeEnv = process.env.NODE_ENV;
  const previousSecret = process.env.OMNICLAW_OPS_SESSION_SECRET;
  const previousContext = process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
  const env = process.env as Record<string, string | undefined>;
  try {
    env.NODE_ENV = "production";
    process.env.OMNICLAW_OPS_SESSION_SECRET = LOCAL_SECRET;
    process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT = "local-compose";

    const cookie = encryptedSessionCookieForTest({
      accessToken: "token",
      expiresAt: Math.floor(Date.now() / 1000) + 60
    });

    assert.match(cookie, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  } finally {
    if (previousNodeEnv === undefined) {
      delete env.NODE_ENV;
    } else {
      env.NODE_ENV = previousNodeEnv;
    }
    if (previousSecret === undefined) {
      delete process.env.OMNICLAW_OPS_SESSION_SECRET;
    } else {
      process.env.OMNICLAW_OPS_SESSION_SECRET = previousSecret;
    }
    if (previousContext === undefined) {
      delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
    } else {
      process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT = previousContext;
    }
  }
});

test("OIDC callback auth errors clear stale auth cookies and disable caching", async () => {
  const previousEnv = {
    NODE_ENV: process.env.NODE_ENV,
    OMNICLAW_OPS_SESSION_SECRET: process.env.OMNICLAW_OPS_SESSION_SECRET,
    OMNICLAW_OPS_OIDC_CLIENT_ID: process.env.OMNICLAW_OPS_OIDC_CLIENT_ID,
    OMNICLAW_OPS_OIDC_AUTHORIZATION_URL: process.env.OMNICLAW_OPS_OIDC_AUTHORIZATION_URL,
    OMNICLAW_OPS_OIDC_TOKEN_URL: process.env.OMNICLAW_OPS_OIDC_TOKEN_URL,
    OMNICLAW_OPS_OIDC_REDIRECT_URI: process.env.OMNICLAW_OPS_OIDC_REDIRECT_URI,
    OMNICLAW_OPS_PUBLIC_ORIGIN: process.env.OMNICLAW_OPS_PUBLIC_ORIGIN,
    OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT: process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT
  };
  const env = process.env as Record<string, string | undefined>;
  try {
    env.NODE_ENV = "production";
    delete process.env.OMNICLAW_OPS_INSECURE_BACKEND_CONTEXT;
    setRequiredOidcEnv();

    const response = await createCallbackResponse(
      new NextRequest("https://ops.example.test/api/auth/callback?state=stale&code=missing-cookie")
    );
    const setCookieHeader = response.headers.get("set-cookie") ?? "";

    assert.equal(response.headers.get("cache-control"), "no-store");
    assert.equal(response.headers.get("location"), "https://ops.example.test/?auth_error=OIDC+state+is+missing");
    assert.match(setCookieHeader, /__Host-omniclaw_ops_session=/);
    assert.match(setCookieHeader, /__Host-omniclaw_ops_oidc=/);
    assert.match(setCookieHeader, /Max-Age=0/);
  } finally {
    for (const [key, value] of Object.entries(previousEnv)) {
      if (value === undefined) {
        delete env[key];
      } else {
        env[key] = value;
      }
    }
  }
});
