import { afterEach, describe, expect, it } from "vitest";
import { dispatchToTurnStore } from "./turn-store-dispatch";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyCardFinalized,
  applyCardResumed,
  applyClarification,
  applyEmojiAck,
  applyFinalAnswer,
  applyNarrativeAppend,
  applyPeerProse,
  applyPeerProseKey,
  applySteerBuffered,
  applyStepDone,
  applyStepProgress,
  applyStepStarted,
  applyTurnDuration,
  failActiveSteps,
  getTurnSnapshot,
  hasActiveToolStep,
  inferFailureUiKind,
  pushPickerBeat,
  resetTurn,
  selectAnswerPayload,
  selectNarrativeBuffer,
  subscribeTurn,
  _pendingProseKeyCountForTesting,
} from "./turn-store";

const T = "turn-1";

afterEach(() => {
  _resetAllTurnsForTesting();
});

describe("turn-store — card lifecycle", () => {
  it("applyCardCreated initializes empty state, idempotent on second call", () => {
    applyCardCreated({ turnId: T });
    const s1 = getTurnSnapshot(T);
    expect(s1?.cardCreated).toBe(true);
    expect(s1?.cardFinalized).toBe(false);
    expect(s1?.steps).toEqual([]);

    applyCardCreated({ turnId: T });
    const s2 = getTurnSnapshot(T);
    expect(s2).toBe(s1);
  });

  it("applyCardFinalized sets terminal state", () => {
    applyCardCreated({ turnId: T });
    applyCardFinalized({ turnId: T, finalState: "completed" });
    const s = getTurnSnapshot(T);
    expect(s?.cardFinalized).toBe(true);
    expect(s?.cardFinalState).toBe("completed");
  });

  // chat-ux-2026-08: sole writer of wall-clock duration. `applyCardFinalized` previously accepted
  // a `totalDurationMs?` argument, but `step_list_card_finalized` wire payload has no
  // duration, so no production caller supplied it → field always null, collapsed strip always using an approximate step sum.
  it("applyTurnDuration 记下墙钟；0 / 负数 / 非有限值不当真", () => {
    applyCardCreated({ turnId: T });
    applyTurnDuration({ turnId: T, totalDurationMs: 124_000 });
    expect(getTurnSnapshot(T)?.totalDurationMs).toBe(124_000);

    const before = getTurnSnapshot(T);
    applyTurnDuration({ turnId: T, totalDurationMs: 0 });
    applyTurnDuration({ turnId: T, totalDurationMs: -1 });
    applyTurnDuration({ turnId: T, totalDurationMs: Number.NaN });
    // Writing the same value creates no new snapshot (avoid pointless rerenders).
    applyTurnDuration({ turnId: T, totalDurationMs: 124_000 });
    expect(getTurnSnapshot(T)).toBe(before);
    expect(getTurnSnapshot(T)?.totalDurationMs).toBe(124_000);
  });

  it("applyCardResumed atomically clears terminal metadata and is idempotent", () => {
    applyCardCreated({ turnId: T });
    applyCardFinalized({ turnId: T, finalState: "failed" });
    applyTurnDuration({ turnId: T, totalDurationMs: 7500 });

    applyCardResumed({ turnId: T });
    const reopened = getTurnSnapshot(T);
    expect(reopened?.cardFinalized).toBe(false);
    expect(reopened?.cardFinalState).toBeNull();
    expect(reopened?.totalDurationMs).toBeNull();

    applyCardResumed({ turnId: T });
    expect(getTurnSnapshot(T)).toBe(reopened);
  });
});

