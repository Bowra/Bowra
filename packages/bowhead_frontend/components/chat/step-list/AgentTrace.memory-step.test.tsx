import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentBlock } from "./AgentBlock";
import { AgentTrace, isRawResultSummary } from "./AgentTrace";
import type { StepView } from "./types";

afterEach(cleanup);

const RAW = '{"ok": true, "id": "mem_85133efb3d33", "stored": "事实：用户口径：GMV 指含税成交额。", "kind":…';

function step(over: Partial<StepView>): StepView {
  return {
    stepId: "step_1",
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "remember",
    headerZh: "remember",
    label: "用户口径：GMV 指含税成交额。",
    state: "done",
    summary: RAW,
    durationText: "5ms",
    detail: null,
    ...over,
  } as StepView;
}

describe("remember step in the trace (116 1.5.19)", () => {
  it("says it was saved, links to Settings › Memory, and never shows the raw result or the memory id", () => {
    const view = renderIntl(<AgentTrace steps={[step({})]} peerNames={{}} peerProse={{}} />);
    expect(screen.getByText("存入记忆")).toBeInTheDocument();
    expect(screen.getByText("已存入")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /存入记忆/ }));
    const link = screen.getByRole("link", { name: "设置 › 记忆" });
    expect(link.closest("p")?.textContent).toBe("已存入你的长期记忆，可以在设置 › 记忆中查看或修改。");
    expect(link.getAttribute("href")).toMatch(/settings\/.*memory/);
    expect(view.container.textContent).not.toMatch(/mem_|\{"ok"/);
  });

  it("drops any tool's raw-JSON summary fallback but keeps real summaries", () => {
    renderIntl(
      <AgentTrace
        steps={[
          step({ stepId: "a", toolName: "some_new_tool" as StepView["toolName"], headerZh: "新工具", label: "x", summary: '{"rows": [1, 2]}' }),
          step({ stepId: "b", toolName: "kb_search", headerZh: "搜索知识库", label: "y", summary: "找到 5 条" }),
        ]}
        peerNames={{}}
        peerProse={{}}
      />,
    );
    expect(screen.queryByText('{"rows": [1, 2]}')).toBeNull();
    expect(screen.getByText("找到 5 条")).toBeInTheDocument();
  });

  it("recognizes raw results, not ordinary text", () => {
    expect(isRawResultSummary(RAW)).toBe(true);
    expect(isRawResultSummary('[{"a": 1}]')).toBe(true);
    expect(isRawResultSummary("找到 5 条")).toBe(false);
    expect(isRawResultSummary("[1, 2]")).toBe(true);
    expect(isRawResultSummary('["a", "b"]')).toBe(true);
    expect(isRawResultSummary("[1] Source: annual report")).toBe(false);
    expect(isRawResultSummary("[3/5] queries done")).toBe(false);
    expect(isRawResultSummary("{分组} 已完成")).toBe(false);
    expect(isRawResultSummary(null)).toBe(false);
  });
});

describe("agent block names", () => {
  it("names Lead once with the product name, never \"Lead Lead\"", () => {
    const view = renderIntl(<AgentBlock agent="lead" />, { locale: "en-US" });
    expect(view.container.querySelector(".agb-name")?.textContent).toBe("Bowhead");
  });

  it("shows the Marketplace name for a built-in whose snapshot is its identifier", () => {
    const view = renderIntl(<AgentBlock agent="compute" displayName="compute" />);
    expect(view.container.querySelector(".agb-name")?.textContent).toBe("数据与计算分析师");
  });
});
