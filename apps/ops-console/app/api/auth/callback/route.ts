import { NextRequest } from "next/server";

import { createCallbackResponse } from "@/lib/ops-session";

export async function GET(request: NextRequest) {
  return createCallbackResponse(request);
}
