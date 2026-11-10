// @vitest-environment node
/**
 * `llm.stream_restarted` → withdraw the output a restarted model attempt had
 * already streamed (Lead narrative, or a peer's `partial_answer` prose).
 *
 * Live SSE and replay share `dispatchToTurnStore`, so the same rules decide both;
 * these tests pin the trimming, live == replay, reconnect idempotency and the
 * pending retry status.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";
import { _resetAllTurnsForTesting, getTurnSnapshot, selectNarrativeBuffer } from "./turn-store";

const T = "turn-restart";

type Row = { event_type: string; payload: Record<string, unknown> };

const retry = (seq: number, over: Record<string, unknown> = {}): Row => ({
  event_type: "llm.retry_scheduled",
  payload: { seq, subagent_id: "sa_lead", profile: "lead", retry: 1, max_retries: 3, reason: "network", delay_s: 2, ...over },
});

const leadRestart = (seq: number, chars: number, stepIds: string[] = []): Row => ({
  event_type: "llm.stream_restarted",
  payload: {
    seq,
    subagent_id: "sa_lead",
    profile: "lead",
    discarded_narrative_chars: chars,
    discarded_partial_answer_chars: 0,
    discarded_step_ids: stepIds,
  },
});

const narrative = (seq: number, delta: string): Row => ({
  event_type: "narrative.delta",
  payload: { seq, card_id: T, delta },
});

/** Live frames of these events carry no card_id; the dispatcher routes them by the stream's turn. */
function dispatchLive(rows: Row[], source: "live" | "replay" = "live"): void {
  for (const row of rows) dispatchToTurnStore(row.event_type, { ...row.payload }, source, T);
}

function narrativeBeats(): Array<{ segId: string; text: string }> {
  return (getTurnSnapshot(T)?.beats ?? []).flatMap((b) =>
    b.kind === "narrative" ? [{ segId: b.segId, text: b.text }] : [],
  );
}

/**
 * Lead: the earlier call ended in prose ("Looking at Q3. "), so the restarted
 * attempt continued that segment ("Sales "), opened a writing step and streamed
 * into a second segment ("rose") before the connection dropped.
 */
