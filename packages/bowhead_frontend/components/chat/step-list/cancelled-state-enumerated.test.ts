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
    const band = buildBandModel({ steps: [stepAt("cancelled")] } as never);
    const tones = JSON.stringify(band ?? {});
    // This step must not appear as active in the band (active = still showing a spinner).
    expect(tones).not.toContain('"tone":"active"');
    // Positive control: the same shape with state=active must actually show active.
    const live = JSON.stringify(buildBandModel({ steps: [stepAt("active")] } as never) ?? {});
    expect(live).toContain('"tone":"active"');
  });
});
