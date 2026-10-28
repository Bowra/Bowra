/**
 * Phase 1.5 E2E UT — exercises the full SSE event → dispatch → turn-store →
 * snapshot pipeline for a realistic turn cycle:
 *
 *   step_list_card_created → step_started × N (with concurrency_group) →
 *   step_progress (token deltas) → step_done → step_list_card_finalized →
 *   plus HITL clarification + emoji_ack steer + cascading clarification.
 *
 * Verifies the contract that lib/use-turn-runner.ts's pre-dispatch hook
 * relies on: every dispatched SSE event becomes a deterministic turn-store
 * mutation visible via getTurnSnapshot. Phase 1.4 wired the new step_*
 * surface end-to-end on live prod; this UT pins the contract so future
 * Phase 1.5 backend dual-emit cleanups don't silently drift.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  _resetAllTurnsForTesting,
  getTurnSnapshot,
  selectNarrativeBuffer,
} from "./turn-store";
import {
  _resetNarrativeCursorsForTesting,
  dispatchToTurnStore,
} from "./turn-store-dispatch";

const T = "turn-e2e";

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

function created(): void {
  dispatchToTurnStore("step_list_card_created", { card_id: T });
}

function started(stepId: string, overrides: Record<string, unknown> = {}): void {
  dispatchToTurnStore("step_started", {
    card_id: T,
    step_id: stepId,
    step_type: "tool_run",
    header_zh: "查询数据",
    started_at: "2026-05-22T14:32:00Z",
    tool_name: "bi_query",
    ...overrides,
  });
}

function done(
  stepId: string,
  state: "success" | "failure" | "partial" = "success",
  overrides: Record<string, unknown> = {},
): void {
  dispatchToTurnStore("step_done", {
    card_id: T,
    step_id: stepId,
    step_type: "tool_run",
    state,
    duration_ms: 700,
    ...overrides,
  });
}

describe("turn-store E2E pipeline (Phase 1.5 contract)", () => {
  it("full turn lifecycle: create → 3 sequential steps done → finalize", () => {
    created();
    started("s1");
    done("s1", "success", { output_preview: "200 rows" });
    started("s2");
    done("s2", "success", { output_preview: "150 rows" });
    started("s3");
    done("s3", "success", { output_preview: "300 rows" });
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "completed",
      step_count: 3,
    });

    const s = getTurnSnapshot(T);
    expect(s?.cardCreated).toBe(true);
    expect(s?.cardFinalized).toBe(true);
    expect(s?.cardFinalState).toBe("completed");
    expect(s?.steps.length).toBe(3);
    expect(s?.steps.every((st) => st.state === "done")).toBe(true);
    expect(s?.steps.map((st) => st.summary)).toEqual([
      "200 rows",
      "150 rows",
      "300 rows",
    ]);
  });

  it("concurrent batch: 3 steps with same concurrency_group_id preserved", () => {
    created();
    started("p1", { concurrency_group_id: "g1" });
    started("p2", { concurrency_group_id: "g1" });
    started("p3", { concurrency_group_id: "g1" });
    done("p1");
    done("p2", "failure", {
      error: { type: "sql_error", message_zh: "列不存在", is_transient: false },
    });
    done("p3");

    const s = getTurnSnapshot(T);
    expect(s?.steps.map((x) => x.concurrencyGroupId)).toEqual(["g1", "g1", "g1"]);
    expect(s?.steps.map((x) => x.state)).toEqual(["done", "failed", "done"]);
    const failed = s?.steps.find((x) => x.state === "failed");
    expect(failed?.error?.kind).toBe("permanent");
    expect(failed?.error?.message).toBe("列不存在");
  });

  it("lead_thinking_writing: progress deltas append to partialContent", () => {
    created();
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "lw1",
      step_type: "lead_thinking_writing",
      header_zh: "写代码",
      started_at: "2026-05-22T14:32:00Z",
      tool_name: "lead_thinking_writing_python",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "lw1",
      content_delta: "import pandas as pd\n",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "lw1",
      content_delta: "df = pd.DataFrame()\n",
    });
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "lw1",
      content_delta: "print(df)\n",
    });
    done("lw1");

    const s = getTurnSnapshot(T);
    expect(s?.steps[0]?.partialContent).toBe(
      "import pandas as pd\ndf = pd.DataFrame()\nprint(df)\n",
    );
    expect(s?.steps[0]?.state).toBe("done");
  });

  it("bi_query batch: step_done with sub_steps populates SubStepResult[]", () => {
    created();
    started("b1");
    done("b1", "partial", {
      output_preview: "3 / 5 成功",
      sub_steps: [
        { label: "SELECT a", state: "success", output_preview: "5 rows" },
        { label: "SELECT b", state: "failure", error_kind: "sql_error" },
        { label: "SELECT c", state: "success", output_preview: "12 rows" },
        { label: "SELECT d", state: "failure", error_kind: "sql_error" },
        { label: "SELECT e", state: "success", output_preview: "8 rows" },
      ],
    });

    const subs = getTurnSnapshot(T)?.steps[0]?.subSteps;
    expect(subs?.length).toBe(5);
    expect(subs?.filter((s) => s.status === "done").length).toBe(3);
    expect(subs?.filter((s) => s.status === "failed").length).toBe(2);
  });

  it("emoji_ack steer: 🤝 + text + ts stored on turn-store", () => {
    created();
    started("s1");
    done("s1");
    dispatchToTurnStore("emoji_ack", {
      target_msg_id: "u_msg_abc",
      emoji: "🤝",
      text: "对了，按月统计",
      ts: "2026-05-22T14:32:05Z",
      __turnId: T,
    });
    dispatchToTurnStore("emoji_ack", {
      target_msg_id: "u_msg_def",
      emoji: "🤝",
      text: "改用 GMV 不要营业额",
      ts: "2026-05-22T14:32:08Z",
      __turnId: T,
    });

    const acks = getTurnSnapshot(T)?.emojiAcks;
    expect(acks?.length).toBe(2);
    expect(acks?.[0]?.text).toBe("对了，按月统计");
    expect(acks?.[0]?.ts).toBe("2026-05-22T14:32:05Z");
    expect(acks?.[1]?.text).toBe("改用 GMV 不要营业额");
  });

  it("turn.follow_up is silently claimed (no state change)", () => {
    created();
    started("s1");
    done("s1");
    const beforeSnap = JSON.stringify(getTurnSnapshot(T));
    const claimed = dispatchToTurnStore("turn.follow_up", {
      follow_up_turn_id: "t_2",
      follow_up_stream_url: "/api/v1/chat/stream/t_2",
      reason: "steer_drain",
      buffered_count: 1,
    });
    expect(claimed).toBe(true);
    const afterSnap = JSON.stringify(getTurnSnapshot(T));
    expect(afterSnap).toBe(beforeSnap);
  });

  it("idempotent: re-emitting step_list_card_created on same turn is a no-op", () => {
    created();
    started("s1");
    done("s1");
    created();
    created();
    const s = getTurnSnapshot(T);
    expect(s?.cardCreated).toBe(true);
    expect(s?.steps.length).toBe(1);
  });

  it("step_progress on missing step is silently dropped (no crash)", () => {
    created();
    dispatchToTurnStore("step_progress", {
      card_id: T,
      step_id: "nonexistent",
      content_delta: "...",
    });
    const s = getTurnSnapshot(T);
    expect(s?.steps).toEqual([]);
  });

  it("HITL flow: card_finalized arrives but card stays usable for cascading clarification", () => {
    created();
    started("s1");
    done("s1");
    // First clarification — backend finalizes with state=completed
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "failed",
      step_count: 1,
    });
    // User picks → backend resumes same turn. turn.resumed does not carry
    // card_id on the live wire, so the dispatcher must route by turn_id.
    dispatchToTurnStore("turn.resumed", {
      turn_id: T,
      request_id: "clar_1",
      chosen_values: ["华东"],
    });
    expect(getTurnSnapshot(T)?.cardFinalized).toBe(false);
    expect(getTurnSnapshot(T)?.cardFinalState).toBeNull();

    // Continued execution appends to the same card.
    started("s2");
    done("s2");
    // Backend re-finalizes
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "completed",
      step_count: 2,
    });

    const s = getTurnSnapshot(T);
    expect(s?.steps.length).toBe(2);
    expect(s?.cardFinalized).toBe(true);
  });

  // Phase 1.6 G3 — Lead narrative streaming end-to-end through dispatcher
  it("narrative.delta stream: chunks accumulate into a single narrative buffer interleaved with steps", () => {
    created();
    // Pre-tool prose
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "好的，" });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "我先查 Q3 数据。" });
    // First tool runs in parallel with continued narrative
    started("s1");
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: " 看到了，" });
    done("s1");
    // Inter-tool conclusion prose
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: " 接下来我再对比 Q2。" });
    started("s2");
    done("s2");
    dispatchToTurnStore("step_list_card_finalized", {
      card_id: T,
      state: "completed",
      step_count: 2,
    });

    const s = getTurnSnapshot(T);
    expect(selectNarrativeBuffer(s)).toBe("好的，我先查 Q3 数据。 看到了， 接下来我再对比 Q2。");
    expect(s?.steps.length).toBe(2);
    expect(s?.cardFinalized).toBe(true);
  });

  // ─── Phase 1.7-A: beats[] order pipeline + concurrency + replay ───
  it("Phase 1.7-A beat-order pipeline: narrative → step → narrative → step → answer", () => {
    created();
    // First narrative segment
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "好的，" });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "我来查。" });
    started("s1");
    done("s1");
    // Second narrative segment (cursor invalidated by step_started → new segId)
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "接下来对比。" });
    started("s2");
    done("s2");
    // Final answer
    dispatchToTurnStore("final_answer", { turn_id: T, text: "结论：A 下滑最大。" });

    const beats = getTurnSnapshot(T)?.beats ?? [];
    const kinds = beats.map((b) => b.kind);
    expect(kinds).toEqual([
      "narrative",
      "step",
      "narrative",
      "step",
      "answer",
    ]);
    // narrative beats use different segIds (broken by step)
    const narrativeBeats = beats.filter((b) => b.kind === "narrative");
    expect(narrativeBeats.length).toBe(2);
    if (narrativeBeats[0]?.kind === "narrative" && narrativeBeats[1]?.kind === "narrative") {
      expect(narrativeBeats[0].segId).not.toBe(narrativeBeats[1].segId);
      expect(narrativeBeats[0].text).toBe("好的，我来查。");
      expect(narrativeBeats[1].text).toBe("接下来对比。");
    }
  });

  it("Phase 1.7-A concurrent step interleaving: multiple concurrency_group_id steps preserved in beats[]", () => {
    created();
    started("p1", { concurrency_group_id: "g1" });
    started("p2", { concurrency_group_id: "g1" });
    started("p3", { concurrency_group_id: "g2" });
    done("p1");
    done("p2");
    done("p3");

    const beats = getTurnSnapshot(T)?.beats ?? [];
    const stepBeats = beats.filter((b) => b.kind === "step");
    expect(stepBeats.length).toBe(3);
    // Order preserved per dispatch order
    expect(stepBeats.map((b) => b.kind === "step" ? b.stepId : "")).toEqual(["p1", "p2", "p3"]);
  });

  it("Phase 1.7-A dup step_started (same stepId) does NOT push duplicate step beat", () => {
    created();
    started("s1");
    started("s1");  // dup
    started("s1");  // dup
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.filter((b) => b.kind === "step").length).toBe(1);
  });

  it("Phase 1.7-A dup narrative.delta within same contiguous run accumulates into single beat", () => {
    created();
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "好" });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "的" });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "，我来" });
    const beats = getTurnSnapshot(T)?.beats ?? [];
    // Exactly 1 narrative beat
    expect(beats.filter((b) => b.kind === "narrative").length).toBe(1);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("好的，我来");
  });

  it("Phase 1.7-A replay-stable: identical event sequence twice via reset produces identical beats[]", () => {
    const events = [
      ["step_list_card_created", { card_id: T }],
      ["narrative.delta", { card_id: T, delta: "我先查 Q3。" }],
      ["step_started", {
        card_id: T,
        step_id: "s1",
        step_type: "tool_run",
        header_zh: "查询",
        started_at: "2026-05-22T14:32:00Z",
        tool_name: "bi_query",
      }],
      ["step_done", {
        card_id: T,
        step_id: "s1",
        step_type: "tool_run",
        state: "success",
        duration_ms: 700,
      }],
      ["narrative.delta", { card_id: T, delta: "接下来对比 Q2。" }],
      ["final_answer", { turn_id: T, text: "结论：A 下滑。" }],
    ] as const;
    for (const [name, payload] of events) {
      dispatchToTurnStore(name as string, payload);
    }
    const beats1 = JSON.parse(JSON.stringify(getTurnSnapshot(T)?.beats ?? []));

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();

    for (const [name, payload] of events) {
      dispatchToTurnStore(name as string, payload);
    }
    const beats2 = JSON.parse(JSON.stringify(getTurnSnapshot(T)?.beats ?? []));

    expect(beats1).toEqual(beats2);
  });
});
