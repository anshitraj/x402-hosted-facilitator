import { NextRequest, NextResponse } from "next/server";

import { readPublicOpsSession } from "@/lib/ops-session";

export function GET(request: NextRequest) {
  return NextResponse.json(readPublicOpsSession(request), {
    headers: {
      "cache-control": "no-store"
    }
  });
}
