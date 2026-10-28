/**
 * `llm.retry_scheduled` → transient running status in turn-store.
 *
 * Live SSE and replay share `dispatchToTurnStore`, so the same rules decide both:
 * set on the retry event, cleared by the next model output or terminal event.
 */
import { afterEach, describe, expect, it } from "vitest";

import {
  _resetNarrativeCursorsForTesting,
  dispatchToTurnStore,
  parseModelRetryStatus,
} from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

const T = "turn-retry";

const retryPayload = (over: Record<string, unknown> = {}) => ({
  subagent_id: "sa_lead",
  profile: "lead",
  retry: 1,
  max_retries: 3,
  reason: "timeout",
  delay_s: 1.5,
  ...over,
});

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
});

describe("live retry status", () => {
  it("sets the status from a live frame without card_id, routed by the stream's turn", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    const claimed = dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "live", T);
    expect(claimed).toBe(true);
    expect(getTurnSnapshot(T)?.modelRetry).toEqual({
      retry: 1,
      maxRetries: 3,
      reason: "timeout",
      profile: "lead",
      subagentId: "sa_lead",
      seq: 5,
    });
  });

  it("keeps the latest retry number while retries continue", () => {
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "live", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ retry: 2, reason: "network", seq: 7 }), "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 2, maxRetries: 3, reason: "network" });
  });

  it.each([
    ["narrative.delta", { card_id: T, delta: "Sales rose" }],
    ["step_started", { card_id: T, step_id: "s1", step_type: "tool_run", header_zh: "查询数据" }],
    ["partial_answer", { card_id: T, text: "x" }],
    ["final_answer", { card_id: T, text: "done" }],
    ["done", { card_id: T, status: "completed" }],
    ["turn_failed", { turn_id: T, reason: "llm_provider_timeout" }],
    ["error", { turn_id: T, message: "x" }],
  ])("clears on the next model output or terminal event: %s", (name, payload) => {
    dispatchToTurnStore("llm.retry_scheduled", retryPayload(), "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toBeDefined();
    dispatchToTurnStore(name, payload, "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });

  it("stays while only bookkeeping arrives (the retried attempt's own send marker)", () => {
    dispatchToTurnStore("llm.retry_scheduled", retryPayload(), "live", T);
    dispatchToTurnStore("turn.llm_invocation", { invocation_id: "inv_1" }, "live", T);
    dispatchToTurnStore("emoji_ack", { __turnId: T, emoji: "🤝" }, "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 1 });
  });

  it("keeps the status when a reconnect replays events from before the retry", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T, seq: 1 });
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "Checking", seq: 4 }, "live", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "live", T);
    // Full replay on reconnect: earlier frames arrive again, the retry frame itself is skipped.
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "Checking", seq: 4 }, "replay", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "replay", T);
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 1, seq: 5 });
  });

  it("does not resurrect a cleared status when a reconnect redelivers the same retry frame", () => {
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "live", T);
    dispatchToTurnStore("narrative.delta", { card_id: T, delta: "ok", seq: 6 }, "live", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ seq: 5 }), "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });

  it("ignores a retry that arrives after the card is finalized", () => {
    dispatchToTurnStore("step_list_card_created", { card_id: T });
    dispatchToTurnStore("step_list_card_finalized", { card_id: T, state: "completed" });
    dispatchToTurnStore("llm.retry_scheduled", retryPayload(), "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });

  it("ignores malformed retry counts", () => {
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ retry: 0 }), "live", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ retry: 4, max_retries: 3 }), "live", T);
    dispatchToTurnStore("llm.retry_scheduled", retryPayload({ retry: "1" }), "live", T);
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });

  it("falls back to a generic reason when none is given", () => {
    expect(parseModelRetryStatus({ retry: 1, max_retries: 2 })?.reason).toBe("unknown");
  });
});

describe("replay agrees with live", () => {
  const finishedTurnRows = [
    { event_type: "step_list_card_created", payload: { card_id: T }, seq: 1 },
    { event_type: "llm.retry_scheduled", payload: retryPayload({ seq: 2 }), seq: 2 },
    { event_type: "narrative.delta", payload: { delta: "Revenue grew 12%.", seq: 3 }, seq: 3 },
    { event_type: "done", payload: { status: "completed", seq: 4 }, seq: 4 },
  ];

  it("a replayed finished turn never shows a pending retry", () => {
    replayTurnEventRows(T, finishedTurnRows as never);
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
    expect(getTurnSnapshot(T)?.cardFinalState).toBe("completed");
  });

  it("the same events delivered live end in the same state", () => {
    for (const row of finishedTurnRows) {
      dispatchToTurnStore(row.event_type, row.payload, "live", T);
    }
    expect(getTurnSnapshot(T)?.modelRetry).toBeUndefined();
  });

  it("replaying a still-running turn whose last event is a retry shows it, like live", () => {
    replayTurnEventRows(T, finishedTurnRows.slice(0, 2) as never);
    expect(getTurnSnapshot(T)?.modelRetry).toMatchObject({ retry: 1, reason: "timeout" });
  });
});
