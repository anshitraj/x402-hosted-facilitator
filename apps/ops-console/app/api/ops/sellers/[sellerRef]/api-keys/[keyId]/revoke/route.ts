import { NextRequest } from "next/server";

import { proxyOpsRequest } from "@/lib/ops-proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ sellerRef: string; keyId: string }> }
) {
  const { sellerRef, keyId } = await params;
  return proxyOpsRequest(
    request,
    `/ops/api/sellers/${encodeURIComponent(sellerRef)}/api-keys/${encodeURIComponent(keyId)}/revoke`
  );
}

