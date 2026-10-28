/**
 * Batch 1 completion — disclosure-strip rendering conditions.
 *
 * ★★ Lock a real regression: the disclosure strip and candidate cards previously **both** lived under
 * `memCands.items.length > 0`. The zero-click direct-write tier **produces no candidates**,
 * so disclosure of what this turn automatically remembered / why it remembered nothing never rendered; it only appeared
 * when it should not (with candidate cards).
 *
 * Component-level cases (memory-disclose.test.tsx) cannot prove this: the bug lies outside the component,
 * in the **call-site condition**. Testing functions without wiring has failed twice in this repository, so render the real
 * TurnContainer here.
 *
 * Mutation guard: restore the outer condition to `memCandMode !== "off" && memCands.items.length > 0`
 * ⇒ the first two cases must fail.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import { _clearAllToasts } from "@/lib/use-toast";
import type { TurnRunnerState, ArchivedTurn } from "@/lib/types";

const memCandsMock = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));
vi.mock("@/lib/use-memory-candidates", () => ({
  useMemoryCandidates: () => memCandsMock.current,
}));

import { TurnContainer } from "./TurnContainer";

const _base = {
  items: [],
  actingId: null,
  decide: vi.fn(),
  redraft: vi.fn(),
  entries: [],
  explicitRequest: null,
  undoEntry: vi.fn(),
};

beforeEach(() => {
  _resetAllTurnsForTesting();
  _clearAllToasts();
  memCandsMock.current = { ..._base };
});
afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _clearAllToasts();
  vi.restoreAllMocks();
});

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: "上个问题", intent: null, complexity: null, chips: [], answer: "答案",
    artifact: null, source: null, summary: null, traceSteps: [],
    turnId: "t_now", sessionId: "s_1", sessionTitle: null,
    running: false, started: true, messages: [], userAttachments: [],
    thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "",
    clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
    solidifyOpen: null, plan: null, terminalStatus: "completed",
    archivedTurns: [] as ArchivedTurn[],
    ...over,
  } as TurnRunnerState;
}

const mount = () => renderIntl(<TurnContainer runner={runner()} />);

describe("披露条的渲染条件 — 不再搭候选卡的便车", () => {
  it("★★ 自动记了一条、但本轮没有候选卡 ⇒ 披露条仍须出现", () => {
    memCandsMock.current = {
      ..._base,
      entries: [
        {
          id: "mem_1",
          kind: "preference",
          key: "ranking_chart_type",
          display_value: "排名图用横向条形图",
          origin: "capture",
          state: "remembered",
          undoable: true,
          candidate_id: null,
        },
      ],
    };
    mount();
    expect(screen.getByTestId("memory-disclose")).toBeTruthy();
    expect(screen.getByTestId("memory-disclose-undo-mem_1")).toBeTruthy();
  });

  it("★★ 「没记住，原因是…」也一样：没有候选卡照样要说出来", () => {
    memCandsMock.current = {
      ..._base,
      explicitRequest: {
        source: "language_setting",
        outcome: "not_remembered",
        reason: "trigger_language_setting",
      },
    };
    mount();
    expect(screen.getByTestId("memory-disclose-notice")).toBeTruthy();
  });

  it("负对照：既没记也没原因 ⇒ 一个字都不画（绝大多数轮是这一档）", () => {
    mount();
    expect(screen.queryByTestId("memory-disclose")).toBeNull();
  });

  it("★ capture not_remembered + an nl_intent card for the same turn ⇒ no 「没有记住」", () => {
    memCandsMock.current = {
      ..._base,
      items: [
        {
          id: "cand_nl",
          userId: "u1",
          kind: "preference",
          key: "fiscal_year",
          value: "财年从7月开始",
          reason: null,
          sourceTurnId: "t_now",
          status: "pending",
          decidedBy: null,
          decidedAt: null,
          createdAt: "2026-10-06T00:00:00Z",
          updatedAt: "2026-10-06T00:00:00Z",
        },
      ],
      explicitRequest: {
        source: "capture",
        outcome: "not_remembered",
        reason: "draft_says_not_lasting",
      },
    };
    mount();
    expect(screen.getByTestId("memory-candidate-card")).toBeTruthy();
    expect(screen.queryByTestId("memory-disclose-notice")).toBeNull();
    expect(screen.queryByText("没有记住")).toBeNull();
  });

  it("★ an approved candidate settles on the card after a refresh, undone state included", () => {
    // No local card state exists after a refresh: the receipt entry alone must restore 「已撤销」.
    memCandsMock.current = {
      ..._base,
      entries: [
        {
          id: "mem_9",
          kind: "preference",
          key: "fiscal_year",
          display_value: "财年从7月开始",
          origin: "candidate",
          state: "undone",
          undoable: false,
          candidate_id: "cand_9",
        },
      ],
    };
    mount();
    expect(screen.queryByTestId("memory-disclose")).toBeNull();
    const settled = screen.getByTestId("memory-candidate-settled-cand_9");
    expect(settled.getAttribute("data-state")).toBe("undone");
    expect(settled.textContent).toContain("已撤销");
    expect(screen.queryByTestId("memory-candidate-undo-cand_9")).toBeNull();
  });
});