describe("turn-store — step lifecycle", () => {
  it("step_started → step_progress (delta append) → step_done", () => {
    applyCardCreated({ turnId: T });

    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "lead_thinking_writing",
      toolName: "lead_thinking_writing_python",
      headerZh: "正在写代码",
    });

    let s = getTurnSnapshot(T);
    expect(s?.steps.length).toBe(1);
    expect(s?.steps[0]?.state).toBe("active");
    expect(s?.steps[0]?.partialContent).toBe("");

    applyStepProgress({ turnId: T, stepId: "s1", contentDelta: "import pandas" });
    applyStepProgress({ turnId: T, stepId: "s1", contentDelta: " as pd\n" });

    s = getTurnSnapshot(T);
    expect(s?.steps[0]?.partialContent).toBe("import pandas as pd\n");

    applyStepDone({
      turnId: T,
      stepId: "s1",
      state: "done",
      summary: "12 行",
      durationMs: 1100,
      detail: { kind: "python", code: "import pandas as pd\n# more" },
    });

    s = getTurnSnapshot(T);
    expect(s?.steps[0]?.state).toBe("done");
    expect(s?.steps[0]?.summary).toBe("12 行");
    expect(s?.steps[0]?.durationMs).toBe(1100);
    expect(s?.steps[0]?.detail?.kind).toBe("python");
  });

  it("step_started for duplicate id replaces in place (resume safety)", () => {
    applyCardCreated({ turnId: T });
    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询数据",
      label: "first",
    });
    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询数据",
      label: "second",
    });
    const s = getTurnSnapshot(T);
    expect(s?.steps.length).toBe(1);
    expect(s?.steps[0]?.label).toBe("second");
  });

  it("step_done propagates failed state + error", () => {
    applyCardCreated({ turnId: T });
    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "parse",
      toolName: "parse_file",
      headerZh: "解析文件",
    });
    applyStepDone({
      turnId: T,
      stepId: "s1",
      state: "failed",
      error: { kind: "permanent", message: "文件损坏" },
    });
    const s = getTurnSnapshot(T);
    expect(s?.steps[0]?.state).toBe("failed");
    expect(s?.steps[0]?.error?.message).toBe("文件损坏");
  });

  it("concurrency_group_id and parent_step_id are preserved", () => {
    applyCardCreated({ turnId: T });
    applyStepStarted({
      turnId: T,
      stepId: "p1",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      concurrencyGroupId: "g1",
    });
    applyStepStarted({
      turnId: T,
      stepId: "p2",
      stepType: "tool_run",
      toolName: "bi_query",
      headerZh: "查询",
      concurrencyGroupId: "g1",
    });
    applyStepStarted({
      turnId: T,
      stepId: "c1",
      stepType: "tool_run",
      toolName: "web_search",
      headerZh: "搜索网页",
      parentStepId: "p1",
    });
    const s = getTurnSnapshot(T);
    expect(s?.steps.map((x) => x.concurrencyGroupId)).toEqual(["g1", "g1", null]);
    expect(s?.steps.find((x) => x.stepId === "c1")?.parentStepId).toBe("p1");
  });

  it("orphan step_progress / step_done (no prior started) is dropped silently", () => {
    applyCardCreated({ turnId: T });
    applyStepProgress({ turnId: T, stepId: "missing", contentDelta: "x" });
    applyStepDone({ turnId: T, stepId: "missing", state: "done" });
    const s = getTurnSnapshot(T);
    expect(s?.steps).toEqual([]);
  });
});

