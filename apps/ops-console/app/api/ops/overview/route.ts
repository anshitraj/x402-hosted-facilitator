import { NextRequest } from "next/server";

import { proxyOpsRequest } from "@/lib/ops-proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  return proxyOpsRequest(request, "/ops/api/overview");
}