const LEAD_RESTART_ROWS: Row[] = [
  { event_type: "step_list_card_created", payload: { seq: 1, card_id: T } },
  narrative(2, "Looking at Q3. "),
  narrative(3, "Sales "),
  {
    event_type: "step_started",
    payload: {
      seq: 4,
      card_id: T,
      step_id: "writing_call_1",
      step_type: "lead_thinking_writing",
      header_zh: "写代码 (Python)",
      started_at: "2026-11-08T10:00:00Z",
      tool_name: "python_sandbox",
      call_id: "call_1",
    },
  },
  { event_type: "step_progress", payload: { seq: 5, card_id: T, step_id: "writing_call_1", content_delta: "import pan" } },
  narrative(6, "rose"),
  retry(7),
  {
    event_type: "step_done",
    payload: {
      seq: 8,
      card_id: T,
      step_id: "writing_call_1",
      step_type: "lead_thinking_writing",
      state: "success",
      duration_ms: 0,
      tool_name: "python_sandbox",
      call_id: "call_1",
    },
  },
  leadRestart(9, "Sales ".length + "rose".length, ["writing_call_1"]),
  narrative(10, "Sales fell 3%."),
  { event_type: "final_answer", payload: { seq: 11, card_id: T, text: "Sales fell 3%." } },
  { event_type: "done", payload: { seq: 12, turn_id: T, status: "completed", duration_ms: 9000 } },
];

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("Lead narrative withdrawal", () => {
  it("removes the restarted attempt's text across a segment boundary and drops the emptied segment", () => {
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 9));
    const beats = getTurnSnapshot(T)?.beats ?? [];
    expect(beats.map((b) => b.kind)).toEqual(["narrative", "step"]);
    expect(narrativeBeats()).toEqual([{ segId: `${T}::seg-1`, text: "Looking at Q3. " }]);
    // The withdrawn writing step stays: the backend closed it with a normal step_done.
    expect(getTurnSnapshot(T)?.steps.map((s) => s.stepId)).toEqual(["writing_call_1"]);
  });

  it("streams the retried attempt into a fresh segment instead of reusing the dropped segId", () => {
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 10));
    expect(narrativeBeats()).toEqual([
      { segId: `${T}::seg-1`, text: "Looking at Q3. " },
      { segId: `${T}::seg-3`, text: "Sales fell 3%." },
    ]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Looking at Q3. Sales fell 3%.");
  });

  it("keeps appending to a partially trimmed open segment", () => {
    dispatchLive([
      { event_type: "step_list_card_created", payload: { seq: 1, card_id: T } },
      narrative(2, "Revenue grew "),
      narrative(3, "12"),
      retry(4),
      leadRestart(5, 2),
      narrative(6, "11%."),
    ]);
    expect(narrativeBeats()).toEqual([{ segId: `${T}::seg-1`, text: "Revenue grew 11%." }]);
  });

  it("counts characters as Unicode code points, like the backend's Python len()", () => {
    dispatchLive([
      narrative(1, "Margin 📈 up 👍🏽"),
      // "👍🏽" is two code points (four UTF-16 units); withdraw it and the space before it.
      leadRestart(2, 3),
    ]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Margin 📈 up");
    dispatchLive([leadRestart(3, 3)]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Margin 📈");
    // "📈" is one code point (two UTF-16 units): one character removes it whole.
    dispatchLive([leadRestart(4, 1)]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Margin ");
  });

  it("withdraws no more than the narrative that exists and ignores malformed counts", () => {
    dispatchLive([narrative(1, "Hi")]);
    dispatchLive([
      { event_type: "llm.stream_restarted", payload: { seq: 2, subagent_id: "sa_lead", profile: "lead", discarded_narrative_chars: -1 } },
      { event_type: "llm.stream_restarted", payload: { seq: 3, subagent_id: "sa_lead", profile: "lead", discarded_narrative_chars: "2" } },
      { event_type: "llm.stream_restarted", payload: { seq: 4, subagent_id: "sa_lead", profile: "lead", discarded_narrative_chars: 1.5 } },
    ]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Hi");
    dispatchLive([leadRestart(5, 10)]);
    expect(getTurnSnapshot(T)?.beats).toEqual([]);
  });
});

describe("peer prose withdrawal", () => {
  const peerProse = (seq: number, peer: string, delta: string): Row => ({
    event_type: "partial_answer",
    payload: { seq, card_id: T, parent_agent_id: peer, delta },
  });
  const peerRestart = (seq: number, peer: string, chars: number): Row => ({
    event_type: "llm.stream_restarted",
    payload: {
      seq,
      subagent_id: peer,
      profile: "business-analyst",
      parent_agent_id: peer,
      discarded_narrative_chars: 0,
      discarded_partial_answer_chars: chars,
      discarded_step_ids: [],
    },
  });

  it("trims only the restarted peer's prose and leaves the Lead narrative alone", () => {
    dispatchLive([
      narrative(1, "Dispatching two analysts."),
      peerProse(2, "peer_a", "East grew "),
      peerProse(3, "peer_b", "South fell 3%."),
      peerProse(4, "peer_a", "12%"),
      retry(5, { subagent_id: "peer_a", profile: "business-analyst" }),
      peerRestart(6, "peer_a", "grew 12%".length),
    ]);
    const state = getTurnSnapshot(T);
    expect(state?.peerProse).toEqual({ peer_a: "East ", peer_b: "South fell 3%." });
    expect(selectNarrativeBuffer(state)).toBe("Dispatching two analysts.");
  });

  it("withdraws Lead narrative from a helper-scoped restart of a reference primary", () => {
    // A reference primary streams Lead narrative from a helper-scoped stream: its
    // restart carries parent_agent_id together with narrative characters.
    dispatchLive([
      narrative(1, "Reading the reference. "),
      narrative(2, "Revenue fe"),
      {
        event_type: "llm.stream_restarted",
        payload: {
          seq: 3,
          subagent_id: "ref_primary",
          profile: "reference",
          parent_agent_id: "ref_primary",
          discarded_narrative_chars: "Revenue fe".length,
          discarded_partial_answer_chars: 0,
          discarded_step_ids: [],
        },
      },
      narrative(4, "Revenue fell 3%."),
    ]);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Reading the reference. Revenue fell 3%.");
  });

  it("drops a peer's entry when all of its prose is withdrawn, then rebuilds it from the retry", () => {
    dispatchLive([
      peerProse(1, "peer_a", "数据📈"),
      peerRestart(2, "peer_a", 3),
    ]);
    expect(getTurnSnapshot(T)?.peerProse).toEqual({});
    dispatchLive([peerProse(3, "peer_a", "数据下降")]);
    expect(getTurnSnapshot(T)?.peerProse).toEqual({ peer_a: "数据下降" });
  });
});

describe("pending retry status", () => {
  it("survives the withdrawal and the close of the withdrawn writing step, then clears on new output", () => {
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 9));
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Looking at Q3. ");
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 1, maxRetries: 3, reason: "network", seq: 7 });
    dispatchLive(LEAD_RESTART_ROWS.slice(9, 10));
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });
});

describe("replay and reconnect agree with live", () => {
  it("replaying the persisted rows yields the identical store", () => {
    dispatchLive(LEAD_RESTART_ROWS);
    const live = structuredClone(getTurnSnapshot(T));
    expect(selectNarrativeBuffer(live)).toBe("Looking at Q3. Sales fell 3%.");

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, LEAD_RESTART_ROWS as never);
    expect(getTurnSnapshot(T)).toEqual(live);
  });

  it("replaying a turn still waiting on the retried attempt shows the trimmed text and the retry, like live", () => {
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 9));
    const live = structuredClone(getTurnSnapshot(T));

    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    replayTurnEventRows(T, LEAD_RESTART_ROWS.slice(0, 9) as never);
    expect(getTurnSnapshot(T)).toEqual(live);
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 1 });
  });

  it("a reconnect that redelivers the whole stream does not withdraw twice", () => {
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 10));
    const before = structuredClone(getTurnSnapshot(T));
    // Full server replay on reconnect, then the same rows again through the hydrate path.
    dispatchLive(LEAD_RESTART_ROWS.slice(0, 10), "replay");
    replayTurnEventRows(T, LEAD_RESTART_ROWS.slice(0, 10) as never);
    expect(getTurnSnapshot(T)).toEqual(before);
    expect(selectNarrativeBuffer(getTurnSnapshot(T))).toBe("Looking at Q3. Sales fell 3%.");
  });
});

describe("EventSource listener", () => {
  it("registers llm.stream_restarted so the browser does not drop it", () => {
    const src = readFileSync(join(__dirname, "use-turn-runner.ts"), "utf8");
    const start = src.indexOf('"step_started", "step_progress", "step_done"');
    const end = src.indexOf("as const).forEach", start);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(src.slice(start, end)).toContain('"llm.stream_restarted"');
  });
});
