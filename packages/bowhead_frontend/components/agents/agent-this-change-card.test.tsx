import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { summarizeDraftChanges } from "@/lib/agent-draft-changes";
import type { AgentDraftChange } from "@/lib/agents-api";
import { AgentThisChangeCard } from "./agent-this-change-card";

const CHANGES: AgentDraftChange[] = [
  {
    path: "/definition/system_prompt", live: "Old.", draft: "Coach.", source: "coach",
    coach: { state: "applied", proposal_id: "acp_1", group_ids: ["g0"], before: "Old.", value: "Coach.", conflict: false },
  },
  { path: "/listing/description", live: "Old summary.", draft: "New summary.", source: "manual" },
  { path: "/definition/tool_refs", live: ["bi_query"], draft: ["bi_query", "powerbi"], source: "ci" },
  // Coach changed this field and the Owner undid it: it matches live again and is not a change.
  {
    path: "/definition/model", live: "analyst", draft: "analyst", source: "manual",
    coach: { state: "undone", proposal_id: "acp_1", group_ids: ["g1"], before: "analyst", value: "fast", conflict: false },
  },
];

function renderCard(props: Partial<Parameters<typeof AgentThisChangeCard>[0]> = {}) {
  const handlers = { onOpenConfig: vi.fn(), onOpenEvaluate: vi.fn(), onViewCheck: vi.fn() };
  renderIntl(
    <AgentThisChangeCard
      changes={CHANGES}
      state="regressions"
      live={7}
      pendingCount={2}
      suites={[{ key: "aes_1", name: "季度经营问答", count: 5 }]}
      {...handlers}
      {...props}
    />,
  );
  return handlers;
}

describe("AgentThisChangeCard", () => {
  afterEach(cleanup);

  it("names each changed section with who changed it, and links each row to its tab", () => {
    const handlers = renderCard();
    expect(screen.getByRole("heading", { name: "本次改动 · 草稿" })).toBeTruthy();
    expect(screen.getByText("相对已发布 v7")).toBeTruthy();
    const changed = screen.getByText("改了什么").closest(".am-this-change-row")!;
    expect(changed.textContent).toBe("改了什么它做什么 2 处Coach 1手动 1 · 它能用什么 1 处外部流水线 1另有 2 条待处理去配置");
    expect(screen.getByText("「季度经营问答」5 条用例")).toBeTruthy();
    expect(screen.getAllByText("有用例变差")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "去配置" }));
    fireEvent.click(screen.getByRole("button", { name: "去评测" }));
    fireEvent.click(screen.getByRole("button", { name: "详情" }));
    expect(handlers.onOpenConfig).toHaveBeenCalledTimes(1);
    expect(handlers.onOpenEvaluate).toHaveBeenCalledTimes(1);
    expect(handlers.onViewCheck).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["passed", "测试通过"],
    ["testing", "测试中"],
    ["not_tested", "还没测试"],
    ["no_cases", "还没测试"],
    ["stale", "结果已过期"],
    ["review", "审批中"],
  ] as const)("states the %s test", (state, text) => {
    renderCard({ state });
    expect(screen.getAllByText(text)).toHaveLength(2);
  });

  it("links a change that isn't tested to the card that tests it instead of empty details", () => {
    const handlers = renderCard({ state: "not_tested" });
    expect(screen.queryByRole("button", { name: "详情" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "去测试" }));
    expect(handlers.onViewCheck).toHaveBeenCalledTimes(1);
  });

  it("uses the singular for one case in English", () => {
    renderIntl(
      <AgentThisChangeCard changes={CHANGES} state="not_tested" live={1} pendingCount={0}
        suites={[{ key: "aes_1", name: "Safety", count: 1 }, { key: "aes_2", name: "Quality", count: 4 }]} onViewCheck={vi.fn()} />,
      { locale: "en-US" },
    );
    expect(screen.getByText("“Safety” · 1 case · “Quality” · 4 cases")).toBeTruthy();
  });

  it("says the draft matches live when nothing differs", () => {
    const handlers = renderCard({ changes: [CHANGES[3]] });
    expect(screen.getByRole("heading", { name: "草稿和已发布的 v7 一样" })).toBeTruthy();
    expect(screen.queryByText("改了什么")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "去配置" }));
    expect(handlers.onOpenConfig).toHaveBeenCalledTimes(1);
  });

  it("says there are no evaluation suites yet, without a link when Evaluate is unavailable", () => {
    renderCard({ suites: [], onOpenEvaluate: undefined });
    expect(screen.getByText("还没有评测组，发布检查只看配置是否完整")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "去评测" })).toBeNull();
  });
});

describe("summarizeDraftChanges", () => {
  it("does not count a field saved empty where live has none", () => {
    expect(summarizeDraftChanges([
      { path: "/definition/system_prompt", live: "Old.", draft: "New.", source: "manual" },
      { path: "/definition/team_tool_connections", live: null, draft: [], source: "manual" },
      { path: "/definition/output_schema", live: null, draft: {}, source: "manual" },
    ])).toEqual([{ section: "does", count: 1, sources: [{ tag: "manual", count: 1 }] }]);
  });


  it("groups changes by Configure section and counts sources in a fixed order", () => {
    expect(summarizeDraftChanges(CHANGES)).toEqual([
      { section: "does", count: 2, sources: [{ tag: "coach", count: 1 }, { tag: "manual", count: 1 }] },
      { section: "uses", count: 1, sources: [{ tag: "ci", count: 1 }] },
    ]);
  });
});
