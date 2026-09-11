export type HealthStatus = "ok" | "unhealthy" | string;

export type ComponentHealth = {
  name: string;
  status: HealthStatus;
  count?: number;
  providerCount?: number;
  networkCount?: number;
  unhealthyNetworkCount?: number;
  errorType?: string;
  signerStatus?: string;
  network?: string;
  provider?: string;
  environment?: string;
  balanceWei?: number;
  minBalanceWei?: number;
  maxConcurrentSettlements?: number;
  signerLockScope?: string;
  latencyMs?: number;
  supportedNetworks?: string[];
};

export type SettlementProviderNetworkStatus = {
  provider: string;
  network: string;
  status: string;
  count: number;
};

export type SettlementRecordSummary = {
  recordId: number;
  sellerRef: string;
  paymentProfileId: string;
  provider: string;
  scheme: string;
  network: string;
  status: string;
  traceId: string;
  transaction: string;
  payer: string;
  errorReason: string;
  amountAtomic: string;
  amountUsdc: string;
  asset: string;
  payTo: string;
  rail: string;
  createdAt: string;
  updatedAt: string;
  reconciliationAttempts: number;
};

export type SettlementReconciliation = {
  owner: string;
  leaseUntil: string;
  attempts: number;
  duplicateClaims: number;
  lastDuplicateAt: string;
};

export type SettlementAttempt = {
  attemptId: number;
  settlementRecordId: number;
  traceId: string;
  status: string;
  startedAt: string;
  finishedAt: string;
  transaction: string;
  transactionKind: string;
  explorerUrl: string;
  errorReason: string;
};

export type SettlementRecordDetail = SettlementRecordSummary & {
  transactionKind: string;
  explorerUrl: string;
  fingerprint: string;
  rawRequirements: {
    scheme: string;
    network: string;
    asset: string;
    amount: string;
    payTo: string;
    rail: string;
    resourceHash?: string;
  };
  reconciliation: SettlementReconciliation;
};

export type SettlementDetailResponse = {
  record: SettlementRecordDetail;
  attempts: SettlementAttempt[];
};

export type ReconciliationActionRequest = {
  reason: string;
  leaseSeconds?: number;
};

export type ReconciliationActionResponse = SettlementDetailResponse & {
  auditEvent?: AuditEvent | null;
};

export type ReconciliationQueueFilters = {
  status?: string;
  minAgeSeconds?: number;
  sellerRef?: string;
  provider?: string;
  network?: string;
  limit?: number;
};

export type ReconciliationQueueItem = SettlementRecordSummary & {
  ageSeconds: number;
  reconciliation: SettlementReconciliation;
};

export type ReconciliationQueueResponse = {
  generatedAt: string;
  filters: {
    statuses: string[];
    minAgeSeconds: number;
    sellerRef: string;
    provider: string;
    network: string;
    limit: number;
  };
  counts: Record<string, number>;
  items: ReconciliationQueueItem[];
  truncated: boolean;
};

export type SettlementSummary = {
  records: number;
  attempts: number;
  manualReviewBacklog: number;
  unknown: number;
  submitted: number;
  statusCounts: Record<string, number>;
  byProviderNetwork: SettlementProviderNetworkStatus[];
  oldestAgeSeconds: Record<string, number>;
  recentRecords: SettlementRecordSummary[];
};

export type PauseState = {
  globalPaused: boolean;
  providers: string[];
  networks: string[];
  writesEnabled: boolean;
};

export type AuditEvent = {
  eventId: number;
  action: string;
  targetType: "global" | "provider" | "network" | "seller" | "settlement";
  target: string;
  before: boolean;
  after: boolean;
  reason: string;
  actor: string;
  correlationId: string;
  createdAt: string;
};

export type OpsOverview = {
  service: string;
  status: HealthStatus;
  generatedAt: string;
  components: {
    settlementStore: ComponentHealth;
    rateLimiter: ComponentHealth;
    telemetry: ComponentHealth;
    controlPlane: ComponentHealth;
    providers: ComponentHealth;
  };
  providers: ComponentHealth & {
    names: string[];
    networks: string[];
    items?: ComponentHealth[];
  };
  settlement: SettlementSummary;
  pauseState: PauseState;
  auditTail: AuditEvent[];
};

export type PauseTargetType = "global" | "provider" | "network";

