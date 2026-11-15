/**
 * A tool call waiting for its requester is the band's current action, ahead of the
 * "still waiting on the model" pause (116 dogfood, 2026-11-14).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { StepConfirmation } from "@/lib/turn-store";

import { RunningBand } from "./RunningBand";
import type { StepView } from "./types";

const viewer = vi.hoisted(() => ({ id: "u_owner" }));

vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (key: string) => key,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { id: viewer.id } }),
}));

afterEach(cleanup);

function waitingStep(over: Partial<StepConfirmation> = {}): StepView {
  const confirmation: StepConfirmation = {
    id: "esc_one",
    kind: "email_send",
    requesterUserId: "u_owner",
    title: "Weekly update",
    sourceLabel: "a@example.com",
    category: null,
    choices: ["reject", "approve"],
    expiresAt: "2026-11-14T10:03:00Z",
    receivedAt: Date.now(),
    ...over,
  };
  return {
    stepId: "step_call_1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "send_email",
    headerZh: "发送邮件",
    state: "active",
    confirmation,
  } as unknown as StepView;
}

function actionText(container: HTMLElement): string {
  return container.querySelector(".bh-band__act")?.textContent ?? "";
}

describe("RunningBand while a tool call waits for confirmation", () => {
  it("tells the requester the task waits for them, not for the model", () => {
    viewer.id = "u_owner";
    const { container } = render(<RunningBand steps={[waitingStep()]} running paused />);
    expect(actionText(container)).toBe("band_waiting_confirmation");
  });

  it("tells another viewer the task waits for the requester", () => {
    viewer.id = "u_other";
    const { container } = render(<RunningBand steps={[waitingStep()]} running paused />);
    expect(actionText(container)).toBe("band_waiting_requester");
  });

  it("returns to the ordinary action once the confirmation is decided", () => {
    viewer.id = "u_owner";
    const { container } = render(<RunningBand steps={[waitingStep({ outcome: "rejected" })]} running paused />);
    expect(actionText(container)).toBe("band_paused");
  });
});
