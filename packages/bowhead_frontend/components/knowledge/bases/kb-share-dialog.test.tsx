import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { appPath } from "@/lib/base-path";
import {
  getKnowledgeBaseMembers,
  putKnowledgeBaseMembers,
  updateKnowledgeBase,
  type KnowledgeBaseDTO,
  type KnowledgeBaseMembers,
} from "@/lib/knowledge-bases-api";
import { NARROW_SHEET_MEDIA_QUERY } from "@/components/ui/sheet";
import { toast } from "@/lib/use-toast";

import { KbShareDialog } from "./kb-share-dialog";

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

const MEMBERS: KnowledgeBaseMembers = {
  knowledge_base_id: "kb_team",
  audience: "members",
  audience_level: "use",
  owner: { user_id: "usr_me", name: "张敏" },
  automatic_managers: [{ user_id: "usr_admin", name: "王管理", reason: "team_admin" }],
  entries: [
    { principal_type: "space", principal_id: "ws_med", label: "医学部", level: "use", state: "active", added_by: "usr_me", created_at: "2026-11-20T00:00:00Z" },
    { principal_type: "user", principal_id: "usr_li", label: "李华", level: "edit", state: "active", added_by: "usr_me", created_at: "2026-11-20T00:00:00Z" },
  ],
  summary: { use: 11, edit: 1, manage: 3, people: 15 },
  can_manage: true,
};

const SPACE_KB: KnowledgeBaseDTO = { ...TEAM_KB, id: "kb_space", home: "space", home_id: "ws_store", name: "门店运营手册" };

