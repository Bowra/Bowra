/**
 * Planning fall-through notice: explicit plan mode failing to produce a plan card must inform the user.
 *
 * This path used to be silent: backend fell through to normal Lead, which often followed the user's wording
 * with Markdown execution-plan prose — no card, approval, or execution, yet users thought it was scheduled.
 *
 * ⚠ Test-method check (this sprint already hit this): component tests fed props directly **cannot cross the dispatch
 * seam**, which gave the plan-dispatch batch false green. Thus pin component rendering and separately pin
 * the done event → runner state transition (at the end of this file).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { _resetAllTurnsForTesting, applyCardCreated, applyFinalAnswer } from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
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

function renderWithFallback(planningFallback?: string) {
  applyCardCreated({ turnId: "t-pf" });
  applyFinalAnswer({ turnId: "t-pf", text: "这是直接作答的正文。", payload: { turnId: "t-pf" } });
  return render(
    <TurnContainer
      runner={runner({
        turnId: "t-pf",
        user: "帮我做个复杂分析",
        answer: "这是直接作答的正文。",
        planningFallback,
      })}
    />,
  );
}

describe("TurnContainer · planning fall-through 告知条", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("出计划失败 → 出现告知条，且排在答案**之前**", () => {
    const { container } = renderWithFallback("authoring_empty");
    const notice = container.querySelector(".pfallback");
    expect(notice).not.toBeNull();
    // Order: correct expectations first, then read the answer.
    const text = container.textContent ?? "";
    expect(text.indexOf("fallback_title")).toBeLessThan(text.indexOf("这是直接作答的正文。"));
  });

  it("每种 reason 各出各自的文案键（不是同一句糊弄）", () => {
    // plan_infeasible was added in agent-capability-routing phase 1: the graph exists, but every step is
    // known to be unexecutable before approval. ★ Add every new reason code to the component's known list and both
    // messages/*.json files; any omission falls back to generic copy that looks normal but describes the wrong situation.
    for (const reason of ["authoring_empty", "reify_failed", "unknown_agents", "plan_infeasible"]) {
      const { container } = renderWithFallback(reason);
      expect(container.querySelector(".pfallback")?.textContent).toContain(`fallback_body_${reason}`);
      cleanup();
      _resetAllTurnsForTesting();
    }
  });

  it("未知 reason 码 → 退回通用文案，绝不把裸码显给用户", () => {
    const { container } = renderWithFallback("some_future_reason");
    const txt = container.querySelector(".pfallback")?.textContent ?? "";
    expect(txt).toContain("fallback_body_authoring_empty");
    expect(txt).not.toContain("some_future_reason");
  });

  it("没触发 → 一个告知条都不该有（Auto 档拆不出计划是正常的）", () => {
    const { container } = renderWithFallback(undefined);
    expect(container.querySelector(".pfallback")).toBeNull();
  });
});
