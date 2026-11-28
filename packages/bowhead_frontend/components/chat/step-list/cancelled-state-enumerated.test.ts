// @vitest-environment node
/**
 * Newly added terminal states must not fall through to an active step style.
 *
 * ★ Both variants occurred once during this review and implementation:
 *   · `toneOf` ends with `state === "done" ? "done" : "active"` ⇒ a new terminal state appears to be running
 *   · `stepStateToStatus` ends with `default: return "done"` ⇒ a new terminal state falsely gets a green ✓
 * Both fail silently when only types are added without updating branches; typecheck cannot catch them (there is a default branch).
 *
 * Thus, test **that cancelled does not take the fallback**, rather than its exact rendering.
 */

import { describe, expect, it } from "vitest";

import { stepStateToStatus } from "./agent-grouping";
import { buildBandModel } from "./running-band-model";
import { buildRunGroups } from "./run-groups";
import type { StepView } from "./types";

function stepAt(state: StepView["state"]): StepView {
  return {
    stepId: "s1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "subagent_spawn",
    headerZh: "派子 agent",
    label: null,
    state,
    summary: null,
    durationMs: null,
    partialContent: "",
    detail: null,
    subSteps: null,
    outputArtifacts: null,
    error: null,
    isStreaming: false,
    subagentId: null,
  } as unknown as StepView;
}

describe("cancelled 必须被各处显式列举", () => {
  it("stepStateToStatus：cancelled 不能落进 default 的 done（绿 ✓ = 谎报成功）", () => {
    expect(stepStateToStatus("cancelled")).not.toBe("done");
    expect(stepStateToStatus("cancelled")).toBe("bail");
  });

  it("stepStateToStatus：正对照——done 仍然是 done", () => {
    // Without this, the bad fix of making default return bail would also pass the preceding test.
    expect(stepStateToStatus("done")).toBe("done");
    expect(stepStateToStatus("active")).toBe("run");
  });

  it("running band：cancelled 的步骤不能被当成 active（那就是继续转圈）", () => {
    // An ordinary tool step: a dispatch row is never a line while its helper runs.
    const toolStep = (state: StepView["state"]): StepView => ({ ...stepAt(state), toolName: "bi_query" });
    const lines = buildRunGroups({ steps: [toolStep("cancelled")], peerNames: {} }).flatMap((group) => group.lines);
    // This step must not appear as active in the band (active = still showing a spinner).
    expect(lines.map((line) => line.tone)).not.toContain("active");
    // Positive control: the same shape with state=active must actually show active.
    const live = buildRunGroups({ steps: [toolStep("active")], peerNames: {} }).flatMap((group) => group.lines);
    expect(live.map((line) => line.tone)).toContain("active");
    // The heartbeat model never takes a cancelled step as the current one either.
    expect(buildBandModel({ steps: [stepAt("cancelled")] } as never).phase).not.toBe("running");
  });
});
