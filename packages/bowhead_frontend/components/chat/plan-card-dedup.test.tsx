/**
 * chat-ux-2026-08 · one card per plan — align live with replay.
 *
 * Observed on 116 prod 1.0.79 (`dogfood-116-prod-1.0.79-2026-08-09.md` §3):
 * after proposing a plan → **refreshing** → approving execution, the same card appeared **twice**:
 * proposal turn 1323px + execution turn「(执行已批准的计划)」1419px, identical text (same dependency graph,
 * step list, and progress count). **F5 left only one** (execution turn shrank to 256px, zero cards).
 * Thus **live ≠ replay**, violating CLAUDE.md's chat invariant.
 *
 * ⚠ The exact creation mechanism **was not identified**. Two independent observations proved the execution turn
 * **cannot legitimately own** that plan beat:
 *   ① Install an EventSource probe after refresh but before approval: execution stream had only
 *      `plan_running`/`plan_completed`, never `plan_proposed`;
 *   ② Fetch execution turn's **persisted** events from production API (59 events): again no `plan_proposed`.
 * `plan_proposed` is the sole trigger of `pushPlanBeat` anywhere in the repo.
 *
 * Pin **the invariant itself**, not a guessed path: regardless of the second card's source,
 * one plan has one card on its first-appearance turn — exactly replay behavior
 * and the intent declared by the plan-beat type comment in `turn-store.ts`:
 * the beat **lives on the proposal turn**.
 *
 * Fixtures must therefore **deliberately create the illegitimate shape** (the same plan beat on both turns);
 * a legitimate single-beat fixture would pin nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, applyCardCreated, pushPlanBeat } from "@/lib/turn-store";
import { _resetAllPlansForTesting, applyPlanProposed } from "@/lib/plan-store";
import { _clearAllToasts } from "@/lib/use-toast";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

const PLAN = "plan_dup01";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetAllPlansForTesting();
  _clearAllToasts();
  applyPlanProposed({
    plan_id: PLAN,
    title: "经营尽调",
    status: "running",
    steps: [
      { id: "ps_a", agent: "compute", objective: "查销量结构", level: 0, seq: 0, status: "running" },
    ],
  });
});
afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _resetAllPlansForTesting();
  _clearAllToasts();
  vi.restoreAllMocks();
});

function archivedTurn(turnId: string, user: string, withPlanBeat: boolean): ArchivedTurn {
  return {
    turnId,
    terminalStatus: "completed",
    user,
    userAttachments: [],
    answer: "",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: 1_700_000_000_000,
    beats: withPlanBeat ? [{ kind: "plan", planId: PLAN }] : [],
  } as unknown as ArchivedTurn;
}

function mount(archivedTurns: ArchivedTurn[]) {
  return renderIntl(
    <TurnContainer
      runner={
        {
          user: null, intent: null, complexity: null, chips: [], answer: "",
          artifact: null, source: null, summary: null, traceSteps: [],
          turnId: null, sessionId: "s_dup", sessionTitle: null,
          running: false, started: true, messages: [], userAttachments: [],
          thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "",
          clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
          solidifyOpen: null, plan: null, archivedTurns,
        } as unknown as TurnRunnerState
      }
    />,
  );
}

describe("计划卡去重 — 一个 plan 一张卡", () => {
  it("★ 两轮都带同一个 plan beat（116 实测形态）→ 只渲一张", () => {
    const { container } = mount([
      archivedTurn("t_propose", "对本商超做一次经营尽调", true),
      archivedTurn("t_exec", "(执行已批准的计划)", true), // ← Must not exist, but actually observed.
    ]);
    expect(container.querySelectorAll(".pcard").length).toBe(1);
  });

  it("★ 留下的那张落在**第一次出现**的那轮（= 重放的行为：提案轮）", () => {
    const { container } = mount([
      archivedTurn("t_propose", "对本商超做一次经营尽调", true),
      archivedTurn("t_exec", "(执行已批准的计划)", true),
    ]);
    const blocks = Array.from(container.querySelectorAll(".bh-bot-block"));
    expect(blocks.length).toBe(2);
    expect(blocks[0].querySelectorAll(".pcard").length).toBe(1); // Proposal turn.
    expect(blocks[1].querySelectorAll(".pcard").length).toBe(0); // Execution turn.
  });

  it("正常单轮不受影响（回归面：别把好路也改没）", () => {
    const { container } = mount([archivedTurn("t_propose", "尽调", true)]);
    expect(container.querySelectorAll(".pcard").length).toBe(1);
  });

  it("★ turn-store 有 beats、归档快照**没有**时不能误杀（收口 review 逮到的自伤）", () => {
    // `ArchivedTurnView` selects beats with `storeState?.beats ?? archived.beats ?? []`,
    // giving **turn-store priority**. If the index reads only archived.beats, a store populated after the archive snapshot
    // (reconcileOnMount / late replay) makes the renderer call renderPlan while the index
    // lacks this planId → planAllowed returns false → the entire card disappears.
    applyCardCreated({ turnId: "t_late" });
    pushPlanBeat("t_late", PLAN); // Only enters turn-store.
    const late = { ...archivedTurn("t_late", "提计划", false), beats: [] } as unknown as ArchivedTurn;
    const { container } = mount([late]);
    expect(container.querySelectorAll(".pcard").length).toBe(1);
  });

  it("两个**不同**的 plan 各渲各的（不能按 kind 一刀切去重）", () => {
    applyPlanProposed({
      plan_id: "plan_other",
      title: "另一个计划",
      status: "running",
      steps: [{ id: "ps_b", agent: "compute", objective: "另一件事", level: 0, seq: 0, status: "running" }],
    });
    const other = {
      ...archivedTurn("t_other", "第二个问题", false),
      beats: [{ kind: "plan", planId: "plan_other" }],
    } as unknown as ArchivedTurn;
    const { container } = mount([archivedTurn("t_propose", "尽调", true), other]);
    expect(container.querySelectorAll(".pcard").length).toBe(2);
  });
});
