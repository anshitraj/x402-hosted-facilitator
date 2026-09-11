import { NextRequest } from "next/server";

import { proxyOpsRequest } from "@/lib/ops-proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ recordId: string }> }
) {
  const { recordId } = await params;
  return proxyOpsRequest(request, `/ops/api/settlements/${encodeURIComponent(recordId)}`);
}