describe("turn-store — narrative + answer + side channels (Phase 1.7-A beats[])", () => {
  it("narrative beats append (same segId) via selectNarrativeBuffer; answer is unrelated", () => {
    applyCardCreated({ turnId: T });
    applyNarrativeAppend({ turnId: T, text: "好的。", segId: "seg-A" });
    applyNarrativeAppend({ turnId: T, text: "先解析文件。", segId: "seg-A" });
    // (W7: removed applyAnswerToken no-op; per-token answer streaming does not enter beats.)
    const s = getTurnSnapshot(T);
    expect(selectNarrativeBuffer(s)).toBe("好的。先解析文件。");
    // Exactly one narrative beat (same segId appended).
    expect(s?.beats.filter((b) => b.kind === "narrative").length).toBe(1);
    // answer beats only land via applyFinalAnswer in 1.7-A.
    expect(s?.beats.filter((b) => b.kind === "answer").length).toBe(0);
  });

  it("applyFinalAnswer pushes one `answer` beat with payload via selectAnswerPayload", () => {
    applyCardCreated({ turnId: T });
    applyFinalAnswer({
      turnId: T,
      text: "最终答案",
      payload: { turnId: T, exec_summary: "abc" },
    });
    const s = getTurnSnapshot(T);
    const payload = selectAnswerPayload(s);
    expect(payload?.exec_summary).toBe("abc");
    expect(s?.beats.filter((b) => b.kind === "answer").length).toBe(1);
    // O2 (2026-07-03): beat carries text, the shared source of bubble copy in live/archived/F5.
    const answerBeat = s?.beats.find((b) => b.kind === "answer");
    expect(answerBeat && "text" in answerBeat ? answerBeat.text : undefined).toBe("最终答案");
  });

  it("applyFinalAnswer without payload still pushes an `answer` beat (zero payload referencing turn)", () => {
    applyCardCreated({ turnId: T });
    applyFinalAnswer({ turnId: T, text: "ok" });
    const s = getTurnSnapshot(T);
    const payload = selectAnswerPayload(s);
    expect(payload?.turnId).toBe(T);
    expect(payload?.exec_summary).toBeUndefined();
  });

  it("clarification / steerBuffer / emojiAcks track separately from beats", () => {
    applyCardCreated({ turnId: T });
    applyClarification({
      turnId: T,
      clarification: { kind: "options", prompt: "选一个", options: [{ id: "a", label: "A" }] },
    });
    applySteerBuffered({ turnId: T, entry: { text: "按月不按周", ts: "2026-05-22T14:32:00Z" } });
    applyEmojiAck({ turnId: T, ack: { msgId: "u1", emoji: "🤝" } });
    const s = getTurnSnapshot(T);
    expect(s?.clarification?.kind).toBe("options");
    expect(s?.steerBuffer.length).toBe(1);
    expect(s?.emojiAcks[0]?.emoji).toBe("🤝");
    // Picker beats are separate (pushPickerBeat path, not applyClarification).
    expect(s?.beats.length).toBe(0);
  });

  it("pushPickerBeat appends a `picker` beat carrying ThetaClarificationState; idempotent by clarificationId", () => {
    applyCardCreated({ turnId: T });
    const themaState = {
      candidates: ["按月", "按周"],
      fallbackText: null,
      clarificationId: "clar-1",
    };
    pushPickerBeat(T, themaState);
    pushPickerBeat(T, themaState);  // dup → no-op
    const s = getTurnSnapshot(T);
    expect(s?.beats.length).toBe(1);
    const beat = s?.beats[0];
    expect(beat?.kind).toBe("picker");
    if (beat?.kind === "picker") {
      expect(beat.clarification.clarificationId).toBe("clar-1");
      expect(beat.clarification.candidates).toEqual(["按月", "按周"]);
    }
    // Different clarificationId → second beat lands.
    pushPickerBeat(T, { ...themaState, clarificationId: "clar-2" });
    expect(getTurnSnapshot(T)?.beats.length).toBe(2);
  });
});

describe("turn-store — subscriptions", () => {
  it("subscribeTurn fires on commit; returned unsubscribe stops further notifications", () => {
    let count = 0;
    const off = subscribeTurn(T, () => {
      count += 1;
    });
    applyCardCreated({ turnId: T });
    applyStepStarted({
      turnId: T,
      stepId: "s1",
      stepType: "tool_run",
      toolName: "web_search",
      headerZh: "搜索网页",
    });
    expect(count).toBe(2);
    off();
    applyStepDone({ turnId: T, stepId: "s1", state: "done" });
    expect(count).toBe(2);
  });

  it("resetTurn drops state and notifies", () => {
    let count = 0;
    subscribeTurn(T, () => {
      count += 1;
    });
    applyCardCreated({ turnId: T });
    expect(getTurnSnapshot(T)).toBeDefined();
    resetTurn(T);
    expect(getTurnSnapshot(T)).toBeUndefined();
    expect(count).toBe(2);
  });

  it("CTO review P1 #5 — listeners are keyed by turnId; commit on T2 does NOT fire T1 listeners", () => {
    let t1Count = 0;
    let t2Count = 0;
    subscribeTurn("T1", () => { t1Count += 1; });
    subscribeTurn("T2", () => { t2Count += 1; });
    applyCardCreated({ turnId: "T1" });
    expect(t1Count).toBe(1);
    expect(t2Count).toBe(0);
    applyCardCreated({ turnId: "T2" });
    expect(t1Count).toBe(1);
    expect(t2Count).toBe(1);
    applyStepStarted({
      turnId: "T1",
      stepId: "s1",
      stepType: "tool_run",
      toolName: "web_search",
      headerZh: "x",
    });
    expect(t1Count).toBe(2);
    expect(t2Count).toBe(1);
  });
});

