/**
 * agent-feedback-loop batch 5 — **receipt cards really mount in chat**.
 *
 * Why this is needed: the card and data-fetching hook each have unit tests, but review mutation testing showed that
 * deleting the entire `<FeedbackReceiptCard/>` JSX block from `TurnContainer` left all 865 `components/chat` tests
 * **passing**. The most important outcome of this batch (receipts reaching users) had no coverage.
 *
 * Concrete failure: `TurnContainer.tsx` has 1500+ lines and is frequently refactored; moving this block into a
 * branch or deleting it leaves CI green and releases passing, while the entire batch produces no visible pixels for users —
 * exactly the recurrence of [[feedback_correct_but_out_of_reach]] (correct ≠ reachable by users),
 * obscured because the notification-center path still makes the feature appear present.
 *
 * Assert `data-testid`, not copy: this locks **wiring**, not rendering details
 * (copy belongs to the card's own tests), so copy changes should not fail it.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const listFeedbackReceipts = vi.fn();
vi.mock("@/lib/feedback-candidates-api", () => ({
  listFeedbackReceipts: (...a: unknown[]) => listFeedbackReceipts(...a),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  markNotificationRead: vi.fn().mockResolvedValue(undefined),
}));

// eslint-disable-next-line import/first
import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  listFeedbackReceipts.mockReset();
});

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: null,
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: null,
    sessionId: null,
    sessionTitle: null,
    running: false,
    started: false,
    messages: [],
    userAttachments: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    createToolOpen: null,
    subagentCreatorOpen: null,
    solidifyOpen: null,
    plan: null,
    ...over,
  };
}

const RECEIPT = {
  notification_id: "ntf_1",
  candidate_id: "fbc_1",
  target_label: "Example Data Analyst",
  final_state: "accepted" as const,
  decided_at: "2026-09-11T03:20:00+00:00",
  mine: true,
};

describe("TurnContainer × 回执卡接线", () => {
  it("有未读回执时卡片出现在对话里", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [RECEIPT] });
    render(<TurnContainer runner={runner()} />);
    await waitFor(() =>
      expect(screen.getByTestId("feedback-receipt-card")).toBeTruthy(),
    );
  });

  it("没有未读回执时不留任何痕迹（负对照：上一条不是恒真）", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [] });
    render(<TurnContainer runner={runner()} />);
    await waitFor(() => expect(listFeedbackReceipts).toHaveBeenCalled());
    expect(screen.queryByTestId("feedback-receipt-card")).toBeNull();
  });

  it("轮次跑动中不取数（`enabled=!runner.running`）", () => {
    listFeedbackReceipts.mockResolvedValue({ items: [RECEIPT] });
    render(<TurnContainer runner={runner({ running: true, turnId: "t_1" })} />);
    expect(listFeedbackReceipts).not.toHaveBeenCalled();
  });
});
