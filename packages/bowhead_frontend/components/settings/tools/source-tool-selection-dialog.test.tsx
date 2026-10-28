import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  listSourceTools,
  patchSource,
  previewSourceImpact,
  TeamToolApiError,
  type ResourceChangeImpact,
  type SourceToolCatalog,
  type TeamToolSourcePublic,
} from "@/lib/team-tool-sources-api";

import { SourceToolSelectionDialog } from "./source-tool-selection-dialog";

vi.mock("@/lib/team-tool-sources-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/team-tool-sources-api")>();
  return {
    ...actual,
    listSourceTools: vi.fn(),
    patchSource: vi.fn(),
    previewSourceImpact: vi.fn(),
  };
});

const SOURCE: TeamToolSourcePublic = {
  id: "ots_remote",
  team_id: "team_1",
  name: "Remote MCP",
  base_url: "https://mcp.example.com/mcp",
  kind: "mcp",
  selected_op_ids: ["search", "fetch"],
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

function tool(operationId: string, imported: boolean, extra: Partial<SourceToolCatalog["items"][number]> = {}) {
  return {
    operation_id: operationId,
    method: "MCP",
    path: "",
    summary: `${operationId} summary`,
    is_write: false,
    approval_executable: null,
    tags: [],
    imported,
    ...extra,
  };
}

const CATALOG: SourceToolCatalog = {
  kind: "mcp",
  imports_all: false,
  updated_at: "2026-09-05T00:00:00Z",
  items: [
    tool("search", true),
    tool("fetch", true),
    tool("drop_table", false, { is_write: true, approval_executable: false }),
  ],
};

function impact(overrides: Partial<ResourceChangeImpact> = {}): ResourceChangeImpact {
  return {
    kind: "resource_change_impact",
    schema_version: 1,
    action: "patch",
    blocking: false,
    summary: {
      current_agent_count: 2,
      running_task_count: 1,
      draft_agent_count: 0,
      historical_agent_count: 0,
      scheduled_binding_count: 1,
      restricted_count: 0,
      completeness: "complete",
      reason_codes: [],
    },
    new_admission: "blocked_for_affected_agents",
    running_execution: "future_dispatch_blocked",
    already_sent_effect: "not_reversible",
    required_follow_up: [{ kind: "schedule_reconfirm", job_id: "job_1" }],
    impact_token: "impact-token-1",
    expires_in_seconds: 300,
    command: { action: "patch", proposed_change: {} },
    reason_codes: [],
    ...overrides,
  };
}

function renderDialog(source: TeamToolSourcePublic = SOURCE, highlight: string[] = []) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  renderIntl(<SourceToolSelectionDialog source={source} highlight={highlight} onClose={onClose} onSaved={onSaved} />);
  return { onClose, onSaved };
}

