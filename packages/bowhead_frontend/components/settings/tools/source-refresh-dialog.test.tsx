import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  applyToolSourceRefresh,
  previewToolSourceRefresh,
  type ToolSourceRefreshPreview,
  type ToolSourceRefreshResult,
} from "@/lib/team-tool-source-refresh-api";
import { TeamToolApiError, type TeamToolSourcePublic } from "@/lib/team-tool-sources-api";

import { SourceRefreshDialog } from "./source-refresh-dialog";

vi.mock("@/lib/team-tool-source-refresh-api", () => ({
  applyToolSourceRefresh: vi.fn(),
  previewToolSourceRefresh: vi.fn(),
}));

const SOURCE: TeamToolSourcePublic = {
  id: "ots_remote",
  team_id: "team_1",
  name: "Remote MCP",
  base_url: "https://mcp.example.com/mcp",
  kind: "mcp",
  selected_op_ids: ["keep", "gone_selected"],
  auth_type: "none",
  has_auth: false,
  visibility: "team_wide",
  status: "enabled",
  tools_count: 3,
  op_count: 2,
  impact_guard_enabled: true,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
};

const EMPTY_SUMMARY = {
  current_agent_count: 0,
  running_task_count: 0,
  draft_agent_count: 0,
  historical_agent_count: 0,
  scheduled_binding_count: 0,
  restricted_count: 0,
  completeness: "complete" as const,
  reason_codes: [],
};

function preview(overrides: Partial<ToolSourceRefreshPreview> = {}): ToolSourceRefreshPreview {
  return {
    kind: "mcp",
    added: [],
    removed: [],
    selected_dropped: [],
    tools_count: 3,
    prompts_added: [],
    prompts_removed: [],
    prompts_count: 0,
    candidate_digest: `sha256:${"a".repeat(64)}`,
    impact_token: "impact-token-1",
    expires_in_seconds: 300,
    summary: EMPTY_SUMMARY,
    required_follow_up: [],
    ...overrides,
  };
}

function applied(overrides: Partial<ToolSourceRefreshResult> = {}): ToolSourceRefreshResult {
  return {
    kind: "mcp",
    added: [],
    removed: [],
    selected_dropped: [],
    tools_count: 3,
    prompts_added: [],
    prompts_removed: [],
    prompts_count: 0,
    paused_schedule_ids: [],
    ...overrides,
  };
}

function renderDialog(source: TeamToolSourcePublic = SOURCE) {
  const onClose = vi.fn();
  const onApplied = vi.fn();
  renderIntl(<SourceRefreshDialog source={source} onClose={onClose} onApplied={onApplied} />);
  return { onClose, onApplied };
}

