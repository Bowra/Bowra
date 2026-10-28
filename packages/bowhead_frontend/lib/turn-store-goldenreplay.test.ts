/**
 * Golden replay (chat-solidify O3, 2026-07-03) — automated protection for live == F5.
 *
 * First pipeline invariant: seen live == persisted == replayed (chat-pipeline.md).
 * Previously only manual F5 (W3) verified it. This test pins it:
 *
 *   Path A (live): preprocess the same event stream as use-turn-runner does live
 *     (fill card_id for agent_spawned / __turnId for emoji_ack, both from the stream URL),
 *     then call dispatchToTurnStore for each event.
 *   Path B (F5): pass the same stream through replayTurnEvents (mock GET /turns/{id}/events).
 *
 *   Assert: both paths' terminal turn-store states deep-equal (beats/steps/peerProse/peerNames/
 *   emojiAcks/card terminal state: compare everything, without selecting fields).
 *
 * The fixture has the real wire shape: payloads contain no fixture-only extras (__turnId, etc.).
 * Events cover segmented narrative/parallel peer deinterleaving/steer ack/citation/terminal state.
 * Adding events or changing the dispatcher must fail here immediately if live == replay breaks.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getLiteProposal, resetLiteForTests } from "./config-copilot-lite";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEvents } from "./turn-store-replay";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

const T = "t_golden";
const STREAM_URL = `/api/v1/chat/stream/${T}`;

/** Golden event stream in real wire shape (one turn, covering the main shapes). */
const GOLDEN_EVENTS: Array<{ event_type: string; payload: Record<string, unknown> }> = [
  { event_type: "turn_started", payload: { seq: 1, session_id: "s_1", user_text: "分析各地区销售", workspace_id: "w" } },
  { event_type: "step_list_card_created", payload: { seq: 2, card_id: T } },
  { event_type: "narrative.delta", payload: { seq: 3, card_id: T, delta: "好的，我先查" } },
  { event_type: "narrative.delta", payload: { seq: 4, card_id: T, delta: "一下总体数据。" } },
  // Two parallel peers: spawn rows + identity + each peer's internal steps + peer prose.
  {
    event_type: "step_started",
    payload: {
      seq: 5,
      card_id: T,
      step_id: "sp1",
      step_type: "subagent_spawn",
      tool_name: "subagent_spawn",
      header_zh: "派发",
      label: "compute-1",
      started_at: "2026-07-03T10:00:01Z",
    },
  },
  {
    event_type: "step_started",
    payload: {
      seq: 6,
      card_id: T,
      step_id: "sp2",
      step_type: "subagent_spawn",
      tool_name: "subagent_spawn",
      header_zh: "派发",
      label: "compute-2",
      started_at: "2026-07-03T10:00:02Z",
    },
  },
  {
    event_type: "agent_spawned",
    payload: { seq: 7, subagent_id: "peer_a", profile: "east-sales-agent", display_name: "华东销售专家", task: "查华东" },
  },
  {
    event_type: "agent_spawned",
    payload: { seq: 8, subagent_id: "peer_b", profile: "south-sales-agent", display_name: "华南销售专家", task: "查华南" },
  },
  {
    event_type: "peer_dispatched",
    payload: {
      seq: 9,
      card_id: T,
      peer_name: "east-sales-agent",
      subagent_id: "peer_a",
      spawn_id: "peer_a",
      call_id: "c-sp1",
      parent_step_id: "sp1",
    },
  },
  {
    event_type: "peer_dispatched",
    payload: {
      seq: 10,
      card_id: T,
      peer_name: "south-sales-agent",
      subagent_id: "peer_b",
      spawn_id: "peer_b",
      call_id: "c-sp2",
      parent_step_id: "sp2",
    },
  },
  {
    event_type: "step_started",
    payload: {
      seq: 11,
      card_id: T,
      step_id: "st_a1",
      step_type: "tool_run",
      tool_name: "bi_query",
      header_zh: "查询",
      subagent_id: "peer_a",
      started_at: "2026-07-03T10:00:03Z",
    },
  },
  {
    event_type: "step_started",
    payload: {
      seq: 12,
      card_id: T,
      step_id: "st_b1",
      step_type: "tool_run",
      tool_name: "python_sandbox",
      header_zh: "计算",
      subagent_id: "peer_b",
      started_at: "2026-07-03T10:00:04Z",
    },
  },
  { event_type: "step_progress", payload: { seq: 13, card_id: T, step_id: "st_b1", content_delta: "import pandas" } },
  { event_type: "partial_answer", payload: { seq: 14, card_id: T, parent_agent_id: "peer_a", delta: "华东同比 +12%。" } },
  { event_type: "partial_answer", payload: { seq: 15, card_id: T, parent_agent_id: "peer_b", delta: "华南环比 -3%。" } },
  { event_type: "step_done", payload: { seq: 16, card_id: T, step_id: "st_a1", state: "done", duration_ms: 900, summary: "234 rows" } },
  { event_type: "step_done", payload: { seq: 17, card_id: T, step_id: "st_b1", state: "done", duration_ms: 1400 } },
  { event_type: "lead.subagent_summary", payload: { seq: 18, card_id: T, spawn_id: "peer_a", status: "succeeded", duration_ms: 900 } },
  { event_type: "lead.subagent_summary", payload: { seq: 19, card_id: T, spawn_id: "peer_b", status: "succeeded_with_warnings", duration_ms: 1400 } },
  { event_type: "approval.required", payload: { seq: 20, card_id: T, approval_id: "ap_1", tool_name: "bi_update_measures" } },
  { event_type: "approval.executed", payload: { seq: 21, card_id: T, approval_id: "ap_1", ok: true } },
  // The second approval takes the **rejection** path (R-2): required during the turn, rejected after done
  // via an out-of-band event (continuing seq). Pin rejected converging to terminal identically in live and F5,
  // rather than staying pending (which would distort approvalStatusById semantics).
  { event_type: "approval.required", payload: { seq: 22, card_id: T, approval_id: "ap_2", tool_name: "bi_delete_measures" } },
  // steer 🤝 (real wire: no card_id / __turnId; each path fills them in).
  { event_type: "emoji_ack", payload: { seq: 23, target_msg_id: "m1", emoji: "🤝", text: "顺便按品类拆", ts: "2026-07-03T10:00:00Z" } },
  { event_type: "turn.citation_registered", payload: { seq: 24, card_id: T, citation_seq: 1, artifact_id: "art_1", kind: "table" } },
  // Config copilot batch 0.5: lightweight confirmation beat (real wire shape: turn_id in payload, review P0-1 anchor).
  {
    event_type: "config.lite_propose",
    payload: {
      seq: 25,
      card_id: T,
      proposal_id: "ccd_prop1",
      turn_id: T,
      form_id: "user_appearance",
      workspace_id: null,
      workspace_name: null,
      rows: [{ field: "mode", from: "light", to: "dark", from_unset: true }],
      notes: {},
    },
  },
  // Config copilot batch 1-b: two successive events for one draft (per-turn beat idempotency + latest payload in store;
  // live and F5 terminal states must match; auto-opening the drawer is live-only at the wire layer, outside the store).
  {
    event_type: "config.draft_updated",
    payload: {
      seq: 25.1,
      card_id: T,
      draft_id: "ccd_draft1",
      turn_id: T,
      form_id: "scheduled_job",
      created: true,
      updated_fields: ["name", "cron"],
      fields: { name: "销售周报", cron: "0 9 * * 1" },
      field_meta: { name: { origin: "generated", needs_confirm: false, note: null } },
      notes: {},
      workspace_id: "w",
    },
  },
  {
    event_type: "config.draft_updated",
    payload: {
      seq: 25.2,
      card_id: T,
      draft_id: "ccd_draft1",
      turn_id: T,
      form_id: "scheduled_job",
      created: false,
      updated_fields: ["cron"],
      fields: { name: "销售周报", cron: "0 10 * * 1" },
      field_meta: { name: { origin: "generated", needs_confirm: false, note: null } },
      notes: {},
      workspace_id: "w",
    },
  },
  { event_type: "final_answer", payload: { seq: 26, card_id: T, text: "各地区销售: 华东领先 [cite:1]。" } },
  { event_type: "step_list_card_finalized", payload: { seq: 27, card_id: T, state: "completed", step_count: 4 } },
  // `duration_ms` is this turn's wall-clock time (monotonic delta in Lead run_turn); the backend includes it in every done.
  // The collapsed strip's `· Xs` reads it. Deliberately differ from the two step durations' sum (900+1400), so if only one path
  // stores it, the full deep-equal comparison below exposes that immediately.
  {
    event_type: "done",
    payload: { seq: 28, turn_id: T, status: "completed", duration_ms: 124000, tokens_input: 100, tokens_output: 50, cost_usd: 0.01 },
  },
  // Out-of-band: approval rejection occurs after terminal done; the bus is closed, and the extra event continues seq.
  // ⚠ The actual live closed bus has no subscribers (events.py emit_out_of_band); this post-terminal event
  // is only persisted, unavailable to the live tab, so delivery already relies on replay. Path A still feeds it to the live reducer
  // to pin **reducer symmetry** (equal terminal states for one event), not delivery symmetry. Rejected and pending
  // are fold-equivalent under hasBlockingApprovalState (both block collapsing), making the delivery gap invisible to users;
  // turn-store-dispatch.test.ts separately pins that equivalence with its fold-equivalence assertion.
  { event_type: "approval.rejected", payload: { seq: 29, card_id: T, approval_id: "ap_2" } },
];

