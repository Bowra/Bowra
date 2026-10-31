import { defineConfig, devices } from "@playwright/test";

const port = process.env.PLAYWRIGHT_PORT ?? "3100";
const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18080";
// Canvas profile requires one worker: mock server is one process, canvas-mock-scenarios.mjs
// per-session state is module-level; all canvas-cc specs share session "s_e2e" and global _test/reset.
// Multiple workers (CI defaults 2) cross-contaminate canvasBySession/reset each other's turnScript, causing flakes (review P1-2).
const workers = process.env.BOWHEAD_E2E_PROFILE === "canvas"
  ? 1
  : process.env.PLAYWRIGHT_WORKERS
  ? Number(process.env.PLAYWRIGHT_WORKERS)
  : process.env.CI
  ? 2
  : 1;
const baseURL = `http://localhost:${port}`;
const apiBaseURL = `http://127.0.0.1:${apiPort}`;

const E2E_PROFILE = process.env.BOWHEAD_E2E_PROFILE ?? "gate";

/** E2e specs requiring Chromium and WebKit (focus/layers/keyboard/history/deep-link navigation). */
const CROSS_BROWSER_SPECS = [
  "**/chat-web-links.spec.ts",
  "**/artifact-presentation.spec.ts",
  "**/configuration-field-guidance.spec.ts",
  "**/canvas-rewrite-lifecycle.spec.ts",
  "**/configuration-model-review.spec.ts",
  "**/release-notes-navigation.spec.ts",
  "**/collaboration-trace.spec.ts",
  "**/model-runtime-parameters.spec.ts",
  "**/runtime-policy-settings.spec.ts",
  "**/kb-ui-refinements.spec.ts",
  "**/layer-stacking.spec.ts",
  "**/html-artifact-fullscreen.spec.ts",
  "**/chart-block-layers.spec.ts",
  "**/chart-composition-edit.spec.ts",
  "**/chart-analytical.spec.ts",
  "**/menu-callsites.spec.ts",
  "**/shell-global-controls.spec.ts",
  "**/composer-ui-refresh-acceptance.spec.ts",
  "**/menu-behavior.spec.ts",
  "**/dialog-scroll-lock.spec.ts",
  "**/sidebar-session-delete.spec.ts",
  "**/sidebar-session-archive.spec.ts",
  "**/dialog-sheet.spec.ts",
  "**/credits-list-rows.spec.ts",
  "**/session-url-history.spec.ts",
  "**/navigation-centers.spec.ts",
  "**/settings-savebar.spec.ts",
  "**/settings-group-layout.spec.ts",
  "**/settings-page-routes.spec.ts",
  "**/workbench-ui-refresh.spec.ts",
  "**/workbench-behavior.spec.ts",
  "**/canvas-ui-refresh.spec.ts",
  "**/canvas-behavior.spec.ts",
  "**/canvas-behavior-rest.spec.ts",
  "**/home-single-column.spec.ts",
  "**/icons-ui-refresh.spec.ts",
  "**/public-pages.spec.ts",
  "**/agent-draft-chat-target.spec.ts",
  "**/agent-audit-recovery.spec.ts",
  "**/configuration-canvas-agents.spec.ts",
  "**/document-canvas-root.spec.ts",
  "**/document-draft-canvas.spec.ts",
  "**/settings-pages-canvas.spec.ts",
  "**/canvas-cc/configuration-settings.spec.ts",
  "**/canvas-cc/results-reading-layout.spec.ts",
  "**/canvas-cc/html-interactive.spec.ts",
];

const LEGACY_E2E_SPECS = [
  "**/chat-composition-v8-fidelity.spec.ts",
  "**/step-list-us1-golden.spec.ts",
  "**/rho/*.spec.ts",
];

const LIVE_E2E_SPECS = [
  "**/mu-fail-safe.spec.ts",
  "**/nu1-kb-ref-citation.spec.ts",
  "**/canvas-reconcile.live.spec.ts",
];

const SSE_E2E_SPECS = [
  "**/chat-web-links.spec.ts",
  "**/model-failure-replay.spec.ts",
  "**/office-context-replay.spec.ts",
  "**/steer-live-sse.spec.ts",
  "**/composer-ordinary-draft-storage.spec.ts",
  "**/composer-submission-recovery.spec.ts",
  "**/agent-draft-chat-target.spec.ts",
  "**/configuration-canvas-agents.spec.ts",
  "**/document-canvas-root.spec.ts",
  "**/settings-pages-canvas.spec.ts",
  // The shared sidebar only fetches sessions in sse mode; gate uses static,
  // read-only scenario rows and cannot exercise an unread session fixture.
  "**/theme-home-accent.spec.ts",
];

// canvas-comprehensive Batch 1 (WP-0): deterministic canvas SSE specs run against
// the stateful mock server in SSE_MODE=sse (gate profile is mock — can't exercise
// real server SSE / drop injection). Isolated under e2e/canvas-cc/.
const CANVAS_E2E_SPECS = [
  "**/canvas-cc/*.spec.ts",
];