describe("turn-store — helpers", () => {
  it("inferFailureUiKind defaults to transient when error or kind missing", () => {
    expect(inferFailureUiKind(null)).toBe("transient");
    expect(inferFailureUiKind({ kind: "permanent", message: "x" })).toBe("permanent");
    expect(inferFailureUiKind({ kind: "transient", message: "x" })).toBe("transient");
    expect(inferFailureUiKind({ kind: "needs_user_input", message: "x" })).toBe("transient");
    expect(inferFailureUiKind({ kind: "unknown", message: "x" })).toBe("transient");
  });
});

describe("applyPeerProseKey (dogfood s_3e854 ④)", () => {
  const spawn = (stepId: string, subagentId?: string) =>
    applyStepStarted({
      turnId: T, stepId, stepType: "subagent",
      toolName: "subagent_spawn", headerZh: "派 peer", subagentId,
    });

  it("keys the most-recent unkeyed subagent_spawn step with the peer id", () => {
    spawn("sp1", "LEAD_id");
    applyPeerProseKey(T, "PEER_id");
    const s = getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp1");
    expect(s?.peerProseKey).toBe("PEER_id");
    expect(s?.subagentId).toBe("LEAD_id"); // own id stays the Lead id (the 3-strikes bug)
  });

  it("★★★ spawn 步**晚于** peer_dispatched 到货时也要认领（原来是永久丢键）", () => {
    // 2026-08-16 discovery: `applyPeerProseKey` previously did `if (idx < 0) return;`,
    // fire-and-forget. Event order is not guaranteed (reconnect/redelivery/concurrent batches); reversed order **permanently loses** this key,
    // breaking spawn-row criteria, doneViaSpawnRow, failed-peer filtering and rowPeerIdOf together.
    // Mutation: remove `_drainPendingProseKey(input.turnId, input.stepId)` from applyStepStarted
    //       or delete the enqueue logic → this case fails.
    applyPeerProseKey(T, "PEER_late", "sp_late");   // ① Arrives first, before this step exists in the store.
    spawn("sp_late", "LEAD_id");                     // ② The step arrives afterward.
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_late")?.peerProseKey)
      .toBe("PEER_late");
  });

  it("★★★ 重连补发的重复 step_started 不许抹掉已认领的 peer 键", () => {
    // Independent review caught `applyStepStarted` rebuilding the entire Step with `peerProseKey` hardcoded null,
    // so reconnect/redelivery erased the key; pending claims could not rescue it (the queue was already consumed).
    // Observed before the fix: PEER_a → null. Same rule as `evtSeq`: redelivery must not erase established facts.
    // Mutation: replace `peerProseKey: prev.steps[idx]?.peerProseKey ?? null` with hardcoded null again → this case fails.
    spawn("sp_dup", "LEAD_id");
    applyPeerProseKey(T, "PEER_a", "sp_dup");
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_dup")?.peerProseKey)
      .toBe("PEER_a");
    spawn("sp_dup", "LEAD_id");   // Redelivery on reconnect.
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_dup")?.peerProseKey)
      .toBe("PEER_a");
  });

  it("★★ 正对照：排队认领必须**按 step id 对号入座**，不许认错行", () => {
    // The queue's real risk: peer A awaits sp_1, but sp_2 arrives first;
    // a loose drain (claim the first whenever queued) attaches A's key to sp_2,
    // wrongly assigning prose ownership, worse than losing the key.
    // Mutation: replace drain's `q.findIndex(e => e.parentStepId === stepId)` with
    //       take index 0 whenever queued → this case fails.
    applyPeerProseKey(T, "PEER_a", "sp_1");   // Queued awaiting sp_1.
    spawn("sp_2", "LEAD_id");                  // But sp_2 arrives first.
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_2")?.peerProseKey)
      .toBeNull();
    // Claim only when sp_1 arrives, and only for that step.
    spawn("sp_1", "LEAD_id");
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_1")?.peerProseKey)
      .toBe("PEER_a");
  });

  it("★ 不带 parent_step_id 的老事件**不入队**（排队等于把猜测延后）", () => {
    // ⚠ This case's first version was **ineffective**: strict draining made queued/unqueued indistinguishable through the public API,
    //   so none of three mutations failed it (caught by independent review). Assert queue length directly instead.
    // Mutation: remove `if (!parentStepId) return;` from `_queueProseKey` → this case fails.
    applyPeerProseKey(T, "PEER_old");                // No parentStepId, and no candidate currently exists.
    expect(_pendingProseKeyCountForTesting(T)).toBe(0);
    spawn("sp_old", "LEAD_id");
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_old")?.peerProseKey)
      .toBeNull();
  });

  it("★★ `resetTurn` 也要清待认领表（否则下一轮同名 stepId 会挂上上一轮的 peer）", () => {
    // Independent review caught the two reset paths differing (only _resetAllTurnsForTesting cleared it).
    // resetTurn runs in mock-runner replay and several component-test cleanups, while mocks **reuse
    // stepId**; leftover entries attach the prior turn's peer id to a new same-named step, claiming the wrong row.
    // Mutation: remove `_pendingProseKeys.delete(turnId)` from `resetTurn` → this case fails.
    applyPeerProseKey(T, "PEER_stale", "sp_reused");
    expect(_pendingProseKeyCountForTesting(T)).toBe(1);
    resetTurn(T);
    expect(_pendingProseKeyCountForTesting(T)).toBe(0);
    // A new turn reuses stepId; it must not claim the previous turn's key.
    spawn("sp_reused", "LEAD_id");
    expect(getTurnSnapshot(T)?.steps.find((x) => x.stepId === "sp_reused")?.peerProseKey)
      .toBeNull();
  });

  it("★ 认领完把条目摘掉，认不上也摘（表不许随会话单调增长）", () => {
    // Independent review caught the old remove-then-claim flow requeuing failed claims after their step had already arrived
    // ⇒ they could never drain. Pin no leftovers on either outcome.
    // Mutation: make `_drainPendingProseKey` call `applyPeerProseKey` again (failed claims requeue) → this case fails.
    applyPeerProseKey(T, "PEER_x", "sp_x");
    expect(_pendingProseKeyCountForTesting(T)).toBe(1);
    // The same stepId arrives, but is not a spawn step → cannot claim.
    applyStepStarted({
      turnId: T, stepId: "sp_x", stepType: "tool_run",
      toolName: "bi_query", headerZh: "查询数据",
    });
    expect(_pendingProseKeyCountForTesting(T)).toBe(0);
  });

  it("is idempotent on replay (same spawnId twice)", () => {
    spawn("sp1");
    applyPeerProseKey(T, "PEER_id");
    applyPeerProseKey(T, "PEER_id");
    expect(
      getTurnSnapshot(T)?.steps.filter((x) => x.peerProseKey === "PEER_id").length,
    ).toBe(1);
  });

  it("pairs sequential spawns to their peers in order", () => {
    spawn("sp1");
    applyPeerProseKey(T, "PEER_1");
    spawn("sp2");
    applyPeerProseKey(T, "PEER_2");
    const steps = getTurnSnapshot(T)?.steps ?? [];
    expect(steps.find((x) => x.stepId === "sp1")?.peerProseKey).toBe("PEER_1");
    expect(steps.find((x) => x.stepId === "sp2")?.peerProseKey).toBe("PEER_2");
  });

  it("pairs parallel spawns by exact parent step id", () => {
    spawn("sp1");
    spawn("sp2");
    applyPeerProseKey(T, "PEER_1", "sp1");
    applyPeerProseKey(T, "PEER_2", "sp2");
    const steps = getTurnSnapshot(T)?.steps ?? [];
    expect(steps.find((x) => x.stepId === "sp1")?.peerProseKey).toBe("PEER_1");
    expect(steps.find((x) => x.stepId === "sp2")?.peerProseKey).toBe("PEER_2");
  });

  it("does not guess between parallel historical spawns without an exact id", () => {
    spawn("sp1");
    spawn("sp2");
    applyPeerProseKey(T, "PEER_1");
    expect(
      getTurnSnapshot(T)?.steps.some((step) => step.peerProseKey === "PEER_1"),
    ).toBe(false);
  });

  it("does not key a non-spawn step", () => {
    applyStepStarted({
      turnId: T, stepId: "t1", stepType: "tool_run",
      toolName: "bi_query", headerZh: "查询",
    });
    applyPeerProseKey(T, "PEER_id");
    expect(
      getTurnSnapshot(T)?.steps.find((x) => x.stepId === "t1")?.peerProseKey,
    ).toBeNull();
  });
});

