// @vitest-environment node
/**
 * Mid-run steering: `steer.applied` places an instruction the running answer took inside the
 * Turn, and the acknowledgement pinned above the composer leaves when its instruction is taken.
 * Live delivery and replay share the reducer, so both must build the same beats.
 */

import { afterEach, describe, expect, it } from "vitest";

import { _resetAllTurnsForTesting, getTurnSnapshot, pendingSteerAcks, type TurnBeat } from "./turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

const GREEN = "Use green bars instead.";
const TITLE = "Add the title Primes.";

function run(turnId: string, source: "live" | "replay"): TurnBeat[] {
  const at = (seq: number, payload: Record<string, unknown>) => ({ ...payload, seq });
  dispatchToTurnStore("step_list_card_created", at(1, { card_id: turnId }), source, turnId);
  dispatchToTurnStore("narrative.delta", at(2, { card_id: turnId, delta: "Drawing the chart. " }), source, turnId);
  dispatchToTurnStore(
    "step_started",
    at(3, { card_id: turnId, step_id: "s1", step_type: "tool_run", header_zh: "Draw", started_at: "2026-11-15T12:00:00Z", tool_name: "python_sandbox" }),
    source,
    turnId,
  );
  dispatchToTurnStore("narrative.delta", at(4, { card_id: turnId, delta: "The first draft is blue." }), source, turnId);
  // Live frames of this event carry no card_id; the stream's Turn routes it.
  dispatchToTurnStore(
    "steer.applied",
    at(5, { items: [{ text: GREEN, queued_at: "2026-11-15T12:00:25+00:00" }, { text: TITLE }], position: 0, iteration: 1, while_waiting: false, waited_ms: 4000 }),
    source,
    turnId,
  );
  dispatchToTurnStore("narrative.delta", at(6, { card_id: turnId, delta: "Switching to green." }), source, turnId);
  return getTurnSnapshot(turnId)?.beats ?? [];
}

describe("steer.applied", () => {
  it("places each taken instruction after the narrative written so far, and later narrative starts a new segment", () => {
    const beats = run("turn-live", "live");

    expect(beats.map((beat) => beat.kind)).toEqual(["narrative", "step", "narrative", "steer", "steer", "narrative"]);
    expect(beats.filter((beat) => beat.kind === "steer")).toEqual([
      { kind: "steer", key: "p0", text: GREEN, queuedAt: "2026-11-15T12:00:25+00:00" },
      { kind: "steer", key: "p1", text: TITLE },
    ]);
    // Mutation: skip the segment close → "Switching to green." would append to the narrative before the instruction.
    expect(beats.filter((beat) => beat.kind === "narrative").map((beat) => beat.text)).toEqual([
      "Drawing the chart. ",
      "The first draft is blue.",
      "Switching to green.",
    ]);
  });

  it("adds nothing when the same event is delivered again", () => {
    const beats = run("turn-again", "live");
    dispatchToTurnStore("steer.applied", { seq: 5, items: [{ text: GREEN }, { text: TITLE }], position: 0, iteration: 1 }, "live", "turn-again");
    // The same event without its sequence number (another delivery path) is still the same instructions.
    dispatchToTurnStore("steer.applied", { items: [{ text: GREEN }, { text: TITLE }], position: 0, iteration: 1 }, "replay", "turn-again");

    expect(getTurnSnapshot("turn-again")?.beats).toEqual(beats);
  });

  it("builds the same Turn from replay as from live delivery", () => {
    const shape = (beats: TurnBeat[]) =>
      beats.map((beat) => (beat.kind === "narrative" ? { kind: beat.kind, text: beat.text } : beat.kind === "step" ? { kind: beat.kind } : beat));

    expect(shape(run("turn-replayed", "replay"))).toEqual(shape(run("turn-streamed", "live")));
  });

  it("ignores an event without a Turn to route to, and items without text", () => {
    expect(dispatchToTurnStore("steer.applied", { seq: 1, items: [{ text: GREEN }] })).toBe(false);
    dispatchToTurnStore("steer.applied", { seq: 2, items: [{ text: "" }, null, { text: 7 }, { text: GREEN }] }, "live", "turn-partial");

    expect(getTurnSnapshot("turn-partial")?.beats).toEqual([{ kind: "steer", key: "s2:3", text: GREEN }]);
  });
});

describe("pendingSteerAcks", () => {
  const ack = (msgId: string, text: string) => ({ msgId, emoji: "🤝", text });

  it("drops the earliest acknowledgement of each taken instruction and keeps the rest pinned", () => {
    const state = {
      emojiAcks: [ack("m1", GREEN), ack("m2", TITLE), ack("m3", GREEN), { msgId: "m4", emoji: "👍", text: GREEN }, { msgId: "m5", emoji: "🤝" }],
      beats: [{ kind: "steer", key: "5:0", text: GREEN }] as TurnBeat[],
    };

    // The same text sent twice: only the first one was taken; the second is still queued.
    expect(pendingSteerAcks(state).map((item) => item.msgId)).toEqual(["m2", "m3"]);
  });

  it("gives the same answer whether the acknowledgement or the taken event arrived first", () => {
    dispatchToTurnStore("steer.applied", { seq: 3, items: [{ text: GREEN }] }, "live", "turn-order");
    dispatchToTurnStore("emoji_ack", { seq: 4, __turnId: "turn-order", target_msg_id: "m1", emoji: "🤝", text: GREEN }, "live", "turn-order");
    dispatchToTurnStore("emoji_ack", { seq: 5, __turnId: "turn-order", target_msg_id: "m2", emoji: "🤝", text: TITLE }, "live", "turn-order");

    expect(pendingSteerAcks(getTurnSnapshot("turn-order")).map((item) => item.msgId)).toEqual(["m2"]);
  });

  it("is empty without a Turn", () => {
    expect(pendingSteerAcks(undefined)).toEqual([]);
  });
});
