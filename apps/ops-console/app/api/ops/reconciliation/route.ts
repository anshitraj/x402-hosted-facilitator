import { NextRequest } from "next/server";

import { proxyOpsRequest } from "@/lib/ops-proxy";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  return proxyOpsRequest(request, `/ops/api/reconciliation${request.nextUrl.search}`);
}
