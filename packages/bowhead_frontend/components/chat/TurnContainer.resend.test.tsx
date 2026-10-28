import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { TurnRunnerState } from "@/lib/types";
import type { RecoveryAction } from "@/lib/turn-feedback";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
const toast = vi.fn();
vi.mock("@/lib/use-toast", () => ({ toast: (...args: unknown[]) => toast(...args) }));

import { TurnContainer } from "./TurnContainer";

afterEach(() => { cleanup(); toast.mockReset(); });

it("shows an unavailable saved-answer continuation without an automatic retry", () => {
  const state = turn("clarification_resume_unavailable", "copy_diagnostics");
  state.terminalStatus = "clarification_pending";
  state.errorReason!.source = "clarification_resume";
  const send = vi.fn();
  const retry = vi.fn();
  const { container } = render(<TurnContainer runner={state} onSend={send} onRetryTurn={retry} />);
  expect(container.querySelector('[data-testid="final-fail"][data-state="clarification_resume_unavailable"]')).not.toBeNull();
  expect(screen.queryByRole("button", { name: "final_fail_retry" })).toBeNull();
  expect(send).not.toHaveBeenCalled();
  expect(retry).not.toHaveBeenCalled();
});

function turn(reasonCode?: string, recoveryAction?: RecoveryAction): TurnRunnerState {
  return {
    user: "计算 ROE", intent: null, complexity: null, chips: [], answer: "结果", artifact: null, source: null,
    summary: null, traceSteps: [], turnId: "t_reference", sessionId: "s1", sessionTitle: null,
    running: false, started: true, messages: [], userAttachments: [], thinking: "", thinkingLength: 0,
    lastThinkingHintAttachedAt: "", clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
    solidifyOpen: null, plan: null,
    errorReason: reasonCode ? { source: "turn_failed", reasonCode, recoveryAction } : null,
    terminalStatus: reasonCode ? "failed" : "completed",
  };
}

it("failed turns return to the composer instead of silently losing references", () => {
  const send = vi.fn();
  const prefill = vi.fn();
  // `retry` without a replay path on this surface degrades to editing the question (D5).
  render(<TurnContainer runner={turn("lead_internal_error", "retry")} onSend={send} onComposerPrefill={prefill} />);
  fireEvent.click(screen.getByRole("button", { name: "msg_resend" }));
  expect(prefill).toHaveBeenCalledWith("计算 ROE");
  expect(send).not.toHaveBeenCalled();
  expect(toast).not.toHaveBeenCalled();
});

it("completed turns also require editing before sending again", () => {
  const send = vi.fn();
  const prefill = vi.fn();
  render(<TurnContainer runner={turn()} onSend={send} onComposerPrefill={prefill} />);
  fireEvent.click(screen.getByRole("button", { name: "msg_resend" }));
  expect(prefill).toHaveBeenCalledWith("计算 ROE");
  expect(send).not.toHaveBeenCalled();
});

it("policy changes show the specific reason and do not auto-send", () => {
  const send = vi.fn();
  const prefill = vi.fn();
  const { container } = render(<TurnContainer runner={turn("reference_policy_changed", "edit_and_resend")} onSend={send} onComposerPrefill={prefill} />);
  expect(container.querySelector('[data-testid="final-fail"][data-state="reference_policy_changed"]')).not.toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "msg_resend" }));
  expect(prefill).toHaveBeenCalledWith("计算 ROE");
  expect(send).not.toHaveBeenCalled();
});

it("failure-card Retry uses the latest turn's replay path when the surface provides it", () => {
  const retry = vi.fn();
  const prefill = vi.fn();
  render(<TurnContainer runner={turn("lead_internal_error", "retry")} onComposerPrefill={prefill} onRetryTurn={retry} />);
  fireEvent.click(screen.getByRole("button", { name: "final_fail_retry" }));
  expect(retry).toHaveBeenCalledOnce();
  expect(prefill).not.toHaveBeenCalled();
});

it("announces the failure card only when the failure arrived on the live stream", () => {
  // Hydrated or replayed failure (F5): already settled, no alert.
  const hydrated = render(<TurnContainer runner={turn("lead_internal_error", "retry")} />);
  expect(hydrated.container.querySelector('[data-testid="final-fail"] [role="alert"]')).toBeNull();
  hydrated.unmount();
  // The same turn running on this page, then failing: announced.
  const running: TurnRunnerState = { ...turn(), answer: "", running: true, terminalStatus: undefined };
  const live = render(<TurnContainer runner={running} />);
  live.rerender(<TurnContainer runner={turn("lead_internal_error", "retry")} />);
  expect(live.container.querySelector('[data-testid="final-fail"] [role="alert"]')).not.toBeNull();
});
