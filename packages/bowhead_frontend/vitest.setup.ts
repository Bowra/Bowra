import { afterEach, beforeEach } from "vitest";

import { resetReportStatusStoreForTests } from "@/lib/report-status-store";

const hasDOM = typeof window !== "undefined";
const cleanup = hasDOM ? (await import("@testing-library/react")).cleanup : undefined;

if (hasDOM) {
  // Load after test mocks are registered; this hook imports the authenticated session tree.
  beforeEach(async () => {
    const { setAgentEvalAlignmentCapabilityForTest } = await import("@/lib/use-agent-eval-alignment-capability");
    setAgentEvalAlignmentCapabilityForTest(true);
  });

  // Browser tests default to Chinese without overriding explicit document, cookie or storage preferences.
  try {
    Object.defineProperty(window.navigator, "language", { value: "zh-CN", configurable: true });
    Object.defineProperty(window.navigator, "languages", { value: ["zh-CN", "zh"], configurable: true });
  } catch {
    // Some browser environments expose non-configurable language properties.
  }
}

// globals: false prevents Testing Library from registering automatic cleanup.
afterEach(async () => {
  cleanup?.();
  resetReportStatusStoreForTests();
  if (hasDOM) {
    const { setAgentEvalAlignmentCapabilityForTest } = await import("@/lib/use-agent-eval-alignment-capability");
    setAgentEvalAlignmentCapabilityForTest(true);
  }
});