export type PauseRequest = {
  targetType: PauseTargetType;
  target?: string;
  paused: boolean;
  reason: string;
};

export type PauseResponse = {
  event: AuditEvent;
  pauseState: PauseState;
};

export type SellerCreateRequest = {
  sellerRef: string;
  tenantRef: string;
  name?: string;
  environment: "testnet" | "local";
  enabledNetworks: string[];
  allowedAssets: string[];
  allowedPayTo: string[];
};

export type SellerSummary = {
  sellerRef: string;
  tenantRef: string;
  name: string;
  environment: string;
  status: string;
  enabledNetworks: string[];
  enabledSchemes: string[];
  enabledProviders: string[];
  allowedAssets: string[];
  allowedPayTo: string[];
  paymentProfiles?: PaymentProfileSummary[];
};

export type PaymentProfileSummary = {
  paymentProfileId: string;
  sellerRef: string;
  name: string;
  status: string;
  enabledNetworks: string[];
  enabledSchemes: string[];
  enabledProviders: string[];
  allowedAssets: string[];
  allowedPayTo: string[];
};

export type SellerListItem = {
  sellerRef: string;
  tenantRef: string;
  name: string;
  environment: string;
  status: string;
  paymentProfileCount: number;
  activeApiKeyCount: number;
  createdAt: string;
  updatedAt: string;
};

export type SellerApiKeySummary = {
  keyId: string;
  sellerRef: string;
  paymentProfileId: string;
  keyPrefix: string;
  status: string;
  createdAt: string;
  revokedAt: string | null;
};

export type SellerSettlementSummary = {
  records: number;
  attempts: number;
  manualReviewBacklog: number;
  unknown: number;
  submitted: number;
  settled: number;
  failed: number;
  statusCounts: Record<string, number>;
  totalAmountAtomic: string;
  totalAmountUsdc: string;
  lastSettlementAt: string;
  lastUpdatedAt: string;
  reconciliationRisk: {
    backlog: number;
    active: number;
    staleActive: number;
    manualReview: number;
    unknown: number;
    oldestAgeSeconds: number;
  };
  recentRecords: SettlementRecordSummary[];
};

export type SellerDetailResponse = {
  seller: SellerSummary;
  apiKeys: SellerApiKeySummary[];
  settlement: SellerSettlementSummary;
};

export type SellerCreateResponse = {
  seller: SellerSummary;
  apiKey: string;
  apiKeyPrefix: string;
  facilitatorPath: string;
  createdBy: string;
  auditEvent?: AuditEvent | null;
};

export type SellerApiKeyIssueResponse = {
  apiKey: string;
  key: SellerApiKeySummary;
  auditEvent?: AuditEvent | null;
};

export type SellerApiKeyRevokeResponse = {
  key: SellerApiKeySummary;
  auditEvent?: AuditEvent | null;
};

export type OpsApiError = {
  status: number;
  detail: string;
};

export type OpsSession = {
  authenticated: boolean;
  subject?: string;
  issuer?: string;
  operatorKey?: string;
  email?: string;
  expiresAt?: number;
};

export async function readSession(): Promise<OpsSession> {
  const response = await fetch("/api/auth/session", {
    cache: "no-store",
    credentials: "include"
  });
  return readJson<OpsSession>(response);
}

export async function readOverview(): Promise<OpsOverview> {
  const response = await fetch("/api/ops/overview", {
    cache: "no-store",
    credentials: "include"
  });
  return readJson<OpsOverview>(response);
}

export async function listSellers(): Promise<SellerListItem[]> {
  const response = await fetch("/api/ops/sellers", {
    cache: "no-store",
    credentials: "include"
  });
  const body = await readJson<{ sellers: SellerListItem[] }>(response);
  return body.sellers;
}

export async function readSeller(sellerRef: string): Promise<SellerDetailResponse> {
  const response = await fetch(`/api/ops/sellers/${encodeURIComponent(sellerRef)}`, {
    cache: "no-store",
    credentials: "include"
  });
  return readJson<SellerDetailResponse>(response);
}

export async function readSettlement(recordId: number): Promise<SettlementDetailResponse> {
  const response = await fetch(`/api/ops/settlements/${encodeURIComponent(String(recordId))}`, {
    cache: "no-store",
    credentials: "include"
  });
  return readJson<SettlementDetailResponse>(response);
}

