import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import {
  archiveTeamSpace,
  fetchTeamSpaceAdmins,
  fetchTeamSpacesPage,
  restoreTeamSpace,
  type TeamSpaceRow,
} from "@/lib/admin-spaces-api";
import { fetchAdminMembers } from "@/lib/admin-members-api";
import { getSpaceInstructions, listSpaceInstructionVersions } from "@/lib/api";
import { ToastContainer } from "@/components/chat/toast-container";
import { _clearAllToasts } from "@/lib/use-toast";

import { TeamSpacesAdmin } from "./team-spaces-admin";

vi.mock("@/lib/admin-spaces-api", () => ({
  AdminSpacesError: class AdminSpacesError extends Error {
    constructor(public status: number, public code: string, message: string) {
      super(message);
    }
  },
  fetchTeamSpacesPage: vi.fn(),
  createTeamSpace: vi.fn(),
  archiveTeamSpace: vi.fn(),
  restoreTeamSpace: vi.fn(),
  fetchTeamSpaceAdmins: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getSpaceInstructions: vi.fn(),
    listSpaceInstructionVersions: vi.fn(),
    saveSpaceInstructions: vi.fn(),
  };
});

vi.mock("@/lib/admin-members-api", () => ({
  fetchAdminMembers: vi.fn(),
}));

vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({
    items: [],
    activeId: "ws_joined",
    activeName: "Joined",
    loading: false,
    error: null,
    setActiveId: vi.fn(),
    refresh: vi.fn(),
  }),
  invalidateSpaces: vi.fn(),
  switchActiveSpace: vi.fn(),
}));

const spaces: TeamSpaceRow[] = [
  {
    workspace_id: "ws_joined",
    name: "Joined",
    description: null,
    status: "active",
    member_count: 4,
    admin_count: 1,
    caller_membership: "admin",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    revision: 3,
  },
  {
    workspace_id: "ws_unjoined",
    name: "Unjoined",
    description: null,
    status: "active",
    member_count: 2,
    admin_count: 1,
    caller_membership: null,
    created_at: "2026-07-02T00:00:00Z",
    updated_at: "2026-07-02T00:00:00Z",
    revision: 7,
  },
];

describe("TeamSpacesAdmin", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("shows team spaces the administrator has not joined without exposing content", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });

    renderIntl(<TeamSpacesAdmin />);

    expect(await screen.findByText("Unjoined")).toBeTruthy();
    expect(screen.getByText(/你尚未加入此空间/)).toBeTruthy();
    expect(screen.queryByTestId("workspace-i18n")).toBeNull();
  });

  it("requires an explicit recovery administrator when an archived space has none", async () => {
    const archived: TeamSpaceRow = {
      ...spaces[1],
      status: "archived",
      admin_count: 0,
      revision: 8,
    };
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: [archived], next_cursor: null });
    vi.mocked(fetchAdminMembers).mockResolvedValue({
      owner_id: "u_owner",
      your_role: "owner",
      items: [
        {
          user_id: "u_owner",
          email: "owner@example.com",
          display_name: "Owner",
          tier: "ultra",
          is_owner: true,
          team_role: "owner",
          promoted_at: null,
          created_at: "2026-07-01T00:00:00Z",
          last_active_at: null,
        },
      ],
    });
    vi.mocked(restoreTeamSpace).mockResolvedValue({
      ...archived,
      status: "active",
      admin_count: 1,
      revision: 9,
    });

    renderIntl(<TeamSpacesAdmin />);

    await screen.findByText("Unjoined");
    await chooseRowAction("Unjoined 的更多操作", "恢复");
    const dialog = await screen.findByRole("dialog", { name: "恢复" });
    const restoreButton = within(dialog).getByRole("button", { name: "恢复" });
    expect((restoreButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText("恢复管理员"), {
      target: { value: "u_owner" },
    });
    fireEvent.click(restoreButton);

    await waitFor(() =>
      expect(restoreTeamSpace).toHaveBeenCalledWith(
        "ws_unjoined",
        8,
        "u_owner",
      ),
    );
  });

  it("uses server cursors and resets to the first page when searching", async () => {
    vi.mocked(fetchTeamSpacesPage).mockImplementation(async (options) => ({
      items: options.cursor ? [spaces[1]] : [spaces[0]],
      next_cursor: options.cursor || options.q ? null : "page-2",
    }));

    renderIntl(<TeamSpacesAdmin />);
    expect(await screen.findByText("Joined")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText("Unjoined")).toBeTruthy();
    expect(screen.getByText("第 2 页")).toBeTruthy();
    expect(fetchTeamSpacesPage).toHaveBeenCalledWith({
      status: "active", q: "", pageSize: 20, cursor: "page-2",
    });

    fireEvent.change(screen.getByLabelText("搜索空间"), { target: { value: "Joined" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText("第 1 页")).toBeTruthy();
    await waitFor(() => expect(fetchTeamSpacesPage).toHaveBeenLastCalledWith({
      status: "active", q: "Joined", pageSize: 20, cursor: null,
    }));
  });

  it("shows a load error without claiming there are no matches, and retries the same search", async () => {
    let attempts = 0;
    vi.mocked(fetchTeamSpacesPage).mockImplementation(async (options) => {
      if (options.q === "Joined" && ++attempts === 1) {
        throw new Error("temporary gateway error");
      }
      return { items: [spaces[0]], next_cursor: null };
    });

    renderIntl(<TeamSpacesAdmin />);
    await screen.findByText("Joined");
    fireEvent.change(screen.getByLabelText("搜索空间"), { target: { value: "Joined" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索" }));

    expect(await screen.findByText("空间列表加载失败。")).toBeTruthy();
    expect(screen.queryByText("当前筛选条件下没有空间。")).toBeNull();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "搜索" }));
    expect(await screen.findByText("Joined")).toBeTruthy();
    expect(attempts).toBe(2);
  });
});

