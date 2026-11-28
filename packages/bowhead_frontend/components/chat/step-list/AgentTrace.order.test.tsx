import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentTrace } from "./AgentTrace";
import type { StepView } from "./types";

afterEach(() => cleanup());

function step(id: string, over: Partial<StepView> = {}): StepView {
  return { stepId: id, parentStepId: null, concurrencyGroupId: null, stepType: "tool_run", headerZh: "查询数据", toolName: "bi_query", state: "done", ...over };
}

const spawn = (id: string, peer: string): StepView =>
  step(id, { stepType: "subagent", toolName: "subagent_spawn", headerZh: "派给助手", subagentId: "LEAD", peerProseKey: peer, label: `任务 ${peer}` });

/** Block order as the reader sees it: "lead" or the helper's task. */
function blockOrder(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".agb")).map((block) =>
    block.querySelector(".agb-task")?.textContent?.trim() || (block.className.includes("ag-lead") ? "lead" : "?"));
}

describe("finished trace order (chat running state R1.5)", () => {
  it("places each helper where Lead dispatched it, between Lead's own runs", () => {
    const { container } = renderIntl(
      <AgentTrace
        steps={[
          step("l1", { subagentId: "LEAD", label: "各门店营业额" }),
          spawn("s1", "P1"),
          step("p1a", { subagentId: "P1", toolName: "python_sandbox", headerZh: "运行代码" }),
          step("l2", { subagentId: "LEAD", label: "汇总" }),
        ]}
        peerNames={{ P1: "compute" }}
        peerProse={{}}
        peerTerminalById={{ P1: { status: "succeeded" } }}
      />,
    );
    expect(blockOrder(container)).toEqual(["lead", "任务 P1", "lead"]);
  });

  it("keeps helpers dispatched together under one heading", () => {
    const { container } = renderIntl(
      <AgentTrace
        steps={[step("l1", { subagentId: "LEAD" }), spawn("s1", "P1"), spawn("s2", "P2")]}
        peerNames={{ P1: "compute", P2: "compute" }}
        peerProse={{}}
        peerTerminalById={{ P1: { status: "succeeded" }, P2: { status: "succeeded" } }}
      />,
    );
    expect(blockOrder(container)).toEqual(["lead", "任务 P1", "任务 P2"]);
    const together = container.querySelector('[data-group-kind="multi-agent"]');
    expect(together?.getAttribute("data-agent-count")).toBe("2");
    expect(together?.querySelectorAll(".agb")).toHaveLength(2);
  });
});