export async function readReconciliationQueue(
  filters: ReconciliationQueueFilters = {}
): Promise<ReconciliationQueueResponse> {
  const params = new URLSearchParams();
  if (filters.status && filters.status !== "all") {
    params.set("status", filters.status);
  }
  if (filters.minAgeSeconds !== undefined && filters.minAgeSeconds > 0) {
    params.set("minAgeSeconds", String(filters.minAgeSeconds));
  }
  if (filters.sellerRef) {
    params.set("sellerRef", filters.sellerRef);
  }
  if (filters.provider) {
    params.set("provider", filters.provider);
  }
  if (filters.network) {
    params.set("network", filters.network);
  }
  if (filters.limit !== undefined) {
    params.set("limit", String(filters.limit));
  }
  const query = params.toString();
  const response = await fetch(`/api/ops/reconciliation${query ? `?${query}` : ""}`, {
    cache: "no-store",
    credentials: "include"
  });
  return readJson<ReconciliationQueueResponse>(response);
}

export async function claimReconciliationRecord(
  recordId: number,
  payload: ReconciliationActionRequest
): Promise<ReconciliationActionResponse> {
  return postReconciliationAction(recordId, "claim", payload);
}

export async function releaseReconciliationRecord(
  recordId: number,
  payload: ReconciliationActionRequest
): Promise<ReconciliationActionResponse> {
  return postReconciliationAction(recordId, "release", payload);
}

export async function markReconciliationManualReview(
  recordId: number,
  payload: ReconciliationActionRequest
): Promise<ReconciliationActionResponse> {
  return postReconciliationAction(recordId, "manual-review", payload);
}

async function postReconciliationAction(
  recordId: number,
  action: "claim" | "release" | "manual-review",
  payload: ReconciliationActionRequest
): Promise<ReconciliationActionResponse> {
  const response = await fetch(`/api/ops/reconciliation/${encodeURIComponent(String(recordId))}/${action}`, {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  return readJson<ReconciliationActionResponse>(response);
}

export async function setPause(payload: PauseRequest): Promise<PauseResponse> {
  const response = await fetch("/api/ops/pauses", {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(payload)
  });
  return readJson<PauseResponse>(response);
}

export async function issueSellerApiKey(
  sellerRef: string,
  paymentProfileId = "default"
): Promise<SellerApiKeyIssueResponse> {
  const response = await fetch(`/api/ops/sellers/${encodeURIComponent(sellerRef)}/api-keys`, {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify({ paymentProfileId })
  });
  return readJson<SellerApiKeyIssueResponse>(response);
}

export async function revokeSellerApiKey(
  sellerRef: string,
  keyId: string,
  reason: string
): Promise<SellerApiKeyRevokeResponse> {
  const response = await fetch(
    `/api/ops/sellers/${encodeURIComponent(sellerRef)}/api-keys/${encodeURIComponent(keyId)}/revoke`,
    {
      method: "POST",
      cache: "no-store",
      credentials: "include",
      headers: {
        "content-type": "application/json"
      },
      body: JSON.stringify({ reason })
    }
  );
  return readJson<SellerApiKeyRevokeResponse>(response);
}

export async function createSeller(payload: SellerCreateRequest): Promise<SellerCreateResponse> {
  const response = await fetch("/api/ops/sellers", {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify({
      sellerRef: payload.sellerRef,
      tenantRef: payload.tenantRef,
      name: payload.name,
      environment: payload.environment,
      enabledNetworks: payload.enabledNetworks,
      allowedAssets: payload.allowedAssets,
      allowedPayTo: payload.allowedPayTo
    })
  });
  const body = await readJson<{
    seller: SellerSummary;
    apiKey: string;
    apiKeyPrefix: string;
    facilitatorPath: string;
    createdBy: string;
    auditEvent?: AuditEvent | null;
  }>(response);

  return {
    seller: body.seller,
    apiKey: body.apiKey,
    apiKeyPrefix: body.apiKeyPrefix,
    facilitatorPath: body.facilitatorPath,
    createdBy: body.createdBy,
    auditEvent: body.auditEvent
  };
}

async function readJson<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw {
      status: response.status,
      detail: typeof body?.detail === "string" ? body.detail : response.statusText
    } satisfies OpsApiError;
  }
  return body as T;
}
