import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";

const operatorEmail = process.env.OMNICLAW_OPS_E2E_EMAIL ?? "ops@omniclaw.ai";
const operatorPassword = process.env.OMNICLAW_OPS_E2E_PASSWORD ?? "omniclaw-alpha-admin";
const sellerPayTo =
  process.env.OMNICLAW_OPS_E2E_SELLER_PAY_TO ?? "0x95cE2edF56cc05b2634267d55D8AfB7f8630c143";

test("operator can use live control-plane flows", async ({ page }) => {
  const sellerRef = `qa-seller-${Date.now()}`;
  const sellerName = `QA Seller ${sellerRef}`;
  const reason = `playwright qa ${Date.now()}`;
  const seededSettlementSeller = `qa-ui-settlement-${Date.now()}`;

  seedLiveSettlementRows(seededSettlementSeller);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Control Plane" })).toBeVisible();
  await page.getByRole("link", { name: /sign in/i }).click();

  await page.getByLabel(/username|email/i).fill(operatorEmail);
  await page.locator("input[name='password']").fill(operatorPassword);
  await page.getByRole("button", { name: /sign in/i }).click();

  await expect(page.getByRole("heading", { name: "Operations Overview" })).toBeVisible();
  await expect(page.getByText("All systems operational")).toBeVisible();
  await expect(page.getByText(/project_id|project id|route_key|route key|internal slug|internal cohort/i)).toHaveCount(0);

  await page.getByRole("button", { name: "Settlements" }).click();
  await expect(page.getByRole("heading", { name: "Settlement & Reconciliation" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent Settlements" })).toBeVisible();
  const recentSettlements = page.locator("section.widePanel").filter({ has: page.getByRole("heading", { name: "Recent Settlements" }) });
  await expect(recentSettlements.getByText(seededSettlementSeller).first()).toBeVisible();
  await expect(recentSettlements.getByText("Exact EVM").first()).toBeVisible();
  await expect(recentSettlements.getByText("Gateway batch").first()).toBeVisible();
  await expect(recentSettlements.getByText("0.001 USDC").first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "exact_evm" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "circle_gateway" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "Settled" }).first()).toBeVisible();
  await recentSettlements.getByRole("button", { name: /view settlement/i }).first().click();
  const settlementDetail = page.locator("section.settlementDetailPanel");
  await expect(settlementDetail.getByRole("heading", { name: /Record [0-9]+/ })).toBeVisible();
  await expect(settlementDetail.getByRole("heading", { name: "Transaction Evidence" })).toBeVisible();
  await expect(settlementDetail.getByRole("heading", { name: "Settlement Attempts" })).toBeVisible();
  await expect(settlementDetail.getByRole("heading", { name: "Reconciliation" })).toBeVisible();
  await expect(settlementDetail.getByText(/0x[a-fA-F0-9]{6,}|Gateway Transfer|Transaction Hash/).first()).toBeVisible();

  await page.getByRole("button", { name: "Providers" }).click();
  await expect(page.getByRole("heading", { name: "Configured Providers" })).toBeVisible();
  const signerRunway = page.locator("section.widePanel").filter({ has: page.getByRole("heading", { name: "Gas & Balance" }) });
  await expect(signerRunway.getByText("Exact EVM signer")).toBeVisible();
  await expect(signerRunway.getByText("Arc Testnet")).toBeVisible();
  await expect(signerRunway.getByText(/circle_gateway|balance unavailable|eip155:5042002/)).toHaveCount(0);

  await page.getByRole("button", { name: "Sellers" }).click();
  await expect(page.getByRole("heading", { name: "Seller Onboarding" })).toBeVisible();
  await expect(page.getByText(/project_id|project id|route_key|route key|internal slug|internal cohort/i)).toHaveCount(0);
  await expect(page.getByLabel("Network")).toHaveValue("eip155:5042002");
  await expect(page.getByLabel("Network")).toContainText("Arc Testnet");
  await expect(page.locator(".sellerForm").getByText("USDC Asset")).toHaveCount(0);
  await expect(page.locator(".sellerForm").getByText("0x3600000000000000000000000000000000000000")).toHaveCount(0);
  await page.getByLabel("Name").fill(sellerName);
  await page.getByLabel("Seller Pay To").fill(sellerPayTo);
  await page.getByRole("button", { name: /create seller/i }).click();

  await expect(page.getByText("The raw API key is shown once")).toBeVisible();
  await expect(page.locator(".issuedAccess").getByText("Arc Testnet")).toBeVisible();
  await expect(page.locator(".issuedAccess").getByText(/exact_evm|circle_gateway|Exact EVM|Circle Gateway|USDC/)).toHaveCount(0);
  await page.getByRole("button", { name: "Clear secret" }).click();
  await expect(page.getByText("Secret cleared. Only the key prefix remains in audit and seller records.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Manage Sellers" })).toBeVisible();
  await expect(page.getByRole("button", { name: new RegExp(sellerName) })).toBeVisible();
  await page.getByRole("button", { name: new RegExp(sellerName) }).click();
  const sellerReferenceRow = page.locator(".sellerDetail .statusRow").filter({
    has: page.locator("span", { hasText: /^Reference$/ })
  });
  await expect(sellerReferenceRow.locator("strong")).toContainText(sellerRef);
  await expect(page.getByRole("heading", { name: "Payment Profiles" })).toBeVisible();
  await expect(page.getByText("Arc Testnet").last()).toBeVisible();
  await expect(page.getByRole("heading", { name: "API Keys" })).toBeVisible();

  await page.getByRole("button", { name: "Issue key" }).click();
  await expect(page.getByText("New API Key")).toBeVisible();
  await expect(page.locator(".sellerDetail .keyBox code")).toContainText(/^omck_/);
  await page.locator(".sellerDetail").getByRole("button", { name: "Clear secret" }).click();
  await expect(page.locator(".sellerDetail").getByText("New API Key")).toHaveCount(0);
  await expect(page.locator(".sellerKeyTable").getByText("active").first()).toBeVisible();
  await page.getByLabel("Revoke Reason").fill("playwright cleanup key rotation");

  await page.locator(".sellerKeyTable").getByRole("button", { name: "Revoke" }).first().click();
  await expect(page.locator(".sellerKeyTable").getByText("revoked").first()).toBeVisible();
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const activeRevokeButtons = page.locator(".sellerKeyTable button:not([disabled])", { hasText: "Revoke" });
    if ((await activeRevokeButtons.count()) === 0) {
      break;
    }
    await activeRevokeButtons.first().click();
    await expect(page.locator(".sellerKeyTable").getByText("revoked").first()).toBeVisible();
  }
  await expect(page.locator(".sellerKeyTable .statusBadge.good")).toHaveCount(0);

  await page.getByRole("button", { name: "Pauses" }).click();
  await expect(page.getByRole("heading", { name: "Emergency Controls" })).toBeVisible();
  await page.getByLabel("Reason").fill(reason);
  await page.getByRole("button", { name: "Apply pause" }).click();
  await expect(page.getByText("exact_evm")).toBeVisible();

  await page.locator("label.switch input").setChecked(false);
  await page.getByLabel("Reason").fill(`${reason} clear`);
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(page.getByText("Providers").locator("..").getByText("none")).toBeVisible();

  await page.locator("aside").getByRole("button", { name: "Audit Trail" }).click();
  await expect(page.getByRole("heading", { name: "Audit Trail" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "API Key Revoke" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "API Key Issue" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "Seller Create" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "Pause Set" }).first()).toBeVisible();
});

