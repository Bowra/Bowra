/**
 * step-view · correct failed spawn-step kind (chat-ux-2026-08 closure).
 *
 * Addresses an **upstream fact**: backend hard-codes failed Lead `subagent_spawn` steps as
 * `is_transient: True` (`subagent.py:4019-4022`, never setting `diagnostics.retryable`),
 * making `isRecoverableStepFailure` read a dead peer as a self-healing retry.
 *
 * ★ Why correction belongs in **the single projection**, never one surface:
 *   band, collapsed strip (`StepSummary.countStepIssues`), and expanded ledger
 *   (`AgentTrace.isRecoverableStepFailure`) share `StepView`.
 *   Fixing only the band means it unmounts at completion, while collapsed strip says「已重试 1 次」for the **same turn**:
 *   one condition with two implementations, this sprint's recurring failure.
 *   Five consecutive independent review rounds converged on this location; never move it back into one surface.
 *
 * Each test records its mutation below; do not delete them.
 */
import { describe, expect, it } from "vitest";

import type { PeerTerminal, Step } from "@/lib/turn-store";

import { isRecoverableStepFailure } from "./self-healing";
import { stepToStepView } from "./step-view";

/** Actual backend shape: failed spawn always has kind:"transient", with no diagnostics.retryable. */
function spawnStep(over: Record<string, unknown> = {}): Step {
  return {
    stepId: "spawn",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "subagent_spawn",
    headerZh: "派子 agent",
    label: null,
    state: "failed",
    summary: null,
    durationMs: 1200,
    partialContent: "",
    detail: null,
    subSteps: null,
    outputArtifacts: null,
    codeInput: null,
    error: { kind: "transient", message: "子任务失败" },
    subagentName: null,
    startedAt: new Date(0),
    callId: null,
    subagentId: "lead_1",
    peerProseKey: "p1",
    evtSeq: 2,
    ...over,
  } as unknown as Step;
}

const FAILED: Record<string, PeerTerminal> = { p1: { status: "failed" } };

describe("★★★ spawn 步失败 ≠ 自愈重试", () => {
  it("旧记录里的模型专用派发指导不再出现在回放步骤中", () => {
    const guidance = "Dispatch refused: this Agent is not part of this user's dispatchable roster. Do not retry.";
    const view = stepToStepView(spawnStep({
      peerProseKey: null,
      summary: guidance,
      error: { kind: "transient", message: guidance },
    }), {});
    expect(view.summary).toBeUndefined();
    expect(view.detail).toBeNull();
    expect(view.error?.message).toBe("");
    expect(view.error?.code).toBe("agent_admission_blocked");
    expect(view.error?.uiKind).toBe("permanent");
    expect(isRecoverableStepFailure(view)).toBe(false);
  });

  it("拿到该 peer 的 failed 终态后，投影把 uiKind 纠正成 permanent", () => {
    // Mutation: `correctSpawnFailureKind` directly returns `step.error` → this must fail.
    const view = stepToStepView(spawnStep(), FAILED);
    expect(view.error?.uiKind).toBe("permanent");
    expect(isRecoverableStepFailure(view)).toBe(false);
  });

  it("★★ **压根没有终态**的 spawn 失败也要纠正（否则死掉的 peer 只剩一次假重试）", () => {
    // Independent review 10 caught this and overturned my previous positive control
    // (no terminal = peer still running → unchanged): `subagent.py:5865-5877` wraps `runner.run(...)`
    // in try/finally **without except**, so a throw prevents all terminal event emission,
    // yet step still carries is_transient=True. Backend emits every terminal event **before the spawn tool returns**,
    // so failed spawn without terminal means none will ever arrive, not merely delayed.
    // Without correction, peerFailCount=0 and the dead peer shows **only as one fake retry**.
    // Mutation: revert `status !== undefined && status !== "failed"` to `status !== "failed"` → this must fail.
    const view = stepToStepView(spawnStep(), {});
    expect(view.error?.uiKind).toBe("permanent");
    expect(isRecoverableStepFailure(view)).toBe(false);
  });

  it("★ 正对照：peer 终态是 succeeded 时**不动**后端原值", () => {
    // Guard against marking everything permanent; without this, genuine self-healing retries falsely reported as
    // hard failures (red ✕ + always forced open) still pass — false reporting in the opposite direction.
    // Failed step with a successful peer is a genuine contradiction; do not guess here.
    // Mutation: `correctSpawnFailureKind` unconditionally returns permanent → this must fail.
    const view = stepToStepView(spawnStep(), { p1: { status: "succeeded" } });
    expect(view.error?.uiKind).toBe("transient");
    expect(isRecoverableStepFailure(view)).toBe(true);
  });

  it("★ 普通工具步不受影响（纠正只认 spawn 行）", () => {
    // `peerProseKey` occurs only on `subagent_spawn` steps (turn-store.ts:774-808),
    // but the condition is presence of peerProseKey; pin that ordinary steps remain unaffected.
    // Mutation: change condition from `step.peerProseKey` to `step.subagentId` → this must fail.
    const view = stepToStepView(
      spawnStep({ peerProseKey: null, subagentId: "p1", toolName: "bi_query" }),
      FAILED,
    );
    expect(isRecoverableStepFailure(view)).toBe(true);
  });

  it("没有 error 的 spawn 步不会被凭空造出一个", () => {
    const view = stepToStepView(spawnStep({ state: "active", error: null }), FAILED);
    expect(view.error).toBeNull();
  });
});