async function findDialog() {
  return screen.findByRole("dialog", { name: "刷新源「Remote MCP」的工具列表" });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SourceRefreshDialog", () => {
  it("shows a labelled loading state and keeps apply disabled until the preview arrives", async () => {
    let resolve: (value: ToolSourceRefreshPreview) => void = () => {};
    vi.mocked(previewToolSourceRefresh).mockReturnValue(new Promise((done) => { resolve = done; }));
    renderDialog();

    const dialog = await findDialog();
    expect(within(dialog).getByRole("status")).toHaveTextContent("正在向 MCP 服务器获取当前的工具列表…");
    expect(within(dialog).getByRole("button", { name: "更新工具列表" })).toBeDisabled();
    // Initial focus is Cancel so a stray Enter never applies the refresh.
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toHaveFocus());

    resolve(preview({ added: ["new_tool"] }));
    expect(await within(dialog).findByText("new_tool")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "更新工具列表" })).toBeEnabled();
  });

  it("renders added, removed and dropped imported tools in separate groups", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({
      added: ["search_docs"],
      removed: ["gone_selected", "gone_unused"],
      selected_dropped: ["gone_selected"],
      prompts_added: ["summarize"],
      prompts_removed: ["old_prompt"],
    }));
    renderDialog();
    const dialog = await findDialog();

    const added = await within(dialog).findByRole("region", { name: "新增工具（1）" });
    expect(within(added).getByText("search_docs")).toBeInTheDocument();
    expect(added).toHaveTextContent("新增工具不会自动导入。");

    const removed = within(dialog).getByRole("region", { name: "服务器已移除的工具（1）" });
    expect(within(removed).getByText("gone_unused")).toBeInTheDocument();
    expect(within(removed).queryByText("gone_selected")).toBeNull();

    const dropped = within(dialog).getByRole("region", { name: "将失效的已导入工具（1）" });
    expect(within(dropped).getByText("gone_selected")).toBeInTheDocument();

    const prompts = within(dialog).getByRole("region", { name: "提示词变化" });
    expect(within(prompts).getByText("summarize")).toBeInTheDocument();
    expect(within(prompts).getByText("old_prompt")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("更新后，服务器共提供 3 个工具。");
  });

  it("says new tools ship with the source when it imports every tool", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({ added: ["search_docs"] }));
    renderDialog({ ...SOURCE, selected_op_ids: [] });
    const added = await screen.findByRole("region", { name: "新增工具（1）" });
    expect(added).toHaveTextContent("新增工具会一并提供给使用它的智能体");
  });

  it("shows the no-changes state while still allowing the latest definitions to be saved", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview());
    renderDialog();
    const dialog = await findDialog();
    expect(await within(dialog).findByText("没有新增或移除的工具。")).toBeInTheDocument();
    expect(within(dialog).queryByRole("region")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "更新工具列表" })).toBeEnabled();
  });

  it("says the list is up to date, without the may-have-changed note, when the server reports no changed tools", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({ changed: [] }));
    renderDialog();
    const dialog = await findDialog();
    expect(await within(dialog).findByText("工具列表已是最新。")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("服务器当前提供 3 个工具，没有新增、移除或变化。");
    expect(dialog).not.toHaveTextContent("可能仍有更新");
    expect(within(dialog).queryByRole("region")).toBeNull();
  });

  it("lists changed tools with what changed and whether each is imported", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({
      changed: [
        { name: "keep", fields: ["description", "input_schema"], imported: true },
        { name: "spare", fields: ["input_schema"], imported: false },
      ],
    }));
    renderDialog();
    const dialog = await findDialog();

    const changed = await within(dialog).findByRole("region", { name: "有变化的工具（2）" });
    const rows = within(changed).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("keep");
    expect(rows[0]).toHaveTextContent("说明 · 参数 · 已导入");
    expect(rows[1]).toHaveTextContent("spare");
    expect(rows[1]).toHaveTextContent("参数 · 未导入");
    expect(changed).toHaveTextContent("智能体下次调用已导入的工具时即按新定义使用");
    expect(dialog).not.toHaveTextContent("没有新增或移除的工具。");
    expect(within(dialog).getByRole("button", { name: "更新工具列表" }).className).not.toContain("danger");
  });

  it("maps a preview failure to readable copy and retries", async () => {
    vi.mocked(previewToolSourceRefresh)
      .mockRejectedValueOnce(new TeamToolApiError(502, "could not connect to MCP server: ConnectError"))
      .mockResolvedValueOnce(preview({ added: ["search_docs"] }));
    renderDialog();
    const dialog = await findDialog();

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("无法连接 MCP 服务器");
    expect(alert).not.toHaveTextContent("ConnectError");
    expect(within(dialog).queryByRole("button", { name: "更新工具列表" })).toBeNull();

    fireEvent.click(within(dialog).getByRole("button", { name: "重试" }));
    expect(await within(dialog).findByText("search_docs")).toBeInTheDocument();
    expect(previewToolSourceRefresh).toHaveBeenCalledTimes(2);
  });

  it("explains an unavailable impact check without exposing the error code", async () => {
    vi.mocked(previewToolSourceRefresh).mockRejectedValue(
      new TeamToolApiError(503, "resource_impact_projection_incomplete", "resource_impact_projection_incomplete"),
    );
    renderDialog();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("暂时无法核对这次更新对智能体的影响，未做任何更改。");
    expect(alert).not.toHaveTextContent("resource_impact");
  });

  it("applies the previewed candidate with the impact token and an idempotency key", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({ added: ["search_docs"] }));
    const result = applied({ added: ["search_docs"] });
    vi.mocked(applyToolSourceRefresh).mockResolvedValue(result);
    const { onApplied } = renderDialog();
    const dialog = await findDialog();
    await within(dialog).findByText("search_docs");

    const apply = within(dialog).getByRole("button", { name: "更新工具列表" });
    expect(apply.className).not.toContain("danger");
    fireEvent.click(apply);

    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(result));
    const [sourceId, request] = vi.mocked(applyToolSourceRefresh).mock.calls[0];
    expect(sourceId).toBe("ots_remote");
    expect(request.candidateDigest).toBe(`sha256:${"a".repeat(64)}`);
    expect(request.impactToken).toBe("impact-token-1");
    expect(request.idempotencyKey.length).toBeGreaterThanOrEqual(16);
  });

  it("shows the Impact Guard consequences and a danger confirm when imported tools are dropped", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({
      removed: ["gone_selected"],
      selected_dropped: ["gone_selected"],
      summary: { ...EMPTY_SUMMARY, current_agent_count: 2, running_task_count: 1, scheduled_binding_count: 1 },
      required_follow_up: [{ kind: "schedule_reconfirm", job_id: "job_1" }],
    }));
    vi.mocked(applyToolSourceRefresh).mockResolvedValue(applied({
      removed: ["gone_selected"],
      selected_dropped: ["gone_selected"],
      paused_schedule_ids: ["job_1"],
    }));
    const { onApplied } = renderDialog();
    const dialog = await findDialog();

    const impact = await within(dialog).findByTestId("tool-refresh-impact");
    expect(impact).toHaveTextContent("2 个当前智能体");
    expect(impact).toHaveTextContent("1 个运行中任务");
    expect(impact).toHaveTextContent("1 个定时任务会暂停，确认依赖后才能恢复。");
    expect(dialog).toHaveTextContent("这些工具已不在服务器上，更新后智能体无法再调用它们。");

    const confirm = within(dialog).getByRole("button", { name: "更新工具列表" });
    expect(confirm.className).toContain("danger");
    expect(applyToolSourceRefresh).not.toHaveBeenCalled();
    fireEvent.click(confirm);
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(vi.mocked(applyToolSourceRefresh).mock.calls[0][1].impactToken).toBe("impact-token-1");
  });

  it("warns that the source turns off when every imported tool disappears", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({
      removed: ["keep", "gone_selected"],
      selected_dropped: ["keep", "gone_selected"],
    }));
    renderDialog();
    expect(await screen.findByText("这个来源导入的工具已全部从服务器移除，更新后会自动停用该来源。")).toBeInTheDocument();
  });

  it("re-runs the preview when the confirmed impact went stale", async () => {
    vi.mocked(previewToolSourceRefresh)
      .mockResolvedValueOnce(preview({ added: ["first"] }))
      .mockResolvedValueOnce(preview({ added: ["second"], impact_token: "impact-token-2" }));
    vi.mocked(applyToolSourceRefresh).mockRejectedValueOnce(
      new TeamToolApiError(409, "resource_impact_stale", "resource_impact_stale"),
    );
    const { onApplied } = renderDialog();
    const dialog = await findDialog();
    await within(dialog).findByText("first");

    fireEvent.click(within(dialog).getByRole("button", { name: "更新工具列表" }));
    expect(await within(dialog).findByText("second")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("工具列表或其使用情况刚刚发生变化，已重新获取；请核对后再更新。");
    expect(onApplied).not.toHaveBeenCalled();
    expect(previewToolSourceRefresh).toHaveBeenCalledTimes(2);
  });

  it("keeps the idempotency key when retrying an apply that failed for another reason", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({ added: ["search_docs"] }));
    vi.mocked(applyToolSourceRefresh)
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(applied({ added: ["search_docs"] }));
    const { onApplied } = renderDialog();
    const dialog = await findDialog();
    await within(dialog).findByText("search_docs");

    fireEvent.click(within(dialog).getByRole("button", { name: "更新工具列表" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("未能更新工具列表，请重试。");
    fireEvent.click(within(dialog).getByRole("button", { name: "更新工具列表" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));

    const keys = vi.mocked(applyToolSourceRefresh).mock.calls.map(([, request]) => request.idempotencyKey);
    expect(keys[0]).toBe(keys[1]);
  });

  it("closes from Cancel without applying", async () => {
    vi.mocked(previewToolSourceRefresh).mockResolvedValue(preview({ added: ["search_docs"] }));
    const { onClose } = renderDialog();
    const dialog = await findDialog();
    await within(dialog).findByText("search_docs");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(applyToolSourceRefresh).not.toHaveBeenCalled();
  });
});