// SSE_MODE=sse for the canvas profile (real EventSource to the mock server) and the
// sse profile (steer-live-sse); every other profile keeps mock (FE-internal scenarios.ts).
const SSE_MODE = (E2E_PROFILE === "canvas" || E2E_PROFILE === "sse") ? "sse" : "mock";

const PROFILE_MATCH: Record<string, string[] | undefined> = {
  all: undefined,
  gate: undefined,
  legacy: LEGACY_E2E_SPECS,
  live: LIVE_E2E_SPECS,
  sse: SSE_E2E_SPECS,
  canvas: CANVAS_E2E_SPECS,
};

const PROFILE_IGNORE: Record<string, string[] | undefined> = {
  // all/gate scan every spec (testMatch undefined) → must exclude canvas-cc, which
  // needs its own SSE_MODE=sse webserver env (would fail in mock mode).
  all: [...CANVAS_E2E_SPECS],
  gate: [...LEGACY_E2E_SPECS, ...LIVE_E2E_SPECS, ...SSE_E2E_SPECS, ...CANVAS_E2E_SPECS],
  // legacy/live/sse restrict via testMatch already, so no canvas leakage there.
  legacy: [],
  live: [],
  sse: [],
  canvas: [],
};

const shouldStartWebServer = E2E_PROFILE !== "live";

/**
 * Playwright E2E: start Next.js dev server (mock mode), run a smoke case.
 * CI: `pnpm install`, then `pnpm exec playwright install --with-deps chromium`, then `pnpm test:e2e`.
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: PROFILE_MATCH[E2E_PROFILE],
  testIgnore: PROFILE_IGNORE[E2E_PROFILE] ?? PROFILE_IGNORE.gate,
  timeout: 60_000,
  expect: { timeout: 5_000 },
  reporter: [["list"]],
  workers,
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  // Local reuseExistingServer=true reuses manually started dev (fast), but full dev suite
  // has clicks intercepted by Next.js DevTools overlay (W11-11: 3 flaky tests).
  // CI uses production build (npm build + start), no overlay, all tests pass.
  // Most stable locally: `npm run build && npm start -- --port 3100`, then `npx playwright test`.
  webServer: shouldStartWebServer
    ? [
        {
          command: `node e2e/mock-api-server.mjs --port ${apiPort}`,
          url: `${apiBaseURL}/health`,
          reuseExistingServer: process.env.PLAYWRIGHT_REUSE_SERVER === "1",
          timeout: 30_000,
          // Explicitly pass the profile so the mock routes /chat/send + /chat/stream to
          // the canvas mock (canvas profile) vs the live-SSE steer mock (sse profile).
          // webServer env is NOT guaranteed to inherit the parent's BOWHEAD_E2E_PROFILE.
          env: { BOWHEAD_E2E_PROFILE: E2E_PROFILE },
        },
        {
          command: process.env.CI
            ? `npm run build && npx next start --port ${port}`
            : `npx next dev --port ${port}`,
          url: baseURL,
          reuseExistingServer: process.env.PLAYWRIGHT_REUSE_SERVER === "1",
          timeout: 180_000,
          env: {
            BOWHEAD_DEV_PROXY_TARGET: apiBaseURL,
            NEXT_PUBLIC_BOWHEAD_E2E: "1",
            NEXT_PUBLIC_BOWHEAD_SSE_MODE: SSE_MODE,
          },
        },
      ]
    : undefined,
  projects: [
    {
      // Safari engine: cross-browser interaction specs only (focus/layers/keyboard). UI redesign bugs repeatedly appeared only in WebKit
      // (button click unfocused, Tab skips buttons by default); green Chromium does not prove Safari usability.
      // First use needs `npx playwright install webkit`; list in CROSS_BROWSER_SPECS.
      name: "webkit-zh",
      testMatch: CROSS_BROWSER_SPECS,
      use: { ...devices["Desktop Safari"], locale: "zh-CN", colorScheme: "light" },
    },
    {
      name: "chromium-zh",
      // Fix zh-CN locale: otherwise detectLocale() reads navigator.language → en-US → English page, missing Chinese headings.
      // Phase 1.7-F (P0-r3.2): colorScheme light forces OS prefers-color-scheme light branch.
      // Together with chat-composition-v8-fidelity.spec.ts localStorage.bowhead.theme=light, second safeguard
      // prevents OS-dark CI using dark tokens (sage palette), silently testing wrong fidelity baseline.
      use: { ...devices["Desktop Chrome"], locale: "zh-CN", colorScheme: "light" },
    },
    {
      name: "composer-en",
      // This second locale project belongs to the layout gate only. Without
      // the profile fence it also ran under the SSE mock, where the layout
      // fixtures intentionally have different runtime state.
      testMatch: E2E_PROFILE === "gate" || E2E_PROFILE === "all"
        ? ["**/composer-layout-v2.spec.ts"]
        : [],
      use: { ...devices["Desktop Chrome"], locale: "en-US", colorScheme: "light" },
    },
  ],
});
