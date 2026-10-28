import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { resetReportStatusStoreForTests } from "@/lib/report-status-store";
import { setAgentEvalAlignmentCapabilityForTest } from "@/lib/use-agent-eval-alignment-capability";

// The Coach alignment loop is behind FEATURE_AGENT_EVAL_ALIGNMENT. Suites run with it on, without a
// /health request; flag-off tests switch it off and this resets it after each test.
setAgentEvalAlignmentCapabilityForTest(true);

// vitest setup — i18n test locale default.
//
// Non-React modules localize via getCurrentLocale() (lib/locale.ts), whose
// resolution order is: <html lang> → NEXT_LOCALE cookie → localStorage →
// browser language (navigator.language / languages) → DEFAULT_LOCALE (en-US).
// Without a preference the product follows the browser language, so the suite
// runs as a Chinese browser: jsdom's navigator.language would otherwise be
// "en-US" while most copy assertions use the zh-CN catalog.
//
// Override only navigator.language — the LOWEST-priority source — so the default
// resolves to zh-CN, WITHOUT clobbering tests that set locale explicitly via
// <html lang>, cookie, or localStorage (e.g. tool-labels / chart-format which
// exercise both locales).
try {
  Object.defineProperty(window.navigator, "language", {
    value: "zh-CN",
    configurable: true,
  });
  Object.defineProperty(window.navigator, "languages", {
    value: ["zh-CN", "zh"],
    configurable: true,
  });
} catch {
  /* navigator may be non-configurable in some environments — best effort */
}

// Testing Library auto-cleanup detects global `afterEach` (see
// @testing-library/react/dist/index.js); this config uses `globals: false`, with no global
// afterEach, so auto-cleanup was never installed. Prior-test DOM, and timers/subscriptions in
// still-mounted components leak into the next test (under high load: `window is not defined`, duplicate
// testid, timeouts). Register cleanup explicitly here; existing manual cleanup calls are harmless.
afterEach(() => {
  cleanup();
  // Module-level report-status cache (shared across readers): never carry one test's status into the next.
  resetReportStatusStoreForTests();
  setAgentEvalAlignmentCapabilityForTest(true);
});
