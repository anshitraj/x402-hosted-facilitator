import { expect, test } from "@playwright/test";

const queueRecordId = 987654;
const queueTimestamp = new Date(Date.now() - 900_000).toISOString();
const evmTransaction =
  "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

const queueItem = {
  recordId: queueRecordId,
  sellerRef: "qa-queue-seller",
  paymentProfileId: "default",
  provider: "exact_evm",
  scheme: "exact",
  network: "eip155:5042002",
  status: "unknown",
  traceId: "tr_queue_e2e",
  transaction: "",
  payer: "0xaaaa...aaaa",
  errorReason: "provider_timeout",
  amountAtomic: "250000",
  amountUsdc: "0.25",
  asset: "0x3600...0000",
  payTo: "0xbbbb...bbbb",
  rail: "exact_evm",
  createdAt: queueTimestamp,
  updatedAt: queueTimestamp,
  reconciliationAttempts: 2,
  ageSeconds: 900,
  reconciliation: {
    owner: "operator-a",
    leaseUntil: queueTimestamp,
    attempts: 2,
    duplicateClaims: 1,
    lastDuplicateAt: queueTimestamp
  }
};

test("operator can filter reconciliation queue and open settlement detail", async ({ page }) => {
  const settlementDetailBody = {
    record: {
      ...queueItem,
      transaction: evmTransaction,
      transactionKind: "evm_tx",
      explorerUrl: `https://testnet.arcscan.app/tx/${evmTransaction}`,
      fingerprint: "redacted",
      rawRequirements: {
        scheme: "exact",
        network: "eip155:5042002",
        asset: "0x3600000000000000000000000000000000000000",
        amount: "250000",
        payTo: "0xbbbb...bbbb",
        rail: "exact_evm",
        resourceHash: "sha256:test"
      }
    },
    attempts: [
      {
        attemptId: 1,
        settlementRecordId: queueRecordId,
        traceId: "tr_queue_e2e",
        status: "settled",
        startedAt: queueTimestamp,
        finishedAt: queueTimestamp,
        transaction: evmTransaction,
        transactionKind: "evm_tx",
        explorerUrl: `https://testnet.arcscan.app/tx/${evmTransaction}`,
        errorReason: ""
      },
      {
        attemptId: 2,
        settlementRecordId: queueRecordId,
        traceId: "tr_queue_gateway",
        status: "settled",
        startedAt: queueTimestamp,
        finishedAt: queueTimestamp,
        transaction: "gateway-transfer-123",
        transactionKind: "gateway_transfer",
        explorerUrl: "",
        errorReason: ""
      }
    ]
  };

  await page.route("**/api/auth/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        authenticated: true,
        subject: "ops-alpha-admin",
        email: "ops@omniclaw.ai",
        expiresAt: Math.floor(Date.now() / 1000) + 300
      })
    });
  });
  await page.route("**/api/ops/overview", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify(overviewBody())
    });
  });
  await page.route("**/api/ops/reconciliation**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/ops/reconciliation") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        generatedAt: new Date().toISOString(),
        filters: {
          statuses: url.searchParams.get("status") === "unknown" ? ["unknown"] : ["unknown", "submitted", "settle_in_progress", "manual_review"],
          minAgeSeconds: Number(url.searchParams.get("minAgeSeconds") ?? "0"),
          sellerRef: url.searchParams.get("sellerRef") ?? "",
          provider: url.searchParams.get("provider") ?? "",
          network: url.searchParams.get("network") ?? "",
          limit: Number(url.searchParams.get("limit") ?? "50")
        },
        counts: { unknown: 1 },
        items: [queueItem],
        truncated: true
      })
    });
  });
  await page.route(`**/api/ops/settlements/${queueRecordId}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify(settlementDetailBody)
    });
  });
  await page.route(`**/api/ops/reconciliation/${queueRecordId}/claim`, async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      reason: "operator reconciliation triage",
      leaseSeconds: 300
    });
    settlementDetailBody.record.reconciliation = {
      ...settlementDetailBody.record.reconciliation,
      owner: "ops-alpha-admin",
      leaseUntil: new Date(Date.now() + 300_000).toISOString(),
      attempts: settlementDetailBody.record.reconciliation.attempts + 1
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        ...settlementDetailBody,
        auditEvent: {
          eventId: 99,
          action: "reconciliation_claim",
          targetType: "settlement",
          target: String(queueRecordId),
          before: false,
          after: true,
          reason: "operator_supplied",
          actor: "ops-alpha-admin",
          correlationId: "rec-claim",
          createdAt: new Date().toISOString()
        }
      })
    });
  });
  await page.route(`**/api/ops/reconciliation/${queueRecordId}/release`, async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      reason: "operator reconciliation triage",
      leaseSeconds: 300
    });
    settlementDetailBody.record.reconciliation = {
      ...settlementDetailBody.record.reconciliation,
      owner: "",
      leaseUntil: ""
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        ...settlementDetailBody,
        auditEvent: {
          eventId: 100,
          action: "reconciliation_release",
          targetType: "settlement",
          target: String(queueRecordId),
          before: true,
          after: false,
          reason: "operator_supplied",
          actor: "ops-alpha-admin",
          correlationId: "rec-release",
          createdAt: new Date().toISOString()
        }
      })
    });
  });
  await page.route(`**/api/ops/reconciliation/${queueRecordId}/manual-review`, async (route) => {
    expect(route.request().postDataJSON()).toEqual({
      reason: "operator reconciliation triage",
      leaseSeconds: 300
    });
    settlementDetailBody.record.status = "manual_review";
    settlementDetailBody.record.errorReason = "manual_review_operator_requested";
    settlementDetailBody.record.reconciliation = {
      ...settlementDetailBody.record.reconciliation,
      owner: "",
      leaseUntil: ""
    };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "cache-control": "no-store" },
      body: JSON.stringify({
        ...settlementDetailBody,
        auditEvent: {
          eventId: 101,
          action: "reconciliation_manual_review",
          targetType: "settlement",
          target: String(queueRecordId),
          before: true,
          after: true,
          reason: "operator_supplied",
          actor: "ops-alpha-admin",
          correlationId: "rec-manual",
          createdAt: new Date().toISOString()
        }
      })
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
  await page.getByRole("button", { name: "Settlements" }).click();

  const reconciliationPanel = page.locator("section.reconciliationPanel");
  await expect(reconciliationPanel.getByRole("heading", { name: "Operational Triage" })).toBeVisible();
  await expect(reconciliationPanel.getByText("qa-queue-seller")).toBeVisible();
  await reconciliationPanel.getByLabel("Status").selectOption("unknown");
  await reconciliationPanel.getByLabel("Minimum Age").selectOption("300");
  await reconciliationPanel.getByLabel("Seller").fill("qa-queue-seller");
  await reconciliationPanel.getByLabel("Provider").fill(" exact_evm ");
  await reconciliationPanel.getByLabel("Network").fill(" eip155:5042002 ");
  await Promise.all([
    page.waitForRequest((request) => {
      const url = new URL(request.url());
      return url.pathname === "/api/ops/reconciliation" &&
        url.searchParams.get("status") === "unknown" &&
        url.searchParams.get("sellerRef") === "qa-queue-seller" &&
        url.searchParams.get("minAgeSeconds") === "300" &&
        url.searchParams.get("provider") === "exact_evm" &&
        url.searchParams.get("network") === "eip155:5042002" &&
        url.searchParams.get("limit") === "50";
    }),
    reconciliationPanel.getByRole("button", { name: "Refresh reconciliation queue" }).click()
  ]);
  await expect(reconciliationPanel.getByText("Queue is truncated at 50 records")).toBeVisible();
  await reconciliationPanel
    .locator(`button[aria-label="Open reconciliation record ${queueRecordId}"]`)
    .click();
  const detailPanel = page.locator("section.settlementDetailPanel");
  await expect(detailPanel.getByRole("heading", { name: `Record ${queueRecordId}` })).toBeVisible();
  await expect(detailPanel.getByRole("heading", { name: "Transaction Evidence" })).toBeVisible();
  await expect(detailPanel.getByRole("heading", { name: "Settlement Attempts" })).toBeVisible();
  await expect(detailPanel.getByRole("heading", { name: "Reconciliation" })).toBeVisible();
  await expect(detailPanel.getByText("sha256:test")).toBeVisible();
  await expect(detailPanel.getByRole("link", { name: "Open explorer" })).toHaveAttribute(
    "href",
    `https://testnet.arcscan.app/tx/${evmTransaction}`
  );
  await expect(detailPanel.getByRole("link", { name: `Open attempt 1 transaction` })).toHaveAttribute(
    "href",
    `https://testnet.arcscan.app/tx/${evmTransaction}`
  );
  await expect(detailPanel.getByText("Gateway reference")).toBeVisible();
  const leaseOwnerRow = detailPanel.locator(".statusRow").filter({ hasText: "Lease Owner" });
  await detailPanel.getByRole("button", { name: "Claim" }).click();
  await expect(leaseOwnerRow.getByText("ops-alpha-admin")).toBeVisible();
  await detailPanel.getByRole("button", { name: "Release" }).click();
  await expect(leaseOwnerRow.getByText("none")).toBeVisible();
  await detailPanel.getByRole("button", { name: "Claim" }).click();
  await expect(leaseOwnerRow.getByText("ops-alpha-admin")).toBeVisible();
  await detailPanel.getByRole("button", { name: "Move to manual review" }).click();
  await expect(detailPanel.getByText("manual review").first()).toBeVisible();
  await expect(detailPanel.getByText("manual_review_operator_requested")).toBeVisible();
  await expect(reconciliationPanel.locator("tr.selectedRow").getByText("qa-queue-seller")).toBeVisible();
});

function overviewBody() {
  const now = new Date().toISOString();
  return {
    service: "omniclaw_hosted_facilitator",
    status: "ok",
    generatedAt: now,
    components: {
      settlementStore: { name: "settlement_store", status: "ok" },
      rateLimiter: { name: "rate_limiter", status: "ok" },
      telemetry: { name: "telemetry", status: "ok" },
      controlPlane: { name: "control_plane", status: "ok" },
      providers: { name: "providers", status: "ok", providerCount: 2 }
    },
    providers: {
      name: "providers",
      status: "ok",
      providerCount: 2,
      names: ["exact_evm", "circle_gateway"],
      networks: ["eip155:5042002"],
      items: []
    },
    settlement: {
      records: 1,
      attempts: 1,
      manualReviewBacklog: 0,
      unknown: 1,
      submitted: 0,
      statusCounts: { unknown: 1 },
      byProviderNetwork: [
        {
          provider: "exact_evm",
          network: "eip155:5042002",
          status: "unknown",
          count: 1
        }
      ],
      oldestAgeSeconds: { unknown: 900 },
      recentRecords: []
    },
    pauseState: {
      globalPaused: false,
      providers: [],
      networks: [],
      writesEnabled: true
    },
    auditTail: []
  };
}
