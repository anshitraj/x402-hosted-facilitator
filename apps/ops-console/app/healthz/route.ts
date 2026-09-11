import { NextResponse } from "next/server";

import { validateOpsRuntimeEnv } from "../../lib/ops-session";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET() {
  const validation = validateOpsRuntimeEnv();
  if (!validation.ok) {
    return NextResponse.json(
      { status: "error", service: "omniclaw-ops-console", detail: validation.error },
      {
        status: 503,
        headers: {
          "cache-control": "no-store"
        }
      }
    );
  }
  return NextResponse.json(
    { status: "ok", service: "omniclaw-ops-console" },
    {
      headers: {
        "cache-control": "no-store"
      }
    }
  );
}
