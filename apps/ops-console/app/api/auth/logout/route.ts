import { NextRequest } from "next/server";

import { createLogoutResponse } from "@/lib/ops-session";

export function GET(request: NextRequest) {
  return createLogoutResponse(request);
}

export function POST(request: NextRequest) {
  return createLogoutResponse(request);
}
