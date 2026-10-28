/**
 * chat-ux-2026-08 (invariant inventory §2.11) — one turn has **one** Step → StepView projection.
 *
 * Found: two coexisting copies with identical fields, differing only in `uiKind`.
 *   · `TurnContainer.collectStepViews`: main live path (beats present), **passes error through unchanged**
 *   · `StepListCardConnected.stepToStepView`: archive / F5 path,
 *     adds `uiKind: inferFailureUiKind(error)`
 *
 * `inferFailureUiKind` maps both `needs_user_input` and **`unknown`** to `"transient"`
 * (deliberate safe default in turn-store.ts), while `isRecoverableStepFailure` reads
 * `error.uiKind ?? error.kind`. For the same failed step with `kind:"unknown"`:
 *
 *   live → no uiKind → **terminal** → red ✕ `is-fail` + always forced open + collapsed strip **does not** count retries
 *   F5 → uiKind="transient" → **self-healing** → amber ⚠ `is-part` + collapsed strip counts one retry
 *
 * Refresh changing the color of identical data directly violates CLAUDE.md's live/persisted/replayed consistency invariant.
 *
 * ⚠ Both fixture layers are necessary (final review required the second):
 *
 *   ① **Inject `kind:"unknown"` directly with `applyStepDone`**: the only sample making both projections disagree
 *      (`transient` / `permanent` agree in both, so those samples leave this gate always green).
 *      This **bypasses the wire normalizer**: `sse-events.ts:370`'s `normalizeStepError` currently
 *      hard-codes `kind: isPermanent ? "permanent" : "transient"`, preventing unknown from reaching the store.
 *      This layer pins the **type contract** (`StepError.kind` declares four values), not today's production shape.
 *   ② **Real ingest**: `dispatchToTurnStore("step_done", …)` receives the wire shape
 *      `{type, message_zh, is_transient}`. This pins live/replay sharing **today's actual path**,
 *      without which only unreachable situations would be guarded.
 *
 * Compare **two actual rendering surfaces** (live current turn vs archived same turnId), not function return values:
 * after projection consolidation, function-level assertions are always true and cannot catch someone inlining another copy.
 *
 * Mutation verified: restore hand-written `collectStepViews`, passing `error: s.error` unchanged;
 * both layer ① tests fail, while permanent negative control and layer ② stay green (equivalent mutations:
 * normalizer narrowing already makes both projections agree on that path, and this must be recorded honestly).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyStepDone,
  applyStepStarted,
} from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

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

/** Backend does not guarantee `kind` is only transient/permanent: `unknown` is a declared valid value
 *  (`step-list/types.ts`'s `StepError.kind`). */
function seedUnknownKindFailure(turnId: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({
    turnId,
    stepId: "s1",
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询",
    startedAt: new Date(),
  });
  applyStepDone({
    turnId,
    stepId: "s1",
    state: "failed",
    durationMs: 1200,
    error: { kind: "unknown", message: "上游返回了没见过的东西" },
  });
}

function archivedTurn(turnId: string): ArchivedTurn {
  return {
    turnId,
    terminalStatus: "completed",
    user: "查询",
    userAttachments: [],
    answer: "结论。",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: Date.now(),
  } as ArchivedTurn;
}

/** Rendered failure state: `is-fail` (red/terminal) or `is-part` (amber/self-healing). */
function stepStatusClass(container: HTMLElement): string {
  if (container.querySelector(".stp.is-fail")) return "is-fail";
  if (container.querySelector(".stp.is-part")) return "is-part";
  return "(none)";
}

/** Archived turns default to collapsed; open the summary strip to see step rows. */
function renderArchived(turnId: string): HTMLElement {
  const { container } = render(
    <TurnContainer
      runner={runner({
        turnId: null,
        archivedTurns: [archivedTurn(turnId)],
      } as Partial<TurnRunnerState>)}
    />,
  );
  const summary = container.querySelector('[data-testid="step-summary"]');
  if (summary) fireEvent.click(summary as HTMLElement);
  return container;
}

describe("Step → StepView 投影 · live 与重放必须同源", () => {
  beforeEach(() => _resetAllTurnsForTesting());

  it('★ kind:"unknown" 的失败步：live 与归档渲出**同一个**状态 class', () => {
    seedUnknownKindFailure("t-proj");

    // Main live path: step beat → BeatStream → TurnBeatFlow → collectStepViews.
    const live = render(
      <TurnContainer
        runner={runner({ turnId: "t-proj", user: "查询", running: false })}
      />,
    );
    const liveClass = stepStatusClass(live.container);

    cleanup();

    // Replay path: ArchivedTurnView → stepToStepView (the same turn-store state).
    const replayClass = stepStatusClass(renderArchived("t-proj"));

    expect(liveClass).not.toBe("(none)");
    expect(replayClass).not.toBe("(none)");
    expect(liveClass).toBe(replayClass);
  });

  it("投影结果本身要判成「会自愈」（uiKind 的安全默认没被丢掉）", () => {
    seedUnknownKindFailure("t-proj-heal");

    const { container } = render(
      <TurnContainer
        runner={runner({ turnId: "t-proj-heal", user: "查询", running: false })}
      />,
    );

    expect(container.querySelector(".stp.is-part")).not.toBeNull();
    expect(container.querySelector(".stp.is-fail")).toBeNull();
  });

  it("★ 走真实 ingest（wire → normalizeStepError → store）：live 与归档仍同源", () => {
    // This does not call applyStepDone; feed the backend's actual wire shape, which today's normalizer
    // narrows to kind:"transient", the **shape actually seen in production**.
    const turnId = "t-proj-wire";
    applyCardCreated({ turnId });
    applyStepStarted({
      turnId,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      startedAt: new Date(),
    });
    dispatchToTurnStore("step_done", {
      card_id: turnId,
      step_id: "s1",
      state: "failed",
      duration_ms: 1200,
      error: {
        type: "tool_exception",
        message_zh: "上游超时",
        is_transient: true,
      },
    });

    const live = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: false })} />,
    );
    const liveClass = stepStatusClass(live.container);

    cleanup();

    const replayClass = stepStatusClass(renderArchived(turnId));

    expect(liveClass).not.toBe("(none)");
    expect(liveClass).toBe(replayClass);
    // is_transient:true → normalize to transient → self-healing → amber, not red.
    expect(liveClass).toBe("is-part");
  });

  it("负对照：permanent 在两条路径下都仍是终态红（别把上面两条做成恒真）", () => {
    const turnId = "t-proj-perm";
    applyCardCreated({ turnId });
    applyStepStarted({
      turnId,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      startedAt: new Date(),
    });
    applyStepDone({
      turnId,
      stepId: "s1",
      state: "failed",
      durationMs: 800,
      error: { kind: "permanent", message: "口径不存在" },
    });

    const live = render(
      <TurnContainer runner={runner({ turnId, user: "查询", running: false })} />,
    );
    expect(stepStatusClass(live.container)).toBe("is-fail");

    cleanup();

    expect(stepStatusClass(renderArchived(turnId))).toBe("is-fail");
  });
});
