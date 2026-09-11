import { defineConfig, devices } from "@playwright/test";

const managedServerUrl = process.env.OMNICLAW_OPS_E2E_BASE_URL ?? "http://127.0.0.1:3101";

export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: {
    timeout: 15_000
  },
  use: {
    baseURL: process.env.OMNICLAW_OPS_E2E_BASE_URL ?? "http://127.0.0.1:13001",
    trace: "off",
    screenshot: "off",
    video: "off"
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"]
      }
    }
  ],
  webServer:
    process.env.OMNICLAW_OPS_E2E_MANAGED_SERVER === "true"
      ? {
          command: "npx next dev --port 3101 --hostname 127.0.0.1",
          url: managedServerUrl,
          cwd: process.cwd(),
          reuseExistingServer: !process.env.CI,
          timeout: 120_000
        }
      : undefined,
  reporter: [["list"]]
});
