/**
 * turn-store-replay — F5 hydrate replay path tests.
 *
 * Locks the chat-polish-r3 Phase 1 B1 fix: an archived turn's steps must
 * match what live SSE produced. The replay path feeds the persisted event
 * log through dispatchToTurnStore — same routing live SSE uses — so the
 * turn-store ends up equivalent to a live run.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  _resetAllTurnsForTesting,
  getTurnSnapshot,
} from "./turn-store";
import { _resetNarrativeCursorsForTesting } from "./turn-store-dispatch";
import { replayTurnEventRows, replayTurnEvents } from "./turn-store-replay";

const T = "turn-replay";

const ORIGINAL_FETCH = globalThis.fetch;

function mockFetchEvents(events: Array<{ seq: number; event_type: string; payload: unknown; occurred_at: string }>) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    return new Response(
      JSON.stringify({ turn_id: T, events }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }) as unknown as typeof fetch;
}

function mockFetchFailure(status: number) {
  globalThis.fetch = vi.fn(async () => {
    return new Response("error", { status });
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

describe("replayTurnEvents", () => {
  it("retains rejected-narrative suppression when resumed SSE omits the old paused done", () => {
    replayTurnEventRows(T, [
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      { seq: 2, event_type: "narrative.delta", payload: { card_id: T, delta: "Malformed question protocol" }, occurred_at: "" },
      {
        seq: 3,
        event_type: "clarification_required",
        payload: { turn_id: T, clarification_id: "original-card", candidates: [], fallback_text: "Which title?", narrative_superseded: true },
        occurred_at: "",
      },
    ]);
    expect(getTurnSnapshot(T)?.narrativeSuperseded).toBe(true);
    replayTurnEventRows(T, [
      { seq: 5, event_type: "turn.resumed", payload: { turn_id: T, request_id: "original-card" }, occurred_at: "" },
      { seq: 6, event_type: "final_answer", payload: { card_id: T, text: "The original task is complete." }, occurred_at: "" },
      { seq: 7, event_type: "done", payload: { turn_id: T, status: "completed" }, occurred_at: "" },
    ]);
    expect(getTurnSnapshot(T)?.narrativeSuperseded).toBe(true);
  });

  it("replays prefetched rows without issuing a second events request", () => {
    const spy = vi.fn();
    globalThis.fetch = spy as unknown as typeof fetch;
    const result = replayTurnEventRows(T, [
      {
        seq: 1,
        event_type: "step_list_card_created",
        payload: { card_id: T },
        occurred_at: "2026-05-26T00:00:00Z",
      },
    ]);

    expect(result).toEqual({ turnId: T, eventCount: 1, claimed: 1 });
    expect(getTurnSnapshot(T)?.cardCreated).toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });

  it("returns zero events on empty turnId without making a request", async () => {
    const spy = vi.fn();
    globalThis.fetch = spy as unknown as typeof fetch;
    const result = await replayTurnEvents("");
    expect(result.eventCount).toBe(0);
    expect(result.claimed).toBe(0);
    expect(spy).not.toHaveBeenCalled();
  });

  it("hydrates turn-store from a step_list_card_created + step_started + step_done sequence", async () => {
    mockFetchEvents([
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "2026-05-26T00:00:00Z" },
      {
        seq: 2,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "s1",
          step_type: "tool_run",
          header_zh: "查询数据",
          started_at: "2026-05-26T00:00:01Z",
          tool_name: "bi_query",
        },
        occurred_at: "2026-05-26T00:00:01Z",
      },
      {
        seq: 3,
        event_type: "step_done",
        payload: {
          card_id: T,
          step_id: "s1",
          step_type: "tool_run",
          state: "success",
          duration_ms: 1234,
          output_preview: "3 行 · 2 列",
        },
        occurred_at: "2026-05-26T00:00:02Z",
      },
    ]);

    const result = await replayTurnEvents(T);
    expect(result.eventCount).toBe(3);
    expect(result.claimed).toBe(3);

    const snap = getTurnSnapshot(T);
    expect(snap?.cardCreated).toBe(true);
    expect(snap?.steps.length).toBe(1);
    expect(snap?.steps[0].stepId).toBe("s1");
    expect(snap?.steps[0].state).toBe("done");
    expect(snap?.steps[0].summary).toBe("3 行 · 2 列");
  });

  it("hydrates the same sandbox output refs used by the live reducer", async () => {
    mockFetchEvents([
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      {
        seq: 2,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "sandbox-output",
          step_type: "tool_run",
          header_zh: "运行代码",
          started_at: "2026-08-06T00:00:00Z",
          tool_name: "python_sandbox",
          code_input: {
            language: "python",
            code: "print('replayed')",
            truncated: false,
          },
        },
        occurred_at: "",
      },
      {
        seq: 3,
        event_type: "step_done",
        payload: {
          card_id: T,
          step_id: "sandbox-output",
          step_type: "tool_run",
          state: "success",
          duration_ms: 551,
          result_inline: null,
          result_meta: {
            output_artifacts: [
              { artifact_id: "art_stdout", filename: "sandbox-stdout.txt", data_role: "stdout" },
              { artifact_id: "art_calc", filename: "calc.md" },
            ],
          },
        },
        occurred_at: "",
      },
    ]);

    await replayTurnEvents(T);

    expect(getTurnSnapshot(T)?.steps[0]?.outputArtifacts).toEqual([
      { artifactId: "art_stdout", filename: "sandbox-stdout.txt", dataRole: "stdout" },
      { artifactId: "art_calc", filename: "calc.md" },
    ]);
    expect(getTurnSnapshot(T)?.steps[0]?.codeInput).toEqual({
      language: "python",
      code: "print('replayed')",
      truncated: false,
    });
  });

  it("hydrates the same typed Power BI failure used by live dispatch", async () => {
    mockFetchEvents([
      {
        seq: 1,
        event_type: "step_list_card_created",
        payload: { card_id: T },
        occurred_at: "",
      },
      {
        seq: 2,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "pbi1",
          step_type: "tool_run",
          header_zh: "查询 Power BI",
          started_at: "2026-05-26T00:00:01Z",
          tool_name: "pbi_execute_dax",
        },
        occurred_at: "",
      },
      {
        seq: 3,
        event_type: "step_done",
        payload: {
          card_id: T,
          step_id: "pbi1",
          step_type: "tool_run",
          state: "failure",
          duration_ms: 20,
          error: {
            type: "pbi",
            message_zh: "legacy provider prose",
            is_transient: true,
            code: "pbi_query_unreachable",
            diagnostics: {
              schema_version: 1,
              disclosure: "authenticated",
              diagnostic_code: "powerbi_query_failure",
              safe_detail: "phase=execute; failure_class=network",
              retryable: true,
            },
          },
        },
        occurred_at: "",
      },
    ]);

    await replayTurnEvents(T);

    expect(getTurnSnapshot(T)?.steps[0].error).toMatchObject({
      code: "pbi_query_unreachable",
      diagnostics: {
        code: "pbi_query_unreachable",
        diagnosticCode: "powerbi_query_failure",
        safeDetail: "phase=execute; failure_class=network",
      },
    });
  });

  it("preserves emit order — replaying twice is idempotent (final state matches single replay)", async () => {
    const events = [
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      {
        seq: 2,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "s1",
          step_type: "tool_run",
          header_zh: "查询数据",
          started_at: "2026-05-26T00:00:01Z",
        },
        occurred_at: "",
      },
      {
        seq: 3,
        event_type: "step_done",
        payload: {
          card_id: T,
          step_id: "s1",
          step_type: "tool_run",
          state: "success",
          duration_ms: 100,
        },
        occurred_at: "",
      },
    ];
    mockFetchEvents(events);

    await replayTurnEvents(T);
    const first = getTurnSnapshot(T);
    expect(first?.steps.length).toBe(1);
    expect(first?.steps[0].state).toBe("done");

    await replayTurnEvents(T);
    const second = getTurnSnapshot(T);
    expect(second?.steps.length).toBe(1);
    expect(second?.steps[0].state).toBe("done");
    expect(second?.steps[0].stepId).toBe(first?.steps[0].stepId);
  });

  it("replays a persisted HITL resume as an open same-turn card", async () => {
    mockFetchEvents([
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      {
        seq: 2,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "s1",
          step_type: "tool_run",
          header_zh: "查询数据",
          started_at: "2026-05-26T00:00:01Z",
        },
        occurred_at: "",
      },
      {
        seq: 3,
        event_type: "step_list_card_finalized",
        payload: { card_id: T, state: "failed", step_count: 1 },
        occurred_at: "",
      },
      {
        seq: 4,
        event_type: "turn.resumed",
        payload: { turn_id: T, request_id: "clar_1", chosen_values: ["华东"] },
        occurred_at: "",
      },
      {
        seq: 5,
        event_type: "step_started",
        payload: {
          card_id: T,
          step_id: "s2",
          step_type: "tool_run",
          header_zh: "继续查询",
          started_at: "2026-05-26T00:00:02Z",
        },
        occurred_at: "",
      },
    ]);

    await replayTurnEvents(T);

    const snap = getTurnSnapshot(T);
    expect(snap?.steps.map((step) => step.stepId)).toEqual(["s1", "s2"]);
    expect(snap?.cardFinalized).toBe(false);
    expect(snap?.cardFinalState).toBeNull();
  });

  it("skips events without an event_type string", async () => {
    mockFetchEvents([
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      { seq: 2, event_type: null as unknown as string, payload: {}, occurred_at: "" },
      { seq: 3, event_type: "", payload: {}, occurred_at: "" },
    ]);
    const result = await replayTurnEvents(T);
    expect(result.eventCount).toBe(3);
    expect(result.claimed).toBe(1);
  });

  it("returns {eventCount: 0} and does not throw on HTTP 500", async () => {
    mockFetchFailure(500);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const result = await replayTurnEvents(T);
    expect(result.eventCount).toBe(0);
    expect(result.claimed).toBe(0);
    expect(getTurnSnapshot(T)).toBeUndefined();
    warn.mockRestore();
  });

  it("does not throw on network failure (fetch rejected)", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const result = await replayTurnEvents(T);
    expect(result.eventCount).toBe(0);
    warn.mockRestore();
  });

  it("backfills card_id (== turnId) for agent_spawned, which omits it → peerNames/peerTasks populate", async () => {
    // P4a regression (caught dogfooding a real 116 DD turn): agent_spawned carries
    // the peer's identity but NO card_id (the live path injects it from the stream
    // URL; the replay path must backfill it). Without the backfill, applyPeerName
    // gets no turnId, peerNames stays empty, and every archived multi-agent turn
    // collapses to a single lead block with NO peer blocks.
    mockFetchEvents([
      { seq: 1, event_type: "step_list_card_created", payload: { card_id: T }, occurred_at: "" },
      {
        seq: 2,
        event_type: "agent_spawned",
        payload: {
          subagent_id: "peer_c",
          profile: "growth-agent",
          display_name: "增长分析专家",
          task: "增长归因",
          agent_id: "osa_growth",
          agent_revision_id: "arv_growth_2",
          agent_release_id: "arl_growth_2",
          agent_execution_id: "aex_growth",
          display_name_snapshot: "增长分析专家",
        },
        occurred_at: "",
      },
      {
        seq: 3,
        event_type: "step_started",
        payload: { card_id: T, step_id: "p1", subagent_id: "peer_c", step_type: "tool_run", header_zh: "查询", started_at: "2026-06-28T00:00:01Z" },
        occurred_at: "",
      },
    ]);

    await replayTurnEvents(T);
    const snap = getTurnSnapshot(T);
    expect(snap?.peerNames).toEqual({ peer_c: "growth-agent" });
    expect(snap?.peerDisplayNames).toEqual({ peer_c: "增长分析专家" });
    expect(snap?.peerTasks).toEqual({ peer_c: "增长归因" });
    expect(snap?.peerAgentAttribution.peer_c).toEqual({
      agentId: "osa_growth",
      agentRevisionId: "arv_growth_2",
      agentReleaseId: "arl_growth_2",
      agentExecutionId: "aex_growth",
      displayNameSnapshot: "增长分析专家",
    });
    // the peer's inner step retains its subagentId → groups under the peer block
    expect(snap?.steps.find((s) => s.stepId === "p1")?.subagentId).toBe("peer_c");
  });
});

  it("graph-mode 批1(review F-2): plan-scoped step 事件豁免 card_id 注入 → replay 进 plan-store 且带 verify", async () => {
    const { applyPlanProposed, getPlanSnapshot } = await import("./plan-store");
    applyPlanProposed({
      plan_id: "plan_r1",
      title: "t",
      steps: [{ id: "ps_r1", agent: "compute", objective: "o", level: 0, depends_on: [] }],
    });
    mockFetchEvents([
      {
        seq: 1,
        event_type: "step_done",
        payload: { plan_id: "plan_r1", step_id: "ps_r1", digest: "d", verify: { verdict: "partial", unmet: ["含数字"] } },
        occurred_at: "",
      },
    ]);

    await replayTurnEvents(T);

    const step = getPlanSnapshot("plan_r1")?.steps[0];
    expect(step?.status).toBe("done");
    expect(step?.verify?.verdict).toBe("partial");
    // Nor should a wrongly injected card_id add it to step-list (a turn-level card must not create a same-named step)
    expect(getTurnSnapshot(T)?.steps?.some((s) => s.stepId === "ps_r1") ?? false).toBe(false);
  });
