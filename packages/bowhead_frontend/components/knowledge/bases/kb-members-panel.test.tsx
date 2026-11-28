import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import {
  getKnowledgeBase,
  getKnowledgeBaseMembers,
  notifyKnowledgeBasesChanged,
  putKnowledgeBaseMembers,
  updateKnowledgeBase,
  type KnowledgeBaseDTO,
  type KnowledgeBaseMembers,
} from "@/lib/knowledge-bases-api";
import { resolveToastDuration, toast, type Toast } from "@/lib/use-toast";

import { KbMembersPanel } from "./kb-members-panel";

vi.mock("@/lib/knowledge-bases-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/knowledge-bases-api")>();
  return {
    ...actual,
    getKnowledgeBase: vi.fn(),
    getKnowledgeBaseMembers: vi.fn(),
    putKnowledgeBaseMembers: vi.fn(),
    updateKnowledgeBase: vi.fn(),
    notifyKnowledgeBasesChanged: vi.fn(),
  };
});
vi.mock("@/lib/content-share-api", () => ({
  fetchTeamDirectory: vi.fn(async () => ({ items: [{ id: "usr_me", display_name: "张敏", email_masked: "z***@example.com" }], next_cursor: null })),
  fetchTeamGroups: vi.fn(async () => ({ items: [] })),
}));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, apiJson: vi.fn(async () => ({ members: [{ user_id: "usr_me" }] })) };
});
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ items: [{ id: "ws_store", name: "门店", desc: "" }], activeId: "ws_store", loading: false, error: null }),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { id: "usr_me", capabilities: { knowledge_bases: true } } }),
}));
vi.mock("@/lib/use-toast", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-toast")>();
  return { ...actual, toast: vi.fn(() => "t_1") };
});

const TEAM_KB: KnowledgeBaseDTO = {
  id: "kb_team",
  is_default: false,
  home: "team",
  home_id: "team_1",
  name: "医药临床资料",
  description: null,
  audience: "members",
  audience_level: "use",
  level: "manage",
  owner: { user_id: "usr_me", name: "张敏" },
  can: { add_files: true, manage: true, transfer: true, delete: true },
  file_count: 14,
  failed_count: 0,
  updated_at: null,
  revision: 3,
};

const TEAM_MEMBERS: KnowledgeBaseMembers = {
  knowledge_base_id: "kb_team",
  audience: "members",
  audience_level: "use",
  owner: { user_id: "usr_me", name: "张敏" },
  automatic_managers: [{ user_id: "usr_admin", name: "王管理", reason: "team_admin" }],
  entries: [
    { principal_type: "user", principal_id: "usr_li", label: "李华", level: "edit", state: "active", added_by: "usr_me", created_at: "2026-11-20T00:00:00Z" },
    { principal_type: "space", principal_id: "ws_med", label: "医学部", level: "use", state: "active", added_by: "usr_me", created_at: "2026-11-20T00:00:00Z" },
    { principal_type: "user", principal_id: "usr_gone", label: null, level: "use", state: "not_in_team", added_by: "usr_me", created_at: "2026-11-20T00:00:00Z" },
  ],
  summary: { use: 11, edit: 1, manage: 3, people: 15 },
  can_manage: true,
};

const SPACE_KB: KnowledgeBaseDTO = {
  ...TEAM_KB,
  id: "kb_space",
  home: "space",
  home_id: "ws_store",
  name: "门店运营手册",
  owner: { user_id: "usr_chen", name: "陈思远" },
  revision: 3,
};

const SPACE_MEMBERS: KnowledgeBaseMembers = {
  knowledge_base_id: "kb_space",
  audience: "members",
  audience_level: "use",
  owner: { user_id: "usr_chen", name: "陈思远" },
  automatic_managers: [{ user_id: "usr_lin", name: "林雨桐", reason: "space_admin" }],
  entries: [
    { principal_type: "user", principal_id: "usr_zhou", label: "周明", level: "edit", state: "active", added_by: "usr_chen", created_at: "2026-11-20T00:00:00Z" },
    { principal_type: "user", principal_id: "usr_left", label: "赵离", level: "use", state: "not_in_space", added_by: "usr_chen", created_at: "2026-11-20T00:00:00Z" },
    { principal_type: "group", principal_id: "grp_old", label: null, level: "use", state: "group_removed", added_by: "usr_chen", created_at: "2026-11-20T00:00:00Z" },
  ],
  summary: { use: 4, edit: 1, manage: 2, people: 7 },
  can_manage: true,
};

