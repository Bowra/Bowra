/**
 * chat-ux-2026-08 — collapsed-strip duration must be **wall time**, identical in live and archive.
 *
 * Observed on 116 (1.0.95): a plan turn actually running about 2 minutes showed
 * `✓ 10 步完成 · 1 次重试 · 15s`, read by users as 15 seconds.
 * `StepSummary` always used `deriveTotalDurationText` (sum of step durations);
 * this approximation **errs in both directions**: it omits thinking gaps between steps (undercount),
 * and sums concurrent peer durations (overcount).
 *
 * Persist backend `done.duration_ms` into turn-store `totalDurationMs`,
 * preferred by `StepSummary`. **Use turn-store, never `runner.summary.total`**:
 * `ArchivedTurn` (`lib/types.ts`) has no duration fields; using runner makes F5
 * change the number, violating CLAUDE.md's live/persisted/replayed consistency invariant.
 *
 * ⚠ Fixture shape determines whether this gate works:
 *   · Summed steps (3000 + 2000 = 5 seconds) **must differ from** wall time (124000ms = 2 minutes 4 seconds).
 *     Equal durations pass on either path, making this a dead guard.
 *   · Duration is localized; assert the **echoed key + parameters**
 *     (`summary_duration_min_sec:2,4` vs `summary_duration_sec:5`), not Chinese literals.
 *   · Feed **real wire shapes** through `dispatchToTurnStore` (never direct reducer calls),
 *     the shared live SSE/F5 replay route, to pin a path that actually occurs.
 *
 * Compare **two actual rendering surfaces** (live collapsed strip vs archived same turnId),
 * not function results: function assertions are always true here and cannot catch callers omitting props.
 *
 * Mutation verification (measured):
 *   ⑤ intl mock regresses to key-only, no parameters → all three **fail**, never silently pass.
 *      Key+parameter assertions make mock degradation fail loudly,
 *      rather than rendering 124s and 5s as the same string and silently making the gate vacuous.
 *   ① Revert `StepSummary` to `deriveTotalDurationText(steps)` → first two each fail (5 seconds).
 *   ② Remove dispatch's `done → applyTurnDuration` → same (store stays null).
 *   ③ Remove `totalDurationMs` prop only from TurnContainer's **archive** call →
 *      live == archived fails while `StepSummary` unit tests pass — precisely its purpose.
 *   ④ Negative control (last test): done without duration_ms must still show the sum.
 *      Remove fallback → it fails, proving preceding tests are not vacuous because no other number exists.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

// ⚠ This mock **must echo parameters**: duration uses i18n (`summary_duration_min_sec`);
// key-only output renders 124s and 5s identically, making every assertion below vacuous.
vi.mock("next-intl", () => {
  const echo = (key: string, vals?: Record<string, unknown>) =>
    vals ? `${key}:${Object.values(vals).filter((value) => typeof value !== "function").join(",")}` : key;
  return { useTranslations: () => Object.assign(echo, { rich: (key: string) => key }) };
});

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  vi.useRealTimers();
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

function archivedTurn(turnId: string): ArchivedTurn {
  return {
    turnId,
    terminalStatus: "completed",
    user: "各地区销售同比",
    userAttachments: [],
    answer: "华东领先。",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: Date.now(),
  } as ArchivedTurn;
}

/**
 * One real-wire turn: steps take 3.0s / 2.0s (sum = 5.0s), wall time 124s.
 * The gap is Lead thinking for two minutes between steps, the actual 116 turn shape.
 */
function seedTurn(turnId: string, doneOver: Record<string, unknown> = {}): void {
  dispatchToTurnStore("step_list_card_created", { card_id: turnId, seq: 1 });
  for (const [i, ms] of [3000, 2000].entries()) {
    const stepId = `s${i + 1}`;
    dispatchToTurnStore("step_started", {
      card_id: turnId,
      step_id: stepId,
      step_type: "tool_run",
      tool_name: "bi_query",
      header_zh: "查询销售",
      started_at: "2026-08-13T10:00:00Z",
      seq: 10 + i * 2,
    });
    dispatchToTurnStore("step_done", {
      card_id: turnId,
      step_id: stepId,
      state: "done",
      duration_ms: ms,
      summary: "234 行",
      seq: 11 + i * 2,
    });
  }
  dispatchToTurnStore("final_answer", { card_id: turnId, text: "华东领先。", seq: 20 });
  dispatchToTurnStore("step_list_card_finalized", {
    card_id: turnId,
    state: "completed",
    step_count: 2,
    seq: 21,
  });
  dispatchToTurnStore("done", {
    turn_id: turnId,
    card_id: turnId,
    status: "completed",
    duration_ms: 124_000,
    seq: 22,
    ...doneOver,
  });
}

/** Live current turn: completion → 600ms grace → collapsed strip. Return its duration text. */
async function liveFoldDurationText(turnId: string): Promise<string | null> {
  vi.useFakeTimers();
  const { container } = render(
    <TurnContainer
      runner={runner({ turnId, user: "各地区销售同比", running: false })}
    />,
  );
  await act(async () => {
    vi.advanceTimersByTime(700);
  });
  const summary = container.querySelector('[data-testid="step-summary"]');
  expect(summary, "live 轮没折叠成 StepSummary — fixture 没走到被测那条路").toBeTruthy();
  return summary!.querySelector('[data-testid="step-summary-duration"]')?.textContent ?? null;
}

