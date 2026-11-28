import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError, apiJson } from "@/lib/api-client";
import { fetchTeamDirectory, fetchTeamGroups } from "@/lib/content-share-api";

import { KbMemberPicker, memberInvalidReason } from "./kb-member-picker";

vi.mock("@/lib/content-share-api", () => ({
  fetchTeamDirectory: vi.fn(),
  fetchTeamGroups: vi.fn(),
}));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, apiJson: vi.fn() };
});
vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({
    items: [
      { id: "ws_med", name: "医学部", desc: "" },
      { id: "ws_store", name: "门店", desc: "" },
    ],
    activeId: "ws_store",
    loading: false,
    error: null,
  }),
}));

const PEOPLE = [
  { id: "usr_a", display_name: "安然", email_masked: "a***@example.com" },
  { id: "usr_b", display_name: "白露", email_masked: "b***@example.com" },
  { id: "usr_c", display_name: "陈晨", email_masked: "c***@example.com" },
];
const GROUPS = [{ id: "grp_sales", name: "销售组", member_count: 12 }];

const SPACE_KB = { id: "kb_space", home: "space" as const, home_id: "ws_store" };
const TEAM_KB = { id: "kb_team", home: "team" as const, home_id: "team_1" };

beforeEach(() => {
  vi.mocked(fetchTeamDirectory).mockResolvedValue({ items: PEOPLE, next_cursor: null });
  vi.mocked(fetchTeamGroups).mockResolvedValue({ items: GROUPS });
  vi.mocked(apiJson).mockResolvedValue({ members: [{ user_id: "usr_a" }, { user_id: "usr_c" }] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function invalid(reason: string) {
  return new ApiError(422, "refused", {
    code: "knowledge_base_member_invalid",
    body: { detail: { error: { code: "knowledge_base_member_invalid", reason, hint: "server text" } } },
  });
}

async function openResults() {
  const input = screen.getByRole("combobox");
  fireEvent.focus(input);
  return input;
}

describe("KbMemberPicker", () => {
  it("offers a Space knowledge base only members of its Space, groups, and no Spaces (Q2)", async () => {
    renderIntl(<KbMemberPicker kb={SPACE_KB} excluded={new Set()} onAdd={vi.fn()} />);
    await openResults();
    expect(await screen.findByRole("option", { name: /安然/ })).toBeTruthy();
    expect(screen.getByRole("option", { name: /陈晨/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /白露/ })).toBeNull();
    expect(screen.getByRole("option", { name: /销售组/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /医学部/ })).toBeNull();
    expect(apiJson).toHaveBeenCalledWith("/api/v1/workspaces/ws_store/members");
    expect(screen.getByText(/空间知识库只能添加本空间的成员和群组/)).toBeTruthy();
  });

  it("offers a Team knowledge base people, groups and the user's Spaces, minus listed principals", async () => {
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set(["user:usr_b", "space:ws_store"])} onAdd={vi.fn()} />);
    await openResults();
    expect(await screen.findByRole("option", { name: /安然/ })).toBeTruthy();
    expect(screen.getByRole("option", { name: /医学部/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /门店/ })).toBeNull();
    expect(screen.queryByRole("option", { name: /白露/ })).toBeNull();
    expect(screen.getByRole("option", { name: /销售组/ })).toBeTruthy();
    expect(apiJson).not.toHaveBeenCalled();
  });

  it("searches by name and adds the chosen Space with the chosen level", async () => {
    const onAdd = vi.fn(async () => {});
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set()} onAdd={onAdd} />);
    const input = await openResults();
    fireEvent.change(input, { target: { value: "医学" } });
    await waitFor(() => expect(fetchTeamDirectory).toHaveBeenLastCalledWith("医学", 0));
    fireEvent.click(await screen.findByRole("option", { name: /医学部/ }));
    expect((input as HTMLInputElement).value).toBe("医学部");
    fireEvent.click(screen.getByRole("button", { name: "添加后的权限 可使用" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "可编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() =>
      expect(onAdd).toHaveBeenCalledWith({ principal_type: "space", principal_id: "ws_med", name: "医学部" }, "edit"),
    );
    await waitFor(() => expect((input as HTMLInputElement).value).toBe(""));
  });

  it("adds a group chosen with the keyboard", async () => {
    const onAdd = vi.fn(async () => {});
    renderIntl(<KbMemberPicker kb={SPACE_KB} excluded={new Set()} onAdd={onAdd} />);
    const input = await openResults();
    await screen.findByRole("option", { name: /销售组/ });
    // Groups come before people; the first option is active.
    fireEvent.keyDown(input, { key: "Enter" });
    expect((input as HTMLInputElement).value).toBe("销售组");
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    await waitFor(() => expect(onAdd).toHaveBeenCalledWith({ principal_type: "group", principal_id: "grp_sales", name: "销售组" }, "use"));
  });

  it("keeps Add disabled until a result is chosen", async () => {
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set()} onAdd={vi.fn()} />);
    const add = screen.getByRole("button", { name: "添加" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    const input = await openResults();
    fireEvent.click(await screen.findByRole("option", { name: /安然/ }));
    expect(add.disabled).toBe(false);
    fireEvent.change(input, { target: { value: "安" } });
    expect(add.disabled).toBe(true);
  });

  it.each([
    ["not_in_space", SPACE_KB, "安然不是这个空间的成员。空间知识库只能添加本空间成员；要跨空间共享，请在团队里新建知识库。"],
    ["not_in_team", TEAM_KB, "安然已不在团队里，请选择团队里的其他成员。"],
    ["owner", TEAM_KB, "安然是所有者，已经可以管理这个知识库，无需添加。"],
    ["group_inactive", TEAM_KB, "群组「安然」已停用或已删除，请选择启用中的群组。"],
    ["group_not_applicable", SPACE_KB, "群组「安然」属于其他空间。空间知识库只能添加全团队的群组或本空间的群组。"],
    ["group_not_applicable", TEAM_KB, "群组「安然」只在它所属的空间里生效。团队知识库只能添加全团队的群组；也可以直接添加那个空间。"],
    ["space_entries_team_only", SPACE_KB, "只有团队知识库可以添加空间。这里请添加本空间的成员或群组。"],
    ["space_not_found", TEAM_KB, "空间「安然」已不存在或不属于这个团队，请选择团队里的其他空间。"],
  ])("maps the refusal reason %s to specific copy", async (reason, kb, expected) => {
    const onAdd = vi.fn(async () => {
      throw invalid(reason);
    });
    renderIntl(<KbMemberPicker kb={kb} excluded={new Set()} onAdd={onAdd} />);
    await openResults();
    fireEvent.click(await screen.findByRole("option", { name: /安然/ }));
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect(await screen.findByText(expected)).toBeTruthy();
    expect(screen.queryByText("server text")).toBeNull();
  });

  it("maps the member limit and other failures through the knowledge base error copy", async () => {
    const onAdd = vi.fn(async () => {
      throw new ApiError(409, "limit", { code: "knowledge_base_member_limit" });
    });
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set()} onAdd={onAdd} />);
    await openResults();
    fireEvent.click(await screen.findByRole("option", { name: /安然/ }));
    fireEvent.click(screen.getByRole("button", { name: "添加" }));
    expect(await screen.findByText("这个知识库的成员条目太多；请改为添加群组或空间，而不是逐个添加人员。")).toBeTruthy();
  });

  it("says the directory is unavailable when it answers nothing, and still offers Spaces", async () => {
    vi.mocked(fetchTeamDirectory).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(fetchTeamGroups).mockResolvedValue({ items: [] });
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set()} onAdd={vi.fn()} />);
    expect(await screen.findByText(/暂时读不到团队通讯录，搜不到人和群组/)).toBeTruthy();
    await openResults();
    expect(await screen.findByRole("option", { name: /医学部/ })).toBeTruthy();
  });

  it("explains when the Space member list cannot be read, and still offers groups", async () => {
    vi.mocked(apiJson).mockRejectedValue(new ApiError(403, "no", { code: "not_workspace_member" }));
    renderIntl(<KbMemberPicker kb={SPACE_KB} excluded={new Set()} onAdd={vi.fn()} />);
    expect(await screen.findByText(/读不到这个空间的成员名单，暂时不能添加人/)).toBeTruthy();
    await openResults();
    expect(await screen.findByRole("option", { name: /销售组/ })).toBeTruthy();
    expect(screen.queryByRole("option", { name: /安然/ })).toBeNull();
  });

  it("says nothing matched for a typed name", async () => {
    vi.mocked(fetchTeamGroups).mockResolvedValue({ items: [] });
    renderIntl(<KbMemberPicker kb={TEAM_KB} excluded={new Set()} onAdd={vi.fn()} />);
    const input = await openResults();
    await screen.findByRole("option", { name: /安然/ });
    vi.mocked(fetchTeamDirectory).mockResolvedValue({ items: [], next_cursor: null });
    fireEvent.change(input, { target: { value: "不存在" } });
    expect(await screen.findByText("没有找到匹配的人、群组或空间，请换个名字试试。")).toBeTruthy();
  });

  it("reads further directory pages to fill a Space knowledge base's results", async () => {
    vi.mocked(fetchTeamDirectory)
      .mockResolvedValueOnce({ items: [PEOPLE[1]], next_cursor: 50 })
      .mockResolvedValueOnce({ items: [PEOPLE[0]], next_cursor: null });
    renderIntl(<KbMemberPicker kb={SPACE_KB} excluded={new Set()} onAdd={vi.fn()} />);
    await openResults();
    expect(await screen.findByRole("option", { name: /安然/ })).toBeTruthy();
    expect(fetchTeamDirectory).toHaveBeenCalledWith(undefined, 50);
  });

  it("reads the refusal reason only from member-invalid errors", () => {
    expect(memberInvalidReason(invalid("owner"))).toBe("owner");
    expect(memberInvalidReason(invalid("made_up"))).toBeNull();
    expect(memberInvalidReason(new ApiError(409, "x", { code: "knowledge_base_member_limit" }))).toBeNull();
    expect(memberInvalidReason(new Error("x"))).toBeNull();
  });
});
