import { NextRequest } from "next/server";

import { createLoginResponse } from "@/lib/ops-session";

export function GET(request: NextRequest) {
  return createLoginResponse(request);
}