beforeEach(() => {
  vi.mocked(getKnowledgeBaseMembers).mockResolvedValue(TEAM_MEMBERS);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

async function openLevelMenu(name: string) {
  fireEvent.click(await screen.findByRole("button", { name }));
}

describe("KbMembersPanel", () => {
  it("changes an entry's level with one PUT and refreshes readers", async () => {
    const onChanged = vi.fn();
    vi.mocked(putKnowledgeBaseMembers).mockResolvedValue({
      ...TEAM_MEMBERS,
      entries: TEAM_MEMBERS.entries.map((entry) => (entry.principal_id === "usr_li" ? { ...entry, level: "manage" } : entry)),
    });
    renderIntl(<KbMembersPanel kb={TEAM_KB} onChanged={onChanged} />);
    await openLevelMenu("李华的权限 可编辑");
    fireEvent.click(screen.getByRole("menuitemradio", { name: "可管理" }));
    await waitFor(() =>
      expect(putKnowledgeBaseMembers).toHaveBeenCalledWith("kb_team", [{ principal_type: "user", principal_id: "usr_li", level: "manage" }]),
    );
    expect(await screen.findByRole("button", { name: "李华的权限 可管理" })).toBeTruthy();
    expect(notifyKnowledgeBasesChanged).toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalled();
  });

  it("removes an entry at once and its Undo toast (at least 8 s) re-adds it with its level", async () => {
    const without = { ...TEAM_MEMBERS, entries: TEAM_MEMBERS.entries.filter((entry) => entry.principal_id !== "usr_li") };
    vi.mocked(putKnowledgeBaseMembers).mockResolvedValueOnce(without).mockResolvedValueOnce(TEAM_MEMBERS);
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    fireEvent.click(await screen.findByRole("button", { name: "李华的更多操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "移除" }));
    await waitFor(() =>
      expect(putKnowledgeBaseMembers).toHaveBeenCalledWith("kb_team", [{ principal_type: "user", principal_id: "usr_li", level: null }]),
    );
    await waitFor(() => expect(screen.queryByText("李华")).toBeNull());
    const pushed = vi.mocked(toast).mock.calls[0][0] as Omit<Toast, "id" | "count">;
    expect(pushed.undo).toBe(true);
    expect(pushed.text).toBe("已从名单移除「李华」");
    expect(resolveToastDuration(pushed)).toBeGreaterThanOrEqual(8000);
    await act(async () => {
      await pushed.action!.onAction();
    });
    expect(putKnowledgeBaseMembers).toHaveBeenLastCalledWith("kb_team", [{ principal_type: "user", principal_id: "usr_li", level: "edit" }]);
    expect(await screen.findByText("李华")).toBeTruthy();
  });

  it("lists the owner and automatic managers without level choices or remove", async () => {
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    const ownerRow = (await screen.findByText("张敏（你）")).closest("tr")!;
    expect(within(ownerRow).getByText("所有者")).toBeTruthy();
    expect(within(ownerRow).getByText("可管理")).toBeTruthy();
    expect(within(ownerRow).queryByRole("button")).toBeNull();
    const adminRow = screen.getByText("王管理").closest("tr")!;
    expect(within(adminRow).getByText("团队负责人/管理员 · 自动")).toBeTruthy();
    expect(within(adminRow).queryByRole("button")).toBeNull();
    expect(screen.getByText("15 人可使用 · 1 人可编辑 · 3 人可管理")).toBeTruthy();
  });

  it("shows Space and stale entries with their notes; a stale entry can only be removed", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue(SPACE_MEMBERS);
    renderIntl(<KbMembersPanel kb={SPACE_KB} />);
    const left = (await screen.findByText("赵离")).closest("tr")!;
    expect(within(left).getByText("不在本空间，重新加入后恢复")).toBeTruthy();
    expect(within(left).queryByRole("button", { name: /赵离的权限/ })).toBeNull();
    expect(within(left).getByRole("button", { name: "赵离的更多操作" })).toBeTruthy();
    const group = screen.getByText("已删除的群组").closest("tr")!;
    expect(within(group).getByText("群组已删除或停用")).toBeTruthy();
    expect(within(group).getByText("群组")).toBeTruthy();
    expect(screen.getByText("空间管理员 · 自动")).toBeTruthy();
  });

  it("changes the audience of a named Space knowledge base with its revision", async () => {
    // The server's state: the members read after a PATCH reports the new audience.
    let server = { ...SPACE_MEMBERS };
    let serverRevision = 3;
    vi.mocked(getKnowledgeBaseMembers).mockImplementation(async () => server);
    vi.mocked(updateKnowledgeBase).mockImplementation(async (_id, body) => {
      server = { ...server, audience: body.audience!, audience_level: body.audience_level! };
      serverRevision += 1;
      return { ...SPACE_KB, audience: server.audience, audience_level: server.audience_level, revision: serverRevision };
    });
    const onChanged = vi.fn();
    renderIntl(<KbMembersPanel kb={SPACE_KB} onChanged={onChanged} />);
    fireEvent.click(await screen.findByRole("button", { name: "可见范围 仅成员" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "空间「门店」所有成员" }));
    await waitFor(() => expect(updateKnowledgeBase).toHaveBeenCalledWith("kb_space", { revision: 3, audience: "space", audience_level: "use" }));
    expect(onChanged).toHaveBeenCalled();
    const edit = await screen.findByRole("radio", { name: "编辑" });
    fireEvent.click(edit);
    await waitFor(() => expect(updateKnowledgeBase).toHaveBeenLastCalledWith("kb_space", { revision: 4, audience: "space", audience_level: "edit" }));
    expect(screen.getByText("只在「门店」空间的对话、智能体和文档矩阵里检索。要在其他空间使用，请在团队里新建知识库。")).toBeTruthy();
  });

  it("offers Team knowledge bases members only or everyone in the Team", async () => {
    vi.mocked(updateKnowledgeBase).mockResolvedValue({ ...TEAM_KB, audience: "team", revision: 4 });
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    fireEvent.click(await screen.findByRole("button", { name: "可见范围 仅成员" }));
    expect(screen.queryByRole("menuitemradio", { name: /空间/ })).toBeNull();
    fireEvent.click(screen.getByRole("menuitemradio", { name: "团队所有人" }));
    await waitFor(() => expect(updateKnowledgeBase).toHaveBeenCalledWith("kb_team", { revision: 3, audience: "team", audience_level: "use" }));
  });

  it("reloads the current settings after a revision conflict and says what to do", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue(SPACE_MEMBERS);
    vi.mocked(updateKnowledgeBase).mockRejectedValue(new ApiError(409, "conflict", { code: "knowledge_base_revision_conflict" }));
    vi.mocked(getKnowledgeBase).mockResolvedValue({ ...SPACE_KB, audience: "space", audience_level: "edit", revision: 7 });
    renderIntl(<KbMembersPanel kb={SPACE_KB} />);
    fireEvent.click(await screen.findByRole("button", { name: "可见范围 仅成员" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "空间「门店」所有成员" }));
    expect(await screen.findByText("其他人刚修改了这个知识库，下面已显示最新设置；请确认后再改一次。")).toBeTruthy();
    expect(getKnowledgeBase).toHaveBeenCalledWith("kb_space");
    expect(screen.getByRole("button", { name: "可见范围 空间「门店」所有成员" })).toBeTruthy();
  });

  it("keeps a default knowledge base's audience fixed and says how to give more rights", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({ ...SPACE_MEMBERS, audience: "space", audience_level: "edit", owner: null });
    renderIntl(<KbMembersPanel kb={{ ...SPACE_KB, is_default: true, name: null, audience: "space", audience_level: "edit", owner: null, can: { ...SPACE_KB.can, transfer: false, delete: false } }} />);
    expect(await screen.findByText("空间成员可编辑")).toBeTruthy();
    expect(screen.getByText("默认知识库的可见范围固定不变。要给更多人更高的权限，请把他们、群组或空间加入名单。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /可见范围/ })).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
    // The member list can still raise rights (C6), and C2 is explained.
    expect(screen.getByRole("button", { name: "添加成员" })).toBeTruthy();
    expect(screen.getByText(/同时是本空间成员的团队负责人和团队管理员/)).toBeTruthy();
  });

  it("is read-only for people who cannot manage it", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({ ...TEAM_MEMBERS, owner: { user_id: "usr_zhang", name: "张敏" }, can_manage: false });
    renderIntl(
      <KbMembersPanel
        kb={{ ...TEAM_KB, level: "use", owner: { user_id: "usr_zhang", name: "张敏" }, can: { add_files: false, manage: false, transfer: false, delete: false } }}
      />,
    );
    expect(await screen.findByText("李华")).toBeTruthy();
    expect(screen.getByText("可编辑")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /的权限/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /更多操作/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /添加成员/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /可见范围/ })).toBeNull();
    expect(screen.getByText("仅成员")).toBeTruthy();
    expect(screen.getByText("只有管理者可以修改可见范围和名单。需要更多权限，请联系所有者张敏。")).toBeTruthy();
  });

  it("explains that My knowledge bases are only yours and cannot be shared", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({
      ...TEAM_MEMBERS,
      audience: "owner",
      automatic_managers: [],
      entries: [],
      summary: { use: 0, edit: 0, manage: 1, people: 1 },
    });
    renderIntl(<KbMembersPanel kb={{ ...TEAM_KB, home: "personal", home_id: "usr_me", audience: "owner", can: { ...TEAM_KB.can, transfer: false } }} />);
    expect(await screen.findByText("仅自己")).toBeTruthy();
    expect(screen.getByText(/「我的」知识库只属于你自己，不能分享/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /添加成员/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /可见范围/ })).toBeNull();
  });

  it("opens the picker from the list head and adds the chosen person", async () => {
    vi.mocked(putKnowledgeBaseMembers).mockResolvedValue(TEAM_MEMBERS);
    const { fetchTeamDirectory } = await import("@/lib/content-share-api");
    vi.mocked(fetchTeamDirectory).mockResolvedValue({
      items: [{ id: "usr_wang", display_name: "王医生", email_masked: "w***@example.com" }],
      next_cursor: null,
    });
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    fireEvent.click(await screen.findByRole("button", { name: "添加成员或空间" }));
    const input = screen.getByRole("combobox");
    fireEvent.focus(input);
    fireEvent.click(await screen.findByRole("option", { name: /王医生/ }));
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() =>
      expect(putKnowledgeBaseMembers).toHaveBeenCalledWith("kb_team", [{ principal_type: "user", principal_id: "usr_wang", level: "use" }]),
    );
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ kind: "success", text: "已添加「王医生」：可使用" }));
  });

  it("uses list rows instead of the table on phones", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: true,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
      })),
    );
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    expect(await screen.findByRole("list", { name: "名单" })).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByRole("button", { name: "李华的权限 可编辑" })).toBeTruthy();
  });

  it("shows an actionable message when the list cannot be loaded and retries", async () => {
    vi.mocked(getKnowledgeBaseMembers)
      .mockRejectedValueOnce(new ApiError(404, "nf", { code: "knowledge_base_not_found" }))
      .mockResolvedValueOnce(TEAM_MEMBERS);
    renderIntl(<KbMembersPanel kb={TEAM_KB} />);
    expect(await screen.findByText(/成员名单没有加载出来：找不到这个知识库/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("李华")).toBeTruthy();
  });
});