/** Live-path preprocessing (copied from use-turn-runner handle() injection rules). */
function liveMassage(name: string, raw: Record<string, unknown>): Record<string, unknown> {
  const payload = { ...raw };
  if (name === "agent_spawned" && typeof payload.card_id !== "string") {
    const tid = STREAM_URL.split("/").filter(Boolean).pop();
    if (tid) payload.card_id = tid;
  }
  if (name === "emoji_ack" && typeof payload.__turnId !== "string") {
    const tid = STREAM_URL.split("/").filter(Boolean).pop();
    if (tid) payload.__turnId = tid;
  }
  return payload;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  vi.restoreAllMocks();
});

describe("golden replay — live dispatch 与 F5 replay 终态必须相等", () => {
  it("preserves typed configuration values, reset intent and review destinations in live and replay", async () => {
    const event = { event_type: "config.lite_propose", payload: {
      seq: 1, proposal_id: "ccd_typed", turn_id: T, form_id: "team_model_config", team_id: "team_original",
      workspace_id: null, review_page: "/team/settings/models/", notes: {}, rows: [
        { field: "providers", from: {}, to: { primary: { enabled: false, values: [0, null] } }, label: "Connections", from_unset: true, baseline_revision: 7 },
        { field: "step_limit", from: 12, to: null, operation: "reset", reset_all: true, from_unset: false, baseline_revision: "limits-v1" },
      ],
    } };
    resetLiteForTests();
    dispatchToTurnStore(event.event_type, event.payload);
    const live = structuredClone(getLiteProposal("ccd_typed"));
    expect(live?.rows).toEqual(event.payload.rows);
    expect(live?.review_page).toBe(event.payload.review_page);
    resetLiteForTests();
    _resetAllTurnsForTesting();
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ events: [event] }), {
      status: 200, headers: { "content-type": "application/json" },
    })) as typeof fetch;
    await replayTurnEvents(T);
    expect(getLiteProposal("ccd_typed")).toEqual(live);
  });

  it("preserves the original settings send association in live and replay", async () => {
    const target = { form_id: "workspace_i18n", user_id: "u_original", team_id: "team_original", workspace_id: "ws_original", edit_generation: 7 };
    const event = {
      event_type: "config.lite_propose",
      payload: {
        seq: 1,
        proposal_id: "ccd_selected",
        turn_id: T,
        form_id: "workspace_i18n",
        team_id: target.team_id,
        workspace_id: target.workspace_id,
        settings_target: target,
        rows: [{ field: "week_start_day", from: "monday", to: "sunday", from_unset: false }],
        notes: {},
      },
    };
    resetLiteForTests();
    dispatchToTurnStore(event.event_type, event.payload);
    const live = structuredClone(getLiteProposal("ccd_selected"));
    expect(live?.settings_target).toEqual(target);
    resetLiteForTests();
    _resetAllTurnsForTesting();
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ events: [event] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ) as typeof fetch;
    await replayTurnEvents(T);
    expect(getLiteProposal("ccd_selected")).toEqual(live);
  });

  it("同一事件流两条路终态 deep-equal(含 steps/beats/peerProse/emojiAcks)", async () => {
    // Path A — live。
    for (const ev of GOLDEN_EVENTS) {
      dispatchToTurnStore(ev.event_type, liveMassage(ev.event_type, ev.payload));
    }
    const liveState = structuredClone(getTurnSnapshot(T));
    expect(liveState).toBeTruthy();
    expect(liveState!.steps.length).toBeGreaterThan(0);
    expect(liveState!.beats.length).toBeGreaterThan(0);
    expect(Object.keys(liveState!.peerProse)).toContain("peer_a");
    expect(liveState!.steps.find((step) => step.stepId === "sp1")?.peerProseKey).toBe("peer_a");
    expect(liveState!.steps.find((step) => step.stepId === "sp2")?.peerProseKey).toBe("peer_b");
    expect(liveState!.peerTerminalById.peer_b.status).toBe("succeeded_with_warnings");
    expect(liveState!.approvalStatusById.ap_1).toBe("executed");
    expect(liveState!.approvalStatusById.ap_2).toBe("rejected");
    expect(liveState!.emojiAcks.length).toBe(1);
    // Wall-clock duration comes from done.duration_ms, not the 900+1400 sum of step durations.
    expect(liveState!.totalDurationMs).toBe(124000);
    // Config copilot batch 0.5: lite beat is appended and proposal store has the payload (live side).
    expect(liveState!.beats.some((b) => b.kind === "lite" && b.proposalId === "ccd_prop1")).toBe(true);
    expect(getLiteProposal("ccd_prop1")?.rows[0]).toEqual({ field: "mode", from: "light", to: "dark", from_unset: true });

    // Path B: F5 replay (fresh page: clear both store and dispatcher module state).
    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    resetLiteForTests();
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      expect(String(url)).toContain(`/turns/${T}/events`);
      return new Response(JSON.stringify({ events: GOLDEN_EVENTS }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as typeof fetch;

    const result = await replayTurnEvents(T);
    expect(result.eventCount).toBe(GOLDEN_EVENTS.length);
    const replayState = structuredClone(getTurnSnapshot(T));

    // Compare the entire terminal state, without field selection: expose any live-only / replay-only handling divergence.
    expect(replayState).toEqual(liveState);
    // Config copilot batch 0.5: F5 also rebuilds the proposal store (card data source live == F5).
    expect(getLiteProposal("ccd_prop1")?.form_id).toBe("user_appearance");
  });

  it("replay 两遍幂等(重复 seq 去重, 终态不变)", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ events: GOLDEN_EVENTS }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    ) as typeof fetch;
    await replayTurnEvents(T);
    const once = structuredClone(getTurnSnapshot(T));
    await replayTurnEvents(T);
    const twice = structuredClone(getTurnSnapshot(T));
    expect(twice).toEqual(once);
  });
});
