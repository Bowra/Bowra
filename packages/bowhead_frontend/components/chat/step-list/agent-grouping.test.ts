import { describe, expect, it } from "vitest";

import { buildAgentGrouping, deriveTerminalStatus, stepStateToStatus } from "./agent-grouping";
import type { StepState, StepView } from "./types";

/** Minimal StepView factory — only the fields the grouping reads matter. */
function sv(partial: Partial<StepView> & { stepId: string }): StepView {
  return {
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "done" as StepState,
    ...partial,
  };
}

describe("buildAgentGrouping", () => {
  it("lead-only turn → all steps are leadSteps, no peer blocks", () => {
    const steps = [
      sv({ stepId: "a", toolName: "parse_file", subagentId: "LEAD" }),
      sv({ stepId: "b", toolName: "bi_query", subagentId: "LEAD" }),
    ];
    const g = buildAgentGrouping(steps, {}, {});
    expect(g.leadSteps.map((s) => s.stepId)).toEqual(["a", "b"]);
    expect(g.peerBlocks).toHaveLength(0);
  });

  it("single peer: spawn row → block header, peer inner steps → block body", () => {
    // real shape: spawn row carries the LEAD id on subagentId + the peer id on
    // peerProseKey; the peer's inner steps carry the peer id on subagentId.
    const steps = [
      sv({ stepId: "lead1", toolName: "kb_search", subagentId: "LEAD" }),
      sv({
        stepId: "spawn1", stepType: "subagent", toolName: "subagent_spawn",
        subagentId: "LEAD", peerProseKey: "PEER_c", label: "按渠道拆 Q4", durationText: "34s",
      }),
      sv({ stepId: "p1", toolName: "bi_query", subagentId: "PEER_c" }),
      sv({ stepId: "p2", toolName: "python_sandbox", subagentId: "PEER_c" }),
    ];
    const g = buildAgentGrouping(steps, { PEER_c: "compute" }, { PEER_c: "线上占 41%…" });

    expect(g.leadSteps.map((s) => s.stepId)).toEqual(["lead1"]); // spawn row is NOT a lead step
    expect(g.peerBlocks).toHaveLength(1);
    const blk = g.peerBlocks[0];
    expect(blk.agent).toBe("compute");
    expect(blk.peerId).toBe("PEER_c");
    expect(blk.spawnStep?.stepId).toBe("spawn1");
    expect(blk.steps.map((s) => s.stepId)).toEqual(["p1", "p2"]);
    expect(blk.prose).toBe("线上占 41%…");
    expect(blk.task).toBe("按渠道拆 Q4");
    expect(blk.time).toBe("34s");
    expect(blk.status).toBe("done");
  });

  it("plan path (no spawn step): task comes from peerTasks (agent_spawned.task)", () => {
    // plan-execution turns spawn peers directly → no subagent_spawn step. The block
    // header task falls back to peerTasks[peerId]; disambiguates same-agent peers.
    const steps = [
      sv({ stepId: "c1", subagentId: "C" }),
      sv({ stepId: "d1", subagentId: "D" }),
    ];
    const g = buildAgentGrouping(
      steps,
      { C: "sales-east", D: "sales-south" },
      {},
      { C: "拆解营收增长驱动", D: "计算库存周转天数" },
      { C: "销售专家", D: "销售专家" },
    );
    expect(g.peerBlocks.map((b) => b.agent)).toEqual(["sales-east", "sales-south"]);
    expect(g.peerBlocks.map((b) => b.displayName)).toEqual(["销售专家", "销售专家"]);
    expect(g.peerBlocks.map((b) => b.peerId)).toEqual(["C", "D"]);
    expect(g.peerBlocks[0].spawnStep).toBeNull();
    expect(g.peerBlocks.map((b) => b.task)).toEqual(["拆解营收增长驱动", "计算库存周转天数"]);
  });

  it("two parallel peers (interleaved emit order) → two blocks in first-appearance order", () => {
    const steps = [
      sv({ stepId: "sp_c", stepType: "subagent", toolName: "subagent_spawn", subagentId: "LEAD", peerProseKey: "C" }),
      sv({ stepId: "sp_f", stepType: "subagent", toolName: "subagent_spawn", subagentId: "LEAD", peerProseKey: "F" }),
      sv({ stepId: "c1", subagentId: "C" }), // interleaved
      sv({ stepId: "f1", subagentId: "F" }),
      sv({ stepId: "c2", subagentId: "C" }),
      sv({ stepId: "f2", subagentId: "F" }),
    ];
    const g = buildAgentGrouping(steps, { C: "compute", F: "financial-analyst" }, {});
    expect(g.leadSteps).toHaveLength(0);
    expect(g.peerBlocks.map((b) => b.agent)).toEqual(["compute", "financial-analyst"]);
    expect(g.peerBlocks[0].steps.map((s) => s.stepId)).toEqual(["c1", "c2"]);
    expect(g.peerBlocks[1].steps.map((s) => s.stepId)).toEqual(["f1", "f2"]);
  });

  it("status: active inner step → run; failed → part (per-step FailureAckCard handles it)", () => {
    const run = buildAgentGrouping(
      [
        sv({ stepId: "sp", stepType: "subagent", toolName: "subagent_spawn", subagentId: "L", peerProseKey: "P", state: "active" }),
        sv({ stepId: "p1", subagentId: "P", state: "active" }),
      ],
      { P: "compute" }, {},
    );
    expect(run.peerBlocks[0].status).toBe("run");

    const fail = buildAgentGrouping(
      [
        sv({ stepId: "sp", stepType: "subagent", toolName: "subagent_spawn", subagentId: "L", peerProseKey: "P", state: "done" }),
        sv({ stepId: "p1", subagentId: "P", state: "failed" }),
      ],
      { P: "compute" }, {},
    );
    expect(fail.peerBlocks[0].status).toBe("part");
  });

  it("peer terminal wins child-step fallback and completed Lead turns render failed peers as bail", () => {
    const steps = [
      sv({
        stepId: "sp", stepType: "subagent", toolName: "subagent_spawn",
        subagentId: "L", peerProseKey: "P", state: "active",
      }),
    ];
    const terminal = {
      P: { status: "failed" as const, durationMs: 2400, failureReason: "peer timeout" },
    };

    const runningTurn = buildAgentGrouping(
      steps, { P: "compute" }, {}, {}, {}, terminal,
    );
    expect(runningTurn.peerBlocks[0].status).toBe("fail");
    expect(runningTurn.peerBlocks[0].time).toBe("2.4s");
    expect(runningTurn.peerBlocks[0].failureReason).toBe("peer timeout");

    const recoveredTurn = buildAgentGrouping(
      steps, { P: "compute" }, {}, {}, {}, terminal, true,
    );
    expect(recoveredTurn.peerBlocks[0].status).toBe("bail");
  });

  it("peer-only terminal with zero child steps still creates a visible block", () => {
    const g = buildAgentGrouping(
      [],
      { P: "financial-analyst" },
      { P: "已完成财务健康度检查" },
      { P: "检查利润率与现金流" },
      { P: "财务专家" },
      { P: { status: "succeeded", durationMs: 900 } },
    );

    expect(g.leadSteps).toHaveLength(0);
    expect(g.peerBlocks).toHaveLength(1);
    expect(g.peerBlocks[0]).toMatchObject({
      peerId: "P",
      agent: "financial-analyst",
      displayName: "财务专家",
      task: "检查利润率与现金流",
      time: "900ms",
      status: "done",
      prose: "已完成财务健康度检查",
    });
    expect(g.peerBlocks[0].steps).toHaveLength(0);
  });

  it("agent_spawned not yet seen → no named peer block and no internal-id header", () => {
    const steps = [
      sv({ stepId: "sp", stepType: "subagent", toolName: "subagent_spawn", subagentId: "L", peerProseKey: "P" }),
      sv({ stepId: "p1", subagentId: "P" }),
    ];
    const g = buildAgentGrouping(steps, {}, {});
    expect(g.peerBlocks).toHaveLength(0);
    // Both rows remain neutral Lead activity until authoritative identity lands.
    expect(g.leadSteps.map((s) => s.stepId)).toEqual(["sp", "p1"]);
  });
});

describe("stepStateToStatus", () => {
  it("maps backend StepState → block glyph status", () => {
    expect(stepStateToStatus("active")).toBe("run");
    expect(stepStateToStatus("failed")).toBe("fail");
    expect(stepStateToStatus("partial")).toBe("part");
    expect(stepStateToStatus("pending")).toBe("pend");
    expect(stepStateToStatus("done")).toBe("done");
  });
});

describe("deriveTerminalStatus for an @Agent continuation", () => {
  it("shows the grouped Agent as running while a continuation runs", () => {
    expect(
      deriveTerminalStatus({ status: "succeeded", rerunning: true }, false),
    ).toBe("run");
    expect(
      deriveTerminalStatus(
        {
          status: "succeeded",
          earlierSucceeded: true,
          continuationFailureReason: "peer_empty_result",
        },
        false,
      ),
    ).toBe("done");
  });
});
