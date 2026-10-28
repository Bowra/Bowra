import { defineConfig, devices } from "@playwright/test";

/**
 * Alt config — run against the existing production server (port 3101); do not start webServer.
 * For W12-12 mobile audit batch runs (main config's 3100 is stale; use 3101).
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:3101",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"], locale: "zh-CN" },
    },
    {
      // W12-12: simulate mobile with iPhone 13 viewport
      name: "iphone-13",
      use: { ...devices["iPhone 13"], locale: "zh-CN" },
    },
    {
      name: "pixel-7",
      use: { ...devices["Pixel 7"], locale: "zh-CN" },
    },
  ],
});