async function findDialog() {
  const dialog = await screen.findByRole("dialog", { name: "选择源「Remote MCP」导入的工具" });
  await within(dialog).findByRole("checkbox", { name: /search/ });
  return dialog;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SourceToolSelectionDialog", () => {
  it("checks the imported tools, marks new and write tools, and keeps Save off until something changes", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    renderDialog(SOURCE, ["drop_table"]);
    const dialog = await findDialog();

    expect(within(dialog).getByRole("checkbox", { name: /search/ })).toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: /fetch/ })).toBeChecked();
    const write = within(dialog).getByRole("checkbox", { name: /drop_table/ });
    expect(write).not.toBeChecked();
    expect(write.closest("li")).toHaveTextContent("新增");
    expect(write.closest("li")).toHaveTextContent("写 · 当前不可执行");
    expect(dialog).toHaveTextContent("已选 2 / 3 个");
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeDisabled();
  });

  it("saves an added tool directly without an impact preview", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE, op_count: 3 });
    const { onSaved } = renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /drop_table/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ imported: 3, paused_schedule_ids: [] }));
    expect(patchSource).toHaveBeenCalledWith("ots_remote", { selected_op_ids: ["search", "fetch", "drop_table"] });
    expect(previewSourceImpact).not.toHaveBeenCalled();
  });

  it("previews the impact before removing an imported tool and saves with the impact token", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(previewSourceImpact).mockResolvedValue(impact());
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE, op_count: 1, paused_schedule_ids: ["job_1"] });
    const { onSaved } = renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));
    expect(dialog).toHaveTextContent("将移除 1 个已导入的工具，保存前会先显示影响范围。");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    const removed = await within(dialog).findByRole("region", { name: "将移除的已导入工具（1）" });
    expect(within(removed).getByText("fetch")).toBeInTheDocument();
    const grid = within(dialog).getByTestId("tool-choose-impact");
    expect(grid).toHaveTextContent("2 个当前智能体");
    expect(grid).toHaveTextContent("1 个定时任务会暂停，确认依赖后才能恢复。");
    expect(previewSourceImpact).toHaveBeenCalledWith("ots_remote", {
      action: "patch",
      proposed_change: { selected_op_ids: ["search"] },
    });
    expect(patchSource).not.toHaveBeenCalled();

    const confirm = within(dialog).getByRole("button", { name: "确认并保存" });
    expect(confirm.className).toContain("danger");
    fireEvent.click(confirm);

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith({ imported: 1, paused_schedule_ids: ["job_1"] }));
    const [sourceId, body, guard] = vi.mocked(patchSource).mock.calls[0];
    expect(sourceId).toBe("ots_remote");
    expect(body).toEqual({ selected_op_ids: ["search"] });
    expect(guard?.impactToken).toBe("impact-token-1");
    expect(guard?.idempotencyKey.length).toBeGreaterThanOrEqual(16);
  });

  it("goes back to the list from the impact step without saving", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(previewSourceImpact).mockResolvedValue(impact());
    renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    fireEvent.click(await within(dialog).findByRole("button", { name: "返回" }));

    expect(await within(dialog).findByRole("checkbox", { name: /fetch/ })).not.toBeChecked();
    expect(patchSource).not.toHaveBeenCalled();
  });

  it("re-previews and asks again when the confirmed impact went stale", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(previewSourceImpact)
      .mockResolvedValueOnce(impact())
      .mockResolvedValueOnce(impact({ impact_token: "impact-token-2" }));
    vi.mocked(patchSource)
      .mockRejectedValueOnce(new TeamToolApiError(409, "resource_impact_stale", "resource_impact_stale"))
      .mockResolvedValueOnce({ ...SOURCE, op_count: 1 });
    const { onSaved } = renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    fireEvent.click(await within(dialog).findByRole("button", { name: "确认并保存" }));

    expect(await within(dialog).findByText("影响范围刚刚发生变化，已为你刷新；请重新核对后确认。")).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(previewSourceImpact).toHaveBeenCalledTimes(2);

    fireEvent.click(within(dialog).getByRole("button", { name: "确认并保存" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const tokens = vi.mocked(patchSource).mock.calls.map(([, , guard]) => guard?.impactToken);
    expect(tokens).toEqual(["impact-token-1", "impact-token-2"]);
  });

  it("refuses to save when the impact cannot be fully verified", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(previewSourceImpact).mockResolvedValue(impact({ blocking: true, impact_token: null }));
    renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("暂时无法完整核对影响范围，未执行本次变更。");
    expect(within(dialog).queryByRole("button", { name: "确认并保存" })).toBeNull();
    expect(patchSource).not.toHaveBeenCalled();
  });

  it("removes directly when the Impact Guard is off for this deployment", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    vi.mocked(patchSource).mockResolvedValue({ ...SOURCE, op_count: 1 });
    const { onSaved } = renderDialog({ ...SOURCE, impact_guard_enabled: false });
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(previewSourceImpact).not.toHaveBeenCalled();
    expect(patchSource).toHaveBeenCalledWith("ots_remote", { selected_op_ids: ["search"] });
  });

  it("does not allow saving an empty selection", async () => {
    vi.mocked(listSourceTools).mockResolvedValue(CATALOG);
    renderDialog();
    const dialog = await findDialog();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: /search/ }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: /fetch/ }));

    expect(dialog).toHaveTextContent("请至少选择一个工具。如需停用全部工具，请关闭这个来源。");
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeDisabled();
  });

  it("checks every tool for a source that imports all of them", async () => {
    vi.mocked(listSourceTools).mockResolvedValue({
      ...CATALOG,
      imports_all: true,
      items: CATALOG.items.map((item) => ({ ...item, imported: true })),
    });
    renderDialog({ ...SOURCE, selected_op_ids: [] });
    const dialog = await findDialog();

    for (const checkbox of within(dialog).getAllByRole("checkbox")) expect(checkbox).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeDisabled();
  });

  it("shows readable copy when the list fails to load and retries", async () => {
    vi.mocked(listSourceTools)
      .mockRejectedValueOnce(new TeamToolApiError(500, "boom"))
      .mockResolvedValueOnce(CATALOG);
    renderDialog();
    const dialog = await screen.findByRole("dialog", { name: "选择源「Remote MCP」导入的工具" });

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("未能加载工具列表，请重试。");
    fireEvent.click(within(dialog).getByRole("button", { name: "重试" }));
    expect(await within(dialog).findByRole("checkbox", { name: /search/ })).toBeChecked();
  });
});
