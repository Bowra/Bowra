import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import {
  applyAgentChangeProposal,
  createAgentCoachProposal,
  dismissAgentChangeProposal,
  getAgentWorkingCopy,
  type AgentChangeProposal,
} from "@/lib/agents-api";
import { aiOptimizeSourceRef } from "@/lib/agent-optimize";
import type { DraftFieldKey } from "@/lib/agent-trial";
import { OPTIMIZE_LONG_WAIT_MS, useInstructionsOptimize, type InstructionsOptimizeOptions } from "./agent-instructions-optimize";

vi.mock("@/lib/agents-api", () => ({
  applyAgentChangeProposal: vi.fn(),
  createAgentCoachProposal: vi.fn(),
  dismissAgentChangeProposal: vi.fn(),
  getAgentWorkingCopy: vi.fn(),
}));

const ORIGINAL = "查门店销售，出个周报。别瞎编。";
const SUGGESTION = "# 职责\n你是门店周报助手。\n# 边界\n只用查到的数字，不编造。\n查不到就如实说明。";

function proposal(overrides: Partial<AgentChangeProposal> = {}): AgentChangeProposal {
  return {
    kind: "agent_change_proposal",
    schema_version: 1,
    proposal_id: "acp_1",
    agent_id: "osa_1",
    source_type: "coach",
    source_ref: aiOptimizeSourceRef("u1"),
    rationale: "保留：门店口径和不编造\n新增：「查不到就如实说明。」\n未配置：web_search",
    status: "open",
    row_version: 1,
    created_at: "2026-11-25T08:00:00Z",
    decided_at: null,
    operations: [
      { op: "replace", path: "/definition/system_prompt", value: SUGGESTION },
      { op: "replace", path: "/listing/description", value: "周报" },
    ],
    atomic_groups: [
      { id: "prompt", operation_indexes: [0], depends_on: [] },
      { id: "listing", operation_indexes: [1], depends_on: [] },
    ],
    suggested_cases: [],
    base: { release_id: "arl_1", working_copy_id: "awc_1", working_copy_row_version: 2 },
    ...overrides,
  };
}

function Harness(props: Partial<InstructionsOptimizeOptions> & { dirtyFields?: DraftFieldKey[] }) {
  const optimize = useInstructionsOptimize({
    agentId: "osa_1",
    agentName: "门店周报",
    enabled: true,
    text: ORIGINAL,
    dirtyFields: [],
    hasDraft: true,
    save: vi.fn(async () => true),
    saveBlocked: null,
    live: "v2",
    facts: async () => ({ tools: ["数据查询"], plugins: ["数据分析"], skills: [], knowledge: ["当前上下文中能读到的知识"] }),
    pending: null,
    onChanged: vi.fn(async () => undefined),
    ...props,
  });
  return <>
    <label htmlFor="prompt">工作说明</label>{optimize.button}
    <textarea id="prompt" readOnly={optimize.waiting} defaultValue={props.text ?? ORIGINAL} />
    {optimize.panel}
    {optimize.dialogs}
  </>;
}