let narrow = false;
beforeEach(() => {
  narrow = false;
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === NARROW_SHEET_MEDIA_QUERY ? narrow : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  );
  vi.mocked(getKnowledgeBaseMembers).mockResolvedValue(MEMBERS);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("KbShareDialog", () => {
  it("lets managers choose who can use it, add members or Spaces and see the summary", async () => {
    const onClose = vi.fn();
    renderIntl(<KbShareDialog kb={TEAM_KB} onClose={onClose} />);
    const dialog = await screen.findByRole("dialog", { name: "分享「医药临床资料」" });
    expect(within(dialog).getByText("这个知识库放在团队里，团队的所有空间都能使用。")).toBeTruthy();
    const members = await within(dialog).findByRole("radio", { name: /仅成员/ });
    expect(members.getAttribute("aria-checked")).toBe("true");
    expect(within(dialog).getByRole("radio", { name: /团队所有人/ })).toBeTruthy();
    expect(within(dialog).getByRole("combobox", { name: "添加成员或空间" })).toBeTruthy();
    expect(within(dialog).getByText("张敏（你）")).toBeTruthy();
    expect(within(dialog).getByText("团队负责人/管理员 · 自动")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "李华的权限 可编辑" })).toBeTruthy();
    expect(within(dialog).getByText("共 15 人可以使用，其中 1 人可以编辑，3 人可以管理。")).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "完成" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("changes the audience immediately with the knowledge base's revision", async () => {
    vi.mocked(updateKnowledgeBase).mockResolvedValue({ ...TEAM_KB, audience: "team", revision: 4 });
    const onChanged = vi.fn();
    renderIntl(<KbShareDialog kb={TEAM_KB} onClose={vi.fn()} onChanged={onChanged} />);
    fireEvent.click(await screen.findByRole("radio", { name: /团队所有人/ }));
    await waitFor(() => expect(updateKnowledgeBase).toHaveBeenCalledWith("kb_team", { revision: 3, audience: "team", audience_level: "use" }));
    expect(onChanged).toHaveBeenCalled();
  });

  it("offers a Space knowledge base members only or all Space members who can use or edit", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({ ...MEMBERS, knowledge_base_id: "kb_space", entries: [] });
    vi.mocked(updateKnowledgeBase).mockResolvedValue({ ...SPACE_KB, audience: "space", audience_level: "edit", revision: 4 });
    renderIntl(<KbShareDialog kb={SPACE_KB} onClose={vi.fn()} />);
    expect(await screen.findByText("这个知识库放在空间「门店」里，只在这个空间里使用。")).toBeTruthy();
    expect(await screen.findByRole("radio", { name: /空间所有成员 · 可使用/ })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "添加成员" })).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: /空间所有成员 · 可编辑/ }));
    await waitFor(() => expect(updateKnowledgeBase).toHaveBeenCalledWith("kb_space", { revision: 3, audience: "space", audience_level: "edit" }));
  });

  it("removes an entry at once with an Undo toast", async () => {
    vi.mocked(putKnowledgeBaseMembers).mockResolvedValue({ ...MEMBERS, entries: MEMBERS.entries.slice(0, 1) });
    renderIntl(<KbShareDialog kb={TEAM_KB} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "李华的更多操作" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "移除" }));
    await waitFor(() =>
      expect(putKnowledgeBaseMembers).toHaveBeenCalledWith("kb_team", [{ principal_type: "user", principal_id: "usr_li", level: null }]),
    );
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ undo: true, action: expect.objectContaining({ label: "撤销" }) }));
  });

  it("shows the same content read-only under 谁能访问 for people who cannot manage it", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({ ...MEMBERS, owner: { user_id: "usr_zhang", name: "张敏" }, can_manage: false });
    const kb = { ...TEAM_KB, level: "use" as const, owner: { user_id: "usr_zhang", name: "张敏" }, can: { add_files: false, manage: false, transfer: false, delete: false } };
    renderIntl(<KbShareDialog kb={kb} onClose={vi.fn()} />);
    const dialog = await screen.findByRole("dialog", { name: "谁能访问「医药临床资料」" });
    expect(await within(dialog).findByText("李华")).toBeTruthy();
    expect(within(dialog).getByText("仅成员")).toBeTruthy();
    expect(within(dialog).queryByRole("radio")).toBeNull();
    expect(within(dialog).queryByRole("combobox")).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /的权限/ })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /更多操作/ })).toBeNull();
    expect(within(dialog).getByText("共 15 人可以使用，其中 1 人可以编辑，3 人可以管理。")).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: "关闭" })).toBeTruthy();
  });

  it("opens My knowledge bases read-only: they cannot be shared", async () => {
    vi.mocked(getKnowledgeBaseMembers).mockResolvedValue({ ...MEMBERS, audience: "owner", automatic_managers: [], entries: [] });
    renderIntl(<KbShareDialog kb={{ ...TEAM_KB, home: "personal", home_id: "usr_me", name: "读书笔记", audience: "owner" }} onClose={vi.fn()} />);
    const dialog = await screen.findByRole("dialog", { name: "谁能访问「读书笔记」" });
    expect(within(dialog).getByText("这个知识库只属于你，在你所在的每个空间里都能使用。")).toBeTruthy();
    expect(await within(dialog).findByText("仅自己")).toBeTruthy();
    expect(within(dialog).getByText(/「我的」知识库只属于你自己，不能分享/)).toBeTruthy();
    expect(within(dialog).queryByRole("combobox")).toBeNull();
  });

  it("copies the page address and says the link grants nothing", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderIntl(<KbShareDialog kb={TEAM_KB} onClose={vi.fn()} />);
    const copy = await screen.findByRole("button", { name: "复制链接" });
    expect(copy.getAttribute("title")).toBe("只复制这个知识库的地址；打开链接不会获得任何权限。");
    fireEvent.click(copy);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}${appPath("/knowledge/base/?id=kb_team")}`));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith({ kind: "success", text: "已复制链接。打开链接不会获得任何权限，只有能访问这个知识库的人才能看到内容。" }),
    );
  });

  it("is a bottom sheet on phones with the search behind 添加成员", async () => {
    narrow = true;
    renderIntl(<KbShareDialog kb={SPACE_KB} onClose={vi.fn()} />);
    const dialog = await screen.findByRole("dialog", { name: "分享「门店运营手册」" });
    await waitFor(() => expect(dialog.getAttribute("data-dialog-presentation")).toBe("sheet"));
    expect(await within(dialog).findByRole("list", { name: "名单" })).toBeTruthy();
    expect(within(dialog).queryByRole("combobox")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "添加成员" }));
    expect(within(dialog).getByRole("combobox")).toBeTruthy();
  });
});