describe("TeamSpacesAdmin 行尾「⋯」（外观段第四批）", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
    _clearAllToasts();
  });
  const archived: TeamSpaceRow = { ...spaces[1], workspace_id: "ws_archived", name: "Archived", status: "archived", revision: 8 };

  it("offers Administrators, Instructions and Archive for active spaces; Archive runs at once with the row revision and offers undo", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(archiveTeamSpace).mockResolvedValue({ ...spaces[1], status: "archived", revision: 8 });
    renderIntl(<><TeamSpacesAdmin /><ToastContainer /></>);
    await screen.findByText("Unjoined");
    expect(screen.queryByRole("button", { name: "归档" })).toBeNull();
    const menu = await openRowMenu("Unjoined 的更多操作");
    expect(menuItemNames(menu)).toEqual(["管理员", "空间说明", "归档"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "归档" }));
    await waitFor(() => expect(archiveTeamSpace).toHaveBeenCalledWith("ws_unjoined", 7));
    expect(screen.queryByRole("dialog", { name: "归档" })).toBeNull();
    const undoToast = await screen.findByText("已归档空间“Unjoined”");
    expect(undoToast).toBeTruthy();
    expect(restoreTeamSpace).not.toHaveBeenCalled();
  });

  it("lets a team administrator edit a Space's instructions without joining it", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(getSpaceInstructions).mockResolvedValue({
      workspace_id: "ws_unjoined",
      version: 0,
      content: "",
      updated_by_name: null,
      updated_at: null,
      max_chars: 6000,
      can_edit: true,
      archived: false,
      administrators: [],
    });
    vi.mocked(listSpaceInstructionVersions).mockResolvedValue([]);
    renderIntl(<TeamSpacesAdmin />);
    await chooseRowAction("Unjoined 的更多操作", "空间说明", { danger: false });
    const dialog = await screen.findByRole("dialog", { name: "「Unjoined」的空间说明" });
    expect(await within(dialog).findByRole("textbox")).toBeTruthy();
    expect(getSpaceInstructions).toHaveBeenCalledWith("ws_unjoined");
  });

  it("undo restores the space with the post-archive revision", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(archiveTeamSpace).mockResolvedValue({ ...spaces[1], status: "archived", revision: 8 });
    vi.mocked(restoreTeamSpace).mockResolvedValue({ ...spaces[1], status: "active", revision: 9 });
    renderIntl(<><TeamSpacesAdmin /><ToastContainer /></>);
    await chooseRowAction("Unjoined 的更多操作", "归档", { danger: false });
    await screen.findByText("已归档空间“Unjoined”");
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(restoreTeamSpace).toHaveBeenCalledWith("ws_unjoined", 8));
    expect(vi.mocked(restoreTeamSpace).mock.calls[0]).toHaveLength(2);
    await waitFor(() => expect(fetchTeamSpacesPage).toHaveBeenCalledTimes(3));
    expect(screen.queryByTestId("toast-error")).toBeNull();
  });

  it("shows a localized error with Copy diagnostics when undo fails and keeps the undo toast", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(archiveTeamSpace).mockResolvedValue({ ...spaces[1], status: "archived", revision: 8 });
    const { AdminSpacesError } = await import("@/lib/admin-spaces-api");
    vi.mocked(restoreTeamSpace)
      .mockRejectedValueOnce(new AdminSpacesError(503, "store_unavailable", "raw server text"))
      .mockResolvedValueOnce({ ...spaces[1], status: "active", revision: 9 });
    renderIntl(<><TeamSpacesAdmin /><ToastContainer /></>);
    await chooseRowAction("Unjoined 的更多操作", "归档", { danger: false });
    await screen.findByText("已归档空间“Unjoined”");
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));

    const errorToast = await screen.findByTestId("toast-error");
    expect(within(errorToast).getByText(/没能撤销归档，空间“Unjoined”仍是已归档状态/)).toBeTruthy();
    expect(within(errorToast).getByRole("button", { name: "复制诊断" })).toBeTruthy();
    expect(errorToast.textContent).not.toContain("raw server text");
    expect(errorToast.textContent).not.toContain("503");
    await waitFor(() => expect((screen.getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(restoreTeamSpace).toHaveBeenCalledTimes(2));
  });

  it.each([
    ["workspace_not_archived", "空间“Unjoined”已经恢复。", "toast-info"],
    ["stale_workspace_update", "空间“Unjoined”已被他人修改，列表已刷新，请查看它现在的状态。", "toast-warning"],
  ])("refreshes the list, closes the undo toast and explains when the space changed before undo (%s)", async (code, text, testId) => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(archiveTeamSpace).mockResolvedValue({ ...spaces[1], status: "archived", revision: 8 });
    const { AdminSpacesError } = await import("@/lib/admin-spaces-api");
    vi.mocked(restoreTeamSpace).mockRejectedValue(new AdminSpacesError(409, code, "raw server text"));
    renderIntl(<><TeamSpacesAdmin /><ToastContainer /></>);
    await chooseRowAction("Unjoined 的更多操作", "归档", { danger: false });
    await screen.findByText("已归档空间“Unjoined”");
    const loadsBeforeUndo = vi.mocked(fetchTeamSpacesPage).mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));

    const notice = await screen.findByTestId(testId);
    expect(within(notice).getByText(text)).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("button", { name: "撤销" })).toBeNull());
    expect(screen.queryByTestId("toast-error")).toBeNull();
    await waitFor(() => expect(vi.mocked(fetchTeamSpacesPage).mock.calls.length).toBeGreaterThan(loadsBeforeUndo));
    expect(restoreTeamSpace).toHaveBeenCalledTimes(1);
  });

  it("opens the space administrators dialog from Administrators", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: spaces, next_cursor: null });
    vi.mocked(fetchTeamSpaceAdmins).mockResolvedValue({ admins: [], team_members: [] });
    renderIntl(<TeamSpacesAdmin />);
    await chooseRowAction("Unjoined 的更多操作", "管理员", { danger: false });
    await waitFor(() => expect(fetchTeamSpaceAdmins).toHaveBeenCalledWith("ws_unjoined"));
    expect(archiveTeamSpace).not.toHaveBeenCalled();
  });

  it("offers only Restore for archived spaces and opens the restore dialog", async () => {
    vi.mocked(fetchTeamSpacesPage).mockResolvedValue({ items: [archived], next_cursor: null });
    vi.mocked(fetchAdminMembers).mockResolvedValue({ owner_id: "u_owner", your_role: "owner", items: [] });
    renderIntl(<TeamSpacesAdmin />);
    const menu = await openRowMenu("Archived 的更多操作");
    expect(menuItemNames(menu)).toEqual(["恢复"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "恢复" }));
    expect(await screen.findByRole("dialog", { name: "恢复" })).toBeTruthy();
    expect(archiveTeamSpace).not.toHaveBeenCalled();
  });
});