describe("useInstructionsOptimize", () => {
  beforeEach(() => {
    vi.mocked(getAgentWorkingCopy).mockResolvedValue({ working_copy: { row_version: 4 }, live: { release_id: "arl_1" } } as never);
    vi.mocked(applyAgentChangeProposal).mockResolvedValue({ status: "accepted" });
    vi.mocked(dismissAgentChangeProposal).mockResolvedValue(proposal({ status: "dismissed" }));
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it("is disabled while 工作说明 is empty, with the hint", () => {
    renderIntl(<Harness text="  " />);
    const button = screen.getByRole("button", { name: "AI 优化" });
    expect(button).toHaveProperty("disabled", true);
    expect(screen.getByText("先写几句它要做什么").id).toBe(button.getAttribute("aria-describedby"));
  });

  it("saves only 工作说明 silently, tags its own request and states the configured facts", async () => {
    const save = vi.fn(async () => true);
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    renderIntl(<Harness dirtyFields={["instructions"]} save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    expect(await screen.findByRole("heading", { name: "AI 建议的工作说明" })).toBeTruthy();
    expect(save).toHaveBeenCalledOnce();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    const [agentId, goal, key, sourceRef] = vi.mocked(createAgentCoachProposal).mock.calls[0];
    expect(agentId).toBe("osa_1");
    expect(key).toMatch(/\S/);
    expect(sourceRef).toMatch(/^ai_optimize:system_prompt:/);
    expect(goal).toContain("能调用的工具：数据查询");
    expect(goal).toContain("插件：数据分析");
    expect(goal).toContain("技能：无");
    expect(goal).toContain("保留：");
  });

  it("asks before saving other fields, without 只用已保存的草稿", async () => {
    const save = vi.fn(async () => true);
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    renderIntl(<Harness dirtyFields={["instructions", "name"]} save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const dialog = await screen.findByRole("alertdialog", { name: "先保存另外 1 项修改？" });
    expect(within(dialog).queryByRole("button", { name: "只用已保存的草稿" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(save).not.toHaveBeenCalled();
    expect(createAgentCoachProposal).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "保存并优化" }));
    await screen.findByRole("heading", { name: "AI 建议的工作说明" });
    expect(save).toHaveBeenCalledOnce();
  });

  it("creates the draft first when there is none, and names a blocked save instead of starting", async () => {
    const save = vi.fn(async () => true);
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    const { unmount } = renderIntl(<Harness hasDraft={false} save={save} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    await screen.findByRole("heading", { name: "AI 建议的工作说明" });
    expect(save).toHaveBeenCalledOnce();
    unmount();

    const onOpenSection = vi.fn();
    renderIntl(<Harness dirtyFields={["instructions"]} saveBlocked={{ problem: "至少选择一个插件、工具或技能", section: "uses" }} onOpenSection={onOpenSection} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("至少选择一个插件、工具或技能");
    fireEvent.click(within(alert).getByRole("button", { name: "去修改" }));
    expect(onOpenSection).toHaveBeenCalledWith("uses");
    expect(createAgentCoachProposal).toHaveBeenCalledOnce();
  });

  it("keeps 工作说明 read-only while waiting, then offers 不等了 after 30 s and still shows the late result", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolve!: (value: AgentChangeProposal) => void;
    vi.mocked(createAgentCoachProposal).mockReturnValue(new Promise((done) => { resolve = done; }));
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    expect(await screen.findByText("已保存草稿 · 正在优化，通常 5 秒左右")).toBeTruthy();
    expect(screen.getByLabelText("工作说明")).toHaveProperty("readOnly", true);
    expect(screen.queryByRole("button", { name: "不等了" })).toBeNull();
    await act(async () => { vi.advanceTimersByTime(OPTIMIZE_LONG_WAIT_MS); });
    expect(screen.getByText("还在优化…")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "不等了" }));
    expect(screen.getByLabelText("工作说明")).toHaveProperty("readOnly", false);
    expect(screen.getByRole("button", { name: "AI 优化" })).toHaveProperty("disabled", true);
    await act(async () => { resolve(proposal()); });
    expect(await screen.findByRole("heading", { name: "AI 建议的工作说明" })).toBeTruthy();
  });

  it("shows before and after side by side, tags quoted additions and raises both cautions", async () => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const result = await screen.findByTestId("ai-optimize-result");
    await waitFor(() => expect(document.activeElement?.textContent).toBe("AI 建议的工作说明"));
    expect(within(result).getByRole("heading", { name: "现在" })).toBeTruthy();
    expect(within(result).getByRole("heading", { name: "AI 建议" })).toBeTruthy();
    const tagged = [...result.querySelectorAll("[data-added]")].map((line) => line.textContent);
    expect(tagged).toEqual(["新增查不到就如实说明。"]);
    expect(result.textContent).toContain("保留：门店口径和不编造");
    expect(result.textContent).toContain("原来的工作说明只有 15 个字");
    expect(result.textContent).toContain("工作说明里提到了 web_search");
    expect(screen.getByText("AI 建议已生成：新增 1 条")).toBeTruthy();
    expect(within(result).getByRole("button", { name: "用这个版本" }).className).toContain("settings-button--secondary");
  });

  it("says how many of the additions are marked when some are not sentences of the suggestion", async () => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal({ rationale: "保留：门店口径和不编造\n新增：「查不到就如实说明。」 | 按职责、步骤分节\n未配置：无" }));
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const result = await screen.findByTestId("ai-optimize-result");
    expect(result.textContent).toContain("新增 2 条，其中 1 条在下面标了「新增」。");
  });

  it("shows the rationale as written, with no tags, when the labelled lines are missing", async () => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal({ rationale: "分成 2 节，原来的规则都在。" }));
    renderIntl(<Harness text={"字".repeat(120)} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const result = await screen.findByTestId("ai-optimize-result");
    expect(result.textContent).toContain("分成 2 节，原来的规则都在。");
    expect(result.querySelectorAll("[data-added]")).toHaveLength(0);
    expect(within(result).queryByRole("list", { name: "用之前要看的地方" })).toBeNull();
  });

  it("applies only the groups that rewrite 工作说明, on the saved draft, then rereads the page", async () => {
    const onChanged = vi.fn(async () => undefined);
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    renderIntl(<Harness onChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    fireEvent.click(await screen.findByRole("button", { name: "用这个版本" }));
    await waitFor(() => expect(applyAgentChangeProposal).toHaveBeenCalledWith("osa_1", expect.objectContaining({ proposal_id: "acp_1" }), ["prompt"], 4));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(true));
    expect(screen.queryByTestId("ai-optimize-result")).toBeNull();
  });

  it("dismisses and returns focus to the button", async () => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    fireEvent.click(await screen.findByRole("button", { name: "放弃" }));
    await waitFor(() => expect(dismissAgentChangeProposal).toHaveBeenCalledOnce());
    await waitFor(() => expect(document.activeElement?.textContent).toBe("AI 优化"));
    expect(screen.queryByTestId("ai-optimize-result")).toBeNull();
  });

  it("refuses a result that changes no 工作说明 group alone", async () => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal({
      operations: [{ op: "replace", path: "/definition/system_prompt", value: SUGGESTION }, { op: "replace", path: "/listing/description", value: "x" }],
      atomic_groups: [{ id: "both", operation_indexes: [0, 1], depends_on: [] }],
    }));
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    fireEvent.click(await screen.findByRole("button", { name: "用这个版本" }));
    expect((await screen.findByRole("alert")).textContent).toContain("不只改了工作说明");
    expect(applyAgentChangeProposal).not.toHaveBeenCalled();
  });

  it("shows a late result from the workbench after a reload", () => {
    renderIntl(<Harness pending={proposal()} />);
    expect(screen.getByTestId("ai-optimize-result")).toBeTruthy();
    expect(screen.getByRole("button", { name: "AI 优化" })).toHaveProperty("disabled", true);
    expect(screen.getByText("先处理下面的 AI 建议")).toBeTruthy();
  });

  it.each<[string, ApiError, string, string | null]>([
    ["a missing model", new ApiError(422, "x", { code: "team_model_unconfigured" }), "团队还没有配置可用的模型", "复制说明给管理员"],
    ["a timeout", new ApiError(409, "x", { code: "agent_coach_generation_unknown" }), "这次优化没完成（超时或没有回应）", "再试一次"],
    ["no change", new ApiError(422, "x", { code: "no_change" }), "AI 认为现在的写法不用改", null],
    ["a bad format", new ApiError(422, "x", { code: "parse_failed" }), "AI 的结果格式不对", "再试一次"],
    ["a guard", new ApiError(422, "x", { code: "secret_guard" }), "已拦下", "再试一次"],
    ["a non-Owner", new ApiError(404, "x", { code: "agent_owner_required" }), "只有所有者能优化", null],
  ])("names %s and the next step", async (_name, cause, text, action) => {
    vi.mocked(createAgentCoachProposal).mockRejectedValue(cause);
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(text);
    expect(alert.textContent).toContain(cause.code === "team_model_unconfigured" || cause.code === "agent_owner_required" || cause.code === "no_change" ? "" : "草稿没有改动");
    if (action) expect(within(alert).getByRole("button", { name: action })).toBeTruthy();
    else expect(within(alert).queryByRole("button", { name: "再试一次" })).toBeNull();
  });

  it.each<[string, string, string]>([
    ["a version conflict", "agent_working_copy_version_conflict", "草稿在别处改过"],
    ["a merge conflict", "agent_change_merge_conflict", "工作说明在优化之后改过"],
    ["a handled proposal", "agent_change_proposal_stale", "这个建议已经处理过"],
  ])("names %s on apply", async (_name, code, text) => {
    vi.mocked(createAgentCoachProposal).mockResolvedValue(proposal());
    vi.mocked(applyAgentChangeProposal).mockRejectedValue(new ApiError(409, "x", { code }));
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    fireEvent.click(await screen.findByRole("button", { name: "用这个版本" }));
    expect((await screen.findByRole("alert")).textContent).toContain(text);
    if (code === "agent_change_merge_conflict") expect(screen.getByRole("button", { name: "重新优化" })).toBeTruthy();
  });

  it("keeps asking with the same key after a proxy answered 504, until the result arrives", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(createAgentCoachProposal)
      .mockRejectedValueOnce(new ApiError(504, "Gateway Timeout"))
      .mockRejectedValueOnce(new ApiError(409, "x", { code: "agent_coach_generation_running" }))
      .mockResolvedValueOnce(proposal());
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化" }));
    expect(await screen.findByText(/不再等了/)).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(20_000); });
    await act(async () => { vi.advanceTimersByTime(20_000); });
    expect(await screen.findByRole("heading", { name: "AI 建议的工作说明" })).toBeTruthy();
    const keys = vi.mocked(createAgentCoachProposal).mock.calls.map((call) => call[2]);
    expect(new Set(keys).size).toBe(1);
  });
});
