import { NextRequest } from "next/server";

import { proxyOpsRequest } from "@/lib/ops-proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ sellerRef: string }> }
) {
  const { sellerRef } = await params;
  return proxyOpsRequest(request, `/ops/api/sellers/${encodeURIComponent(sellerRef)}/api-keys`);
}

