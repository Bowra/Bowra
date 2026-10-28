/**
 * A scheduled model retry is the band's current action until it clears.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { modelRetryMessageKey, RunningBand } from "./RunningBand";
import type { StepView } from "./types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).join(",")}` : key,
}));

afterEach(cleanup);

const steps: StepView[] = [];

function actionText(container: HTMLElement): string {
  return container.querySelector(".bh-band__act")?.textContent ?? "";
}

describe("model retry copy", () => {
  it.each([
    ["timeout", "band_model_retry_timeout"],
    ["output_limit", "band_model_output_recovery"],
    ["network", "band_model_retry_network"],
    ["rate_limited", "band_model_retry_unavailable"],
    ["upstream", "band_model_retry_unavailable"],
    ["unknown", "band_model_retry_unavailable"],
  ])("%s → %s", (reason, key) => {
    expect(modelRetryMessageKey(reason)).toBe(key);
  });
});

describe("RunningBand with a pending retry", () => {
  it("shows the retry with its count as the current action, ahead of the paused copy", () => {
    const { container } = render(
      <RunningBand
        steps={steps}
        running
        paused
        modelRetry={{ retry: 2, maxRetries: 3, reason: "timeout" }}
      />,
    );
    expect(actionText(container)).toBe("band_model_retry_timeout:2,3");
    expect(container.querySelector("[data-model-retry]")).not.toBeNull();
  });

  it("returns to the ordinary action once the status clears", () => {
    const { container, rerender } = render(
      <RunningBand steps={steps} running modelRetry={{ retry: 1, maxRetries: 3, reason: "network" }} />,
    );
    expect(actionText(container)).toBe("band_model_retry_network:1,3");
    rerender(<RunningBand steps={steps} running modelRetry={null} />);
    expect(actionText(container)).not.toContain("band_model_retry");
    expect(container.querySelector("[data-model-retry]")).toBeNull();
  });
});