/** Archive for the same turnId (after F5): collapsed by default. */
function archivedFoldDurationText(turnId: string): string | null {
  const { container } = render(
    <TurnContainer
      runner={runner({
        turnId: null,
        archivedTurns: [archivedTurn(turnId)],
      } as Partial<TurnRunnerState>)}
    />,
  );
  const summary = container.querySelector('[data-testid="step-summary"]');
  expect(summary, "归档轮没渲出 StepSummary — fixture 没走到被测那条路").toBeTruthy();
  return summary!.querySelector('[data-testid="step-summary-duration"]')?.textContent ?? null;
}

describe("折叠条时长 · 墙钟, 且 live == 归档", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("★ live 折叠条显示墙钟 124s, 不是各步耗时之和 5.0s", async () => {
    seedTurn("t-dur-live");
    const text = await liveFoldDurationText("t-dur-live");
    expect(text).toContain("summary_duration_min_sec:2,4");   // 124000ms
    expect(text).not.toContain("summary_duration_sec:5");     // Sum 5000ms.
  });

  it("★ 归档折叠条显示同一个墙钟(F5 后数字不变)", () => {
    seedTurn("t-dur-arch");
    const text = archivedFoldDurationText("t-dur-arch");
    expect(text).toContain("summary_duration_min_sec:2,4");   // 124000ms
    expect(text).not.toContain("summary_duration_sec:5");     // Sum 5000ms.
  });

  it("★ 同一个 turn 的两个渲染面, 时长字符串必须逐字相同", async () => {
    const turnId = "t-dur-both";
    seedTurn(turnId);

    const live = await liveFoldDurationText(turnId);
    cleanup();
    vi.useRealTimers();
    const archived = archivedFoldDurationText(turnId);

    expect(live).toBeTruthy();
    expect(live).toBe(archived);
  });

  it("负对照：done 不带 duration_ms 时退回求和(兜底仍活着)", async () => {
    // Old archived done events may lack this field. Prefer an approximation to no number,
    // but only as that **explicit fallback**, never the main path.
    seedTurn("t-dur-legacy", { duration_ms: undefined });
    const text = await liveFoldDurationText("t-dur-legacy");
    expect(text).toContain("summary_duration_sec:5");         // Sum 5000ms.
    expect(text).not.toContain("summary_duration_min_sec:2,4");
  });
});

/** A turn that failed after two steps: one done, one failed, then a failed terminal. */
function seedFailedTurn(turnId: string): void {
  dispatchToTurnStore("step_list_card_created", { card_id: turnId, seq: 1 });
  for (const [i, state] of (["success", "failure"] as const).entries()) {
    const stepId = `f${i + 1}`;
    dispatchToTurnStore("step_started", {
      card_id: turnId, step_id: stepId, step_type: "tool_run", tool_name: "bi.heavy_compute",
      header_zh: "查询销售", started_at: "2026-08-13T10:00:00Z", seq: 10 + i * 2,
    });
    dispatchToTurnStore("step_done", { card_id: turnId, step_id: stepId, state, duration_ms: 2000, seq: 11 + i * 2 });
  }
  dispatchToTurnStore("step_list_card_finalized", { card_id: turnId, state: "failed", step_count: 2, seq: 21 });
  dispatchToTurnStore("done", { turn_id: turnId, card_id: turnId, status: "failed", duration_ms: 124_000, seq: 22 });
}

const FAILURE = { source: "turn_failed", reasonCode: "wallclock_exceeded" } as const;

describe("a failed turn folds into one 'Not finished' line (spec §8), live and archived alike", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it("live: the failed turn shows the failed summary instead of the expanded trace", async () => {
    seedFailedTurn("t-fail-live");
    const { container } = render(
      <TurnContainer runner={runner({ turnId: "t-fail-live", user: "各地区销售同比", running: false, errorReason: FAILURE })} />,
    );
    await act(async () => {});
    const summary = container.querySelector('[data-testid="step-summary"]');
    expect(summary?.getAttribute("data-state")).toBe("failed");
    expect(summary?.textContent).toContain("summary_not_finished");
    expect(container.querySelector(".bh-step-list")).toBeNull();
  });

  it("archived: folded by default with the same line, expandable to the trace", async () => {
    seedFailedTurn("t-fail-arch");
    const turn = { ...archivedTurn("t-fail-arch"), terminalStatus: "failed", answer: "", errorReason: FAILURE } as ArchivedTurn;
    const { container } = render(
      <TurnContainer runner={runner({ turnId: null, archivedTurns: [turn] } as Partial<TurnRunnerState>)} />,
    );
    const summary = container.querySelector<HTMLElement>('[data-testid="step-summary"]');
    expect(summary?.getAttribute("data-state")).toBe("failed");
    expect(summary?.querySelector('[data-testid="step-summary-progress"]')?.textContent).toBe(" · summary_steps_count:1");
    expect(summary?.querySelector('[data-testid="step-summary-duration"]')?.textContent).toContain("summary_duration_min_sec:2,4");
    await act(async () => { summary!.click(); });
    expect(container.querySelector('[data-testid="step-summary"]')).toBeNull();
  });
});