describe("applyPeerProse (dogfood s_3e854 ④)", () => {
  it("appends tokens per peer id into the turn-store", () => {
    applyPeerProse(T, "P1", "ab");
    applyPeerProse(T, "P1", "cd");
    applyPeerProse(T, "P2", "xy");
    const s = getTurnSnapshot(T);
    expect(s?.peerProse["P1"]).toBe("abcd");
    expect(s?.peerProse["P2"]).toBe("xy");
  });

  it("ignores empty delta / missing ids", () => {
    applyCardCreated({ turnId: T });
    applyPeerProse(T, "P1", "");
    applyPeerProse(T, "", "x");
    expect(getTurnSnapshot(T)?.peerProse).toEqual({});
  });
})

describe("turn-store — failActiveSteps (⑩ interrupted turn)", () => {
  it("fails only active steps, keeps done steps, leaves error null", () => {
    applyCardCreated({ turnId: T });
    applyStepStarted({ turnId: T, stepId: "s1", stepType: "tool_run", toolName: "bi_query", headerZh: "查询数据" });
    applyStepStarted({ turnId: T, stepId: "s2", stepType: "tool_run", toolName: "python_sandbox", headerZh: "运行代码" });
    applyStepDone({ turnId: T, stepId: "s1", state: "done" });
    // s1 done, s2 still active (turn interrupted mid-run) → should flip only s2.
    failActiveSteps(T);
    const steps = getTurnSnapshot(T)!.steps;
    expect(steps.find((s) => s.stepId === "s1")!.state).toBe("done");
    const s2 = steps.find((s) => s.stepId === "s2")!;
    expect(s2.state).toBe("failed");
    expect(s2.error).toBeNull(); // no per-step FailureAckCard
  });

  it("is a no-op (no re-commit) when there are no active steps", () => {
    applyCardCreated({ turnId: T });
    applyStepStarted({ turnId: T, stepId: "s1", stepType: "tool_run", toolName: "bi_query", headerZh: "查询数据" });
    applyStepDone({ turnId: T, stepId: "s1", state: "done" });
    const before = getTurnSnapshot(T)!.steps;
    failActiveSteps(T);
    expect(getTurnSnapshot(T)!.steps).toBe(before);
  });

  it("is safe on an unknown turn", () => {
    expect(() => failActiveSteps("nope")).not.toThrow();
  });
})

it("observes qualified Shopify writes without widening existing exact Tool observers", () => {
  applyCardCreated({ turnId: T });
  dispatchToTurnStore("step_started", {
    card_id: T,
    step_id: "content",
    step_type: "tool_run",
    tool_name: "teammcp.ibt_shop.shopify_apply_content_update",
    header_zh: "Review content",
  });
  expect(hasActiveToolStep(".shopify_apply_content_update", "suffix")).toBe(true);
  expect(hasActiveToolStep("shopify_apply_content_update")).toBe(false);
  expect(hasActiveToolStep("send_email")).toBe(false);
  applyStepDone({ turnId: T, stepId: "content", state: "done" });
  expect(hasActiveToolStep(".shopify_apply_content_update", "suffix")).toBe(false);
});