function seedLiveSettlementRows(sellerRef: string) {
  const suffix = Date.now();
  const sql = `
WITH seeded AS (
  INSERT INTO settlement_records (
    seller_account_id, payment_profile_id, fingerprint, provider, scheme, network,
    status, trace_id, transaction_hash, payer, raw_requirements_json, created_at, updated_at
  )
  VALUES
    (
      '${sellerRef}', 'default', 'pw-exact-${suffix}', 'exact_evm', 'exact',
      'eip155:5042002', 'settled', 'tr_pw_exact_${suffix}',
      '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      '0x1111111111111111111111111111111111111111',
      '{"scheme":"exact","network":"eip155:5042002","asset":"0x3600000000000000000000000000000000000000","amount":"1000","payTo":"0x2222222222222222222222222222222222222222","extra":{"name":"USDC"}}'::jsonb,
      NOW(), NOW()
    ),
    (
      '${sellerRef}', 'default', 'pw-gateway-${suffix}', 'circle_gateway', 'exact',
      'eip155:5042002', 'settled', 'tr_pw_gateway_${suffix}',
      'gateway-transfer-${suffix}',
      '0x3333333333333333333333333333333333333333',
      '{"scheme":"exact","network":"eip155:5042002","asset":"0x3600000000000000000000000000000000000000","amount":"1000","payTo":"0x2222222222222222222222222222222222222222","extra":{"name":"gateway_batch"}}'::jsonb,
      NOW(), NOW()
    )
  RETURNING id, trace_id, transaction_hash
)
INSERT INTO settlement_attempts (
  settlement_record_id, trace_id, status, started_at, finished_at, transaction_hash, error_reason
)
SELECT id, trace_id, 'settled', NOW(), NOW(), transaction_hash, NULL
FROM seeded;
`;
  execFileSync(
    "docker",
    [
      "compose",
      "-f",
      "../../docker-compose.yml",
      "exec",
      "-T",
      "postgres",
      "psql",
      "-U",
      "omniclaw",
      "-d",
      "omniclaw",
      "-v",
      "ON_ERROR_STOP=1",
      "-q",
      "-c",
      sql
    ],
    { cwd: process.cwd(), stdio: "pipe" }
  );
}
