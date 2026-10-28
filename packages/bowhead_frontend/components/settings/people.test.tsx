import { BUILD_EDITION } from "@/lib/product-identity";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import { SettingsPeople } from "./people";
import { fetchMe, type MeUser } from "@/lib/auth-api";
import {
  AdminMembersError,
  createCredentialRecovery,
  createLocalMember,
  fetchAdminMembers,
  fetchTeamInvitesPage,
  inviteTeamMember,
  deactivateTeamMember,
  fetchMemberWorkspaceGrants,
  resendTeamInvite,
  revokeMemberWorkspaceGrant,
  revokeTeamInvite,
  transferTeamOwnership,
  upsertMemberWorkspaceGrant,
  type AdminMembersListResp,
  type TeamInviteRow,
} from "@/lib/admin-members-api";
import { getAuthCapabilities } from "@/lib/invite-api";
import { fetchTeamSpaces } from "@/lib/admin-spaces-api";

vi.mock("@/lib/auth-api", () => ({ fetchMe: vi.fn() }));
vi.mock("@/lib/invite-api", () => ({ getAuthCapabilities: vi.fn() }));
vi.mock("@/lib/admin-members-api", () => ({
  AdminMembersError: class AdminMembersError extends Error {
    constructor(public status: number, public code: string, message: string) {
      super(message);
      this.name = "AdminMembersError";
    }
  },
  createLocalMember: vi.fn(),
  createCredentialRecovery: vi.fn(),
  fetchAdminMembers: vi.fn(),
  fetchTeamInvitesPage: vi.fn(),
  inviteTeamMember: vi.fn(),
  resendTeamInvite: vi.fn(),
  revokeTeamInvite: vi.fn(),
  changeTeamRole: vi.fn(),
  transferTeamOwnership: vi.fn(),
  deactivateTeamMember: vi.fn(),
  fetchMemberWorkspaceGrants: vi.fn(),
  upsertMemberWorkspaceGrant: vi.fn(),
  revokeMemberWorkspaceGrant: vi.fn(),
  revokeCredentialRecovery: vi.fn(),
}));
vi.mock("@/lib/admin-spaces-api", () => ({
  fetchTeamSpaces: vi.fn(),
}));

const baseMe: MeUser = {
  id: "u_admin",
  email: "admin@example.com",
  display_name: "Admin",
  avatar_url: null,
  team_id: "team_1",
  tier: "ultra",
  role: "user",
  team_role: "team_admin",
  status: "active",
  email_verified: true,
  credit_balance: 0,
  credit_quota: 0,
  locale: "zh-CN",
  timezone: "Asia/Shanghai",
  is_team_admin: true,
  workspace_count: 1,
  needs_onboarding: false,
};

const members: AdminMembersListResp = {
  owner_id: "u_owner",
  your_role: "team_admin",
  items: [
    {
      user_id: "u_owner",
      email: "owner@example.com",
      display_name: "Owner",
      tier: "ultra",
      is_owner: true,
      team_role: "owner",
      promoted_at: null,
      created_at: "2026-06-01T00:00:00Z",
      last_active_at: null,
    },
    {
      user_id: "u_admin",
      email: "admin@example.com",
      display_name: "Admin",
      tier: "ultra",
      is_owner: false,
      team_role: "team_admin",
      promoted_at: "2026-06-02T00:00:00Z",
      created_at: "2026-06-02T00:00:00Z",
      last_active_at: "2026-06-30T02:00:00Z",
    },
    {
      user_id: "u_member",
      email: "member@example.com",
      display_name: "Member",
      tier: "pro",
      is_owner: false,
      team_role: "member",
      promoted_at: null,
      created_at: "2026-06-03T00:00:00Z",
      last_active_at: null,
    },
  ],
};

const pendingInvite: TeamInviteRow = {
  id: "oinv_pending",
  team_id: "team_1",
  team_role: "member",
  invited_email: "pending@example.com",
  status: "active",
  invited_by: "u_admin",
  created_at: "2026-07-27T00:00:00Z",
  expires_at: "2026-08-03T00:00:00Z",
  consumed_at: null,
  revoked_at: null,
};

async function openLocalNameField(label: string) {
  fireEvent.click(await screen.findByRole("button", { name: label === "Name" ? "Add Member" : "添加成员" }));
  return screen.findByLabelText(label);
}

async function openInviteEmail() {
  fireEvent.click(await screen.findByRole("button", { name: /邀请成员|Invite people/ }));
  return screen.findByPlaceholderText("person@example.com");
}

function openMemberMoreMenu(memberName: string, label = `${memberName} 的更多操作`) {
  fireEvent.click(screen.getByRole("button", { name: label }));
}

describe("SettingsPeople", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    vi.resetAllMocks();
  });

  function arrangeCommon() {
    vi.mocked(fetchTeamInvitesPage).mockResolvedValue({ invites: [], total: 0, page: 1, size: 25 });
    vi.mocked(getAuthCapabilities).mockResolvedValue({
      deployment_mode: "local",
      account_creation_policy: "invite_only",
      email_verification_policy: "not_required",
      auth_methods: ["password"],
      public_registration: false,
      bootstrap_required: false,
      invite_activation: false,
    });
  }

  function arrangeInviteAvailable() {
    arrangeCommon();
    vi.mocked(getAuthCapabilities).mockResolvedValue({
      deployment_mode: "saas",
      account_creation_policy: "open",
      email_verification_policy: "not_required",
      auth_methods: ["password"],
      public_registration: true,
      bootstrap_required: false,
      invite_activation: true,
    });
  }

  it.each([
    ["  oWn  ", "owner@example.com"],
    ["ADMIN@EXAMPLE", "admin@example.com"],
  ])("queries members by nickname or email (%s) and restores the list when cleared", async (query, email) => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockImplementation(async (opts) => {
      const q = opts?.q?.toLowerCase() ?? "";
      const items = members.items.filter((item) =>
        item.email.toLowerCase().includes(q) || item.display_name?.toLowerCase().includes(q));
      return { ...members, items, total: items.length };
    });
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    const search = await screen.findByRole("searchbox", { name: "搜索成员" });
    fireEvent.change(search, { target: { value: query } });
    await waitFor(() => expect(fetchAdminMembers).toHaveBeenCalledWith(expect.objectContaining({ q: query.trim() })));
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2));
    expect(within(screen.getByRole("table")).getByText(email)).toBeTruthy();

    fireEvent.change(search, { target: { value: "no-match" } });
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("请调整搜索词或团队角色"));
    expect(screen.getByRole("tab", { name: "成员 · 匹配 0" })).toBeTruthy();

    fireEvent.change(search, { target: { value: "" } });
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(4));
  });

  it("requests one member page at a time and clears page selection when navigating", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockImplementation(async (opts) => ({
      ...members,
      items: opts?.page === 2 ? [members.items[0]] : members.items,
      total: 26,
    }));
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("checkbox", { name: "选择 Member" }));
    expect(screen.getByText("已选 1 人（仅当前页）")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "下一页" })).toHaveLength(2);
    fireEvent.click(screen.getAllByRole("button", { name: "下一页" })[0]);
    await waitFor(() => expect(fetchAdminMembers).toHaveBeenCalledWith(expect.objectContaining({ page: 2, size: 25 })));
    await waitFor(() => expect(screen.queryByText("已选 1 人（仅当前页）")).toBeNull());
  });

  it("uses a shorter mobile page and keeps an explicit page size in the URL", async () => {
    arrangeCommon();
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    vi.mocked(fetchAdminMembers).mockResolvedValue({ ...members, total: 26 });
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    await waitFor(() => expect(fetchAdminMembers).toHaveBeenCalledWith(expect.objectContaining({ size: 10 })));
    expect((screen.getByRole("combobox", { name: "每页显示" }) as HTMLSelectElement).value).toBe("10");
    expect(screen.getAllByRole("button", { name: "下一页" })).toHaveLength(2);
    fireEvent.change(screen.getByRole("combobox", { name: "每页显示" }), { target: { value: "25" } });
    await waitFor(() => expect(fetchAdminMembers).toHaveBeenCalledWith(expect.objectContaining({ size: 25 })));
    expect(new URL(window.location.href).searchParams.get("size")).toBe("25");
  });

  it("restores the selected page size and page numbers from a shared link", async () => {
    arrangeInviteAvailable();
    window.history.replaceState({}, "", "/?size=50&page=2&view=invites&invite_page=2");
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    vi.mocked(fetchAdminMembers).mockResolvedValue({ ...members, total: 80 });
    vi.mocked(fetchTeamInvitesPage).mockResolvedValue({ invites: [pendingInvite], total: 80, page: 2, size: 50 });
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    await waitFor(() => expect(fetchAdminMembers).toHaveBeenCalledWith(expect.objectContaining({ page: 2, size: 50 })));
    await waitFor(() => expect(fetchTeamInvitesPage).toHaveBeenCalledWith(expect.objectContaining({ page: 2, size: 50 })));
    expect((screen.getByRole("combobox", { name: "每页显示" }) as HTMLSelectElement).value).toBe("50");
    expect(screen.getAllByRole("navigation", { name: /列表.*分页/ })).toHaveLength(2);
  });

  it("shows labels for member tier and last activity", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    const row = (await screen.findByText("member@example.com")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("套餐")).toBeTruthy();
    expect(within(row!).getByText("最后活跃")).toBeTruthy();
  });

  it("clears selected members and hides old rows as soon as search changes", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("checkbox", { name: "选择 Member" }));
    expect(screen.getByRole("button", { name: "批量移出" })).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索成员" }), {
      target: { value: "different person" },
    });

    expect(screen.queryByRole("button", { name: "批量移出" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "选择 Member" })).toBeNull();
  });

  it("does not show a filtered count as the team total while clearing a role filter", async () => {
    arrangeCommon();
    let unfilteredRequests = 0;
    vi.mocked(fetchAdminMembers).mockImplementation(async (opts) => {
      if (opts?.role === "team_admin") return { ...members, items: [members.items[1]], total: 1 };
      unfilteredRequests += 1;
      if (unfilteredRequests === 1) return { ...members, total: 10 };
      return new Promise<AdminMembersListResp>(() => {});
    });
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    await screen.findByRole("tab", { name: "成员 10" });
    fireEvent.change(screen.getByRole("combobox", { name: "团队角色" }), {
      target: { value: "team_admin" },
    });
    await screen.findByRole("tab", { name: "成员 · 匹配 1" });
    fireEvent.change(screen.getByRole("combobox", { name: "团队角色" }), {
      target: { value: "" },
    });

    expect(screen.getByRole("tab", { name: "成员 · 查询中" })).toBeTruthy();
  });

  it("invites deduplicated email addresses in a SaaS batch and shows each result", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(inviteTeamMember).mockImplementation(async (email) => ({
      ok: true,
      method: "email_invite",
      email_sent: false,
      invite_url: `https://example.test/invite/${email}`,
    }));
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("button", { name: "邀请成员" }));
    fireEvent.click(screen.getByRole("radio", { name: "批量邀请" }));
    fireEvent.change(screen.getByRole("textbox", { name: "邮箱列表" }), {
      target: { value: "alice@t.com\nbob@t.com\nalice@t.com" },
    });
    expect(screen.getByText("已识别 2 个有效邮箱，已忽略 1 个重复邮箱")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发送批量邀请" }));
    await waitFor(() => expect(inviteTeamMember).toHaveBeenCalledTimes(2));
    expect(screen.getByText("处理结果（2）")).toBeTruthy();
    expect(screen.getByRole("button", { name: "本批已处理" })).toHaveProperty("disabled", true);
  });

  it("excludes invalid addresses from the batch invitation preview", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("button", { name: "邀请成员" }));
    fireEvent.click(screen.getByRole("radio", { name: "批量邀请" }));
    fireEvent.change(screen.getByRole("textbox", { name: "邮箱列表" }), {
      target: { value: "alice@t.com\nbob@t.com\nalice@t.com\nbroken" },
    });

    expect(screen.getByText("已识别 2 个有效邮箱，已忽略 1 个重复邮箱")).toBeTruthy();
    expect(screen.getByText("请修正无效邮箱：broken")).toBeTruthy();
    expect(screen.getByRole("button", { name: "发送批量邀请" })).toHaveProperty("disabled", true);
  });

  it("imports CSV addresses into the batch invitation draft for review", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("button", { name: "邀请成员" }));
    fireEvent.click(screen.getByRole("radio", { name: "批量邀请" }));
    const file = new File(["alice@t.com\nbob@t.com"], "members.csv", { type: "text/csv" });
    Object.defineProperty(file, "text", { value: vi.fn().mockResolvedValue("alice@t.com\nbob@t.com") });
    fireEvent.change(screen.getByLabelText("导入单列邮箱 CSV"), { target: { files: [file] } });

    await waitFor(() => expect((screen.getByRole("textbox", { name: "邮箱列表" }) as HTMLTextAreaElement).value)
      .toBe("alice@t.com\nbob@t.com"));
    expect(screen.getByText("已识别 2 个有效邮箱，已忽略 0 个重复邮箱")).toBeTruthy();
  });

  it("requires a count confirmation for bulk removal and links to credits only with permission", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      credit_capabilities: {
        credits_read: true,
        credit_grant: false,
        credit_user_quota_write: false,
        credit_user_tier_write: false,
        credit_period_reset: false,
        credit_team_quota_write: false,
        credit_attention_dismiss: false,
      },
    });
    vi.mocked(deactivateTeamMember).mockResolvedValue({ ok: true });
    renderIntl(<SettingsPeople />);

    await screen.findByText("Member");
    openMemberMoreMenu("Member");
    expect(screen.getByRole("menuitem", { name: "查看额度" }).getAttribute("href")).toContain("user_id=u_member");
    fireEvent.keyDown(screen.getByRole("menu", { name: "Member 的更多操作" }), { key: "Escape" });
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 Member" }));
    fireEvent.click(screen.getByRole("button", { name: "批量移出" }));
    const confirm = screen.getByRole("button", { name: "确认移出" });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByRole("textbox", { name: "输入 1 以确认移出人数" }), { target: { value: "1" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(deactivateTeamMember).toHaveBeenCalledWith("u_member"));
    expect(await screen.findByText("已移出 1 人，失败 0 人。")).toBeTruthy();
  });

  it("reports each failed member while continuing a bulk removal", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue({
      ...members,
      items: [...members.items, {
        ...members.items[2], user_id: "u_second", email: "second@example.com", display_name: "Second",
      }],
    });
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(deactivateTeamMember).mockImplementation(async (userId) => {
      if (userId === "u_second") throw new AdminMembersError(422, "last_workspace_admin", "blocked");
      return { ok: true };
    });
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("checkbox", { name: "选择 Member" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 Second" }));
    fireEvent.click(screen.getByRole("button", { name: "批量移出" }));
    fireEvent.change(screen.getByRole("textbox", { name: "输入 2 以确认移出人数" }), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "确认移出" }));

    await waitFor(() => expect(deactivateTeamMember).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("已移出 1 人，失败 1 人。")).toBeTruthy();
    expect(screen.getByText(/Second: /)).toBeTruthy();
  });

  it("lets team_admin manage members but hides team-role controls", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />);

    await screen.findByRole("tab", { name: /成员/ });
    expect(screen.getByRole("button", { name: "添加成员" })).toBeTruthy();
    expect(screen.getAllByText("管理员").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("修改角色").length).toBe(0);
    expect(screen.queryByRole("button", { name: "转让负责人" })).toBeNull();
    expect(screen.getAllByText("添加成员").length).toBeGreaterThan(0);
    expect(screen.queryByText("数据源绑定")).toBeNull();
    expect(screen.queryByText("打开数据源")).toBeNull();
  });

  it("shows ownership transfer only to the current owner and submits the selected member", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue({ ...members, your_role: "owner" });
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      id: "u_owner",
      email: "owner@example.com",
      display_name: "Owner",
      team_role: "owner",
    });
    vi.mocked(transferTeamOwnership).mockResolvedValue({ ok: true, owner_user_id: "u_member" });

    renderIntl(<SettingsPeople />);

    await screen.findByRole("tab", { name: /成员/ });
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "转让负责人…" }));
    expect(screen.getByText(/确认将团队负责人转让给 Member/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认转让" }));
    await waitFor(() => expect(transferTeamOwnership).toHaveBeenCalledWith("u_member"));
  });

  it("renders the local-user creation surface in the selected locale", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />, { locale: "en-US" });

    fireEvent.click(await screen.findByRole("button", { name: "Add Member" }));
    await screen.findByRole("heading", { name: "Add Member" });
    expect(screen.getByLabelText("Name")).toBeTruthy();
    expect(screen.getByLabelText("Email")).toBeTruthy();
    expect(screen.queryByLabelText("Initial password")).toBeNull();
    expect(
      screen.getByText(
        new Date("2026-06-30T02:00:00Z").toLocaleString("en-US", {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        }),
      ),
    ).toBeTruthy();
    expect(screen.queryByText("添加成员")).toBeNull();
  });

  it("shows the one-time password setup link for a local invitation", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(createLocalMember).mockResolvedValue({
      id: "u_new",
      email: "new@example.com",
      display_name: "New Member",
      method: "created",
      recovery_path: "/reset-password?flow=admin&token=setup-token",
      recovery_expires_at: "2026-06-30T02:15:00Z",
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderIntl(<SettingsPeople />, { locale: "en-US" });

    fireEvent.change(await openLocalNameField("Name"), {
      target: { value: "New Member" },
    });
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "NEW@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Member" }));

    await waitFor(() => expect(createLocalMember).toHaveBeenCalledWith({
      email: "new@example.com",
      display_name: "New Member",
    }));
    expect(await screen.findByRole("heading", { name: "Member Added" })).toBeTruthy();
    expect(screen.getByText("new@example.com")).toBeTruthy();
    expect(screen.getByText(/reset-password\?flow=admin&token=setup-token/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Copy username" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("new@example.com"));

    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      new URL("/reset-password?flow=admin&token=setup-token", window.location.origin).toString(),
    ));
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({ user_id: "u_new", items: [] });
    fireEvent.click(screen.getByRole("button", { name: "Configure Space Access" }));
    await waitFor(() => expect(fetchMemberWorkspaceGrants).toHaveBeenCalledWith("u_new"));
  });

  it.each([
    [
      "team_not_found",
      "zh-CN",
      "当前团队不可用，请刷新页面后重试；如问题持续，请联系系统管理员。",
    ],
    [
      "team_not_found",
      "en-US",
      "The current team is unavailable. Refresh the page and try again. If the problem continues, contact your system administrator.",
    ],
    [
      "admin_direct_password_disabled",
      "zh-CN",
      "当前部署已启用邀请激活，请发送邀请，由用户自行设置密码。",
    ],
    [
      "admin_direct_password_disabled",
      "en-US",
      "Invitation activation is enabled. Send an invitation so the user can set their own password.",
    ],
    [
      "local_only",
      "zh-CN",
      "直接创建成员仅适用于私有化部署，请改用邀请流程。",
    ],
    [
      "local_only",
      "en-US",
      "Direct member creation is only available in private deployments. Use the invitation flow instead.",
    ],
    [
      "member_create_conflict",
      "zh-CN",
      "成员创建状态发生冲突，请刷新成员列表后重试。",
    ],
    [
      "member_create_conflict",
      "en-US",
      "The member creation state changed. Refresh the member list and try again.",
    ],
    [
      "seat_exceeded",
      "zh-CN",
      "当前团队没有可用席位，请移除不再使用的成员，或联系团队负责人扩容。",
    ],
    [
      "seat_exceeded",
      "en-US",
      "This team has no available seats. Remove an inactive member or ask the team owner to add capacity.",
    ],
  ] as const)(
    "shows localized direct-create error %s in %s without the code",
    async (code, locale, message) => {
      arrangeCommon();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(createLocalMember).mockRejectedValue(
        new AdminMembersError(409, code, code),
      );

      renderIntl(<SettingsPeople />, { locale });

      const nameLabel = locale === "en-US" ? "Name" : "姓名";
      const emailLabel = locale === "en-US" ? "Email" : "邮箱";
      const submitLabel = locale === "en-US" ? "Add Member" : "添加成员";
      fireEvent.change(await openLocalNameField(nameLabel), {
        target: { value: "New Member" },
      });
      fireEvent.change(screen.getByLabelText(emailLabel), {
        target: { value: "new@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(code)).toBeNull();
    },
  );

  it("validates the create-user email in Chinese before calling the API", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />);

    fireEvent.change(await openLocalNameField("姓名"), {
      target: { value: "New Member" },
    });
    fireEvent.change(screen.getByLabelText("邮箱"), {
      target: { value: "invalid-email" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加成员" }));

    expect(
      await screen.findByText("请输入有效邮箱，例如 name@company.com。"),
    ).toBeTruthy();
    expect(createLocalMember).not.toHaveBeenCalled();
  });

  it.each([
    ["zh-CN", "姓名", "邮箱", "添加成员", "姓名或邮箱格式无效，请检查后重试。"],
    [
      "en-US",
      "Name",
      "Email",
      "Add Member",
      "The name or email is invalid. Review both fields and try again.",
    ],
  ] as const)(
    "localizes an unexpected backend field-validation response in %s",
    async (locale, nameLabel, emailLabel, submitLabel, message) => {
      arrangeCommon();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(createLocalMember).mockRejectedValue(
        new AdminMembersError(
          422,
          "error",
          "email: Value error, invalid email",
        ),
      );

      renderIntl(<SettingsPeople />, { locale });

      fireEvent.change(await openLocalNameField(nameLabel), {
        target: { value: "New Member" },
      });
      fireEvent.change(screen.getByLabelText(emailLabel), {
        target: { value: "new@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(/Value error/)).toBeNull();
    },
  );


  it("lets audit_admin view workspace grants as read-only", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue({
      ...members,
      your_role: "audit_admin",
    });
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      team_role: "audit_admin",
      is_team_admin: false,
    });
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({
      user_id: "u_member",
      items: [
        {
          workspace_id: "ws_people",
          name: "People Space",
          description: null,
          status: "active",
          role: null,
          joined_at: null,
        },
      ],
    });

    renderIntl(<SettingsPeople />);

    await screen.findByRole("searchbox", { name: "搜索成员" });
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "管理空间权限" }));

    fireEvent.click(await screen.findByRole("button", { name: "查看其他空间" }));
    await screen.findByText("People Space");
    expect(screen.getByText(/你可以查看空间授权/)).toBeTruthy();
    expect(screen.getByText("未授权")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "授权" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog", { name: "Member 的空间授权" })).toBeNull();
  });

  it("does not auto-grant a workspace when choosing a role for an empty grant", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({
      user_id: "u_member",
      items: [
        {
          workspace_id: "ws_people",
          name: "People Space",
          description: null,
          status: "active",
          role: null,
          joined_at: null,
        },
      ],
    });
    vi.mocked(upsertMemberWorkspaceGrant).mockResolvedValue({ ok: true });

    renderIntl(<SettingsPeople />);

    await screen.findAllByText("Member");
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "管理空间权限" }));

    fireEvent.click(await screen.findByRole("button", { name: "添加空间" }));
    await screen.findByText("People Space");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "admin" } });
    expect(upsertMemberWorkspaceGrant).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /授权/ }));
    await waitFor(() => expect(upsertMemberWorkspaceGrant).toHaveBeenCalledWith("u_member", "ws_people", "admin"));
    expect((await screen.findByRole("status")).textContent).toContain("已保存 People Space 的空间角色：管理员");
  });

  it("warns before closing a changed but unsaved space role", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({
      user_id: "u_member",
      items: [{
        workspace_id: "ws_people",
        name: "People Space",
        description: null,
        status: "active",
        role: "member",
        joined_at: "2026-06-03T00:00:00Z",
      }],
    });
    renderIntl(<SettingsPeople />);

    await screen.findAllByText("Member");
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "管理空间权限" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "People Space 的空间角色" }), {
      target: { value: "admin" },
    });
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(screen.getByRole("dialog", { name: "放弃角色修改" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByRole("dialog", { name: "Member 的空间授权" })).toBeTruthy();
    expect(upsertMemberWorkspaceGrant).not.toHaveBeenCalled();
  });

  it.each([
    {
      code: "last_workspace_admin",
      locale: "zh-CN",
      operation: "upsert",
      workspaceButton: "管理空间权限",
      actionLabel: "只读",
      expected: "空间授权更新失败：该成员是某个空间的唯一管理员，请先授予另一位成员管理员权限。",
    },
    {
      code: "last_workspace_admin",
      locale: "en-US",
      operation: "upsert",
      workspaceButton: "Manage Space Access",
      actionLabel: "Viewer",
      expected:
        "Space grant update failed: This member is the only admin in a space. Grant another member admin access first.",
    },
    {
      code: "workspace_not_found",
      locale: "zh-CN",
      operation: "upsert",
      workspaceButton: "管理空间权限",
      actionLabel: "只读",
      expected: "空间授权更新失败：找不到该空间，可能已被归档或不属于当前团队。",
    },
    {
      code: "workspace_not_found",
      locale: "en-US",
      operation: "upsert",
      workspaceButton: "Manage Space Access",
      actionLabel: "Viewer",
      expected: "Space grant update failed: Space not found. It may be archived or outside this team.",
    },
    {
      code: "workspace_team_mismatch",
      locale: "zh-CN",
      operation: "upsert",
      workspaceButton: "管理空间权限",
      actionLabel: "只读",
      expected: "空间授权更新失败：该成员或空间已不属于当前团队，请刷新成员和空间列表后重试。",
    },
    {
      code: "workspace_team_mismatch",
      locale: "en-US",
      operation: "upsert",
      workspaceButton: "Manage Space Access",
      actionLabel: "Viewer",
      expected:
        "Space grant update failed: This member or space no longer belongs to the current team. Refresh the member and space lists and try again.",
    },
    {
      code: "grant_not_found",
      locale: "zh-CN",
      operation: "revoke",
      workspaceButton: "管理空间权限",
      actionLabel: "移除",
      expected: "空间授权更新失败：该空间授权已不存在，请刷新成员的空间授权列表。",
    },
    {
      code: "grant_not_found",
      locale: "en-US",
      operation: "revoke",
      workspaceButton: "Manage Space Access",
      actionLabel: "Revoke",
      expected: "Space grant update failed: This space grant no longer exists. Refresh the member's space grants.",
    },
  ] as const)("maps $code to a localized $locale workspace-grant error", async ({
    code,
    locale,
    operation,
    workspaceButton,
    actionLabel,
    expected,
  }) => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({
      user_id: "u_member",
      items: [
        {
          workspace_id: "ws_people",
          name: "People Space",
          description: null,
          status: "active",
          role: "admin",
          joined_at: "2026-06-03T00:00:00Z",
        },
      ],
    });
    const actionError = new AdminMembersError(409, code, code);
    if (operation === "revoke") {
      vi.mocked(revokeMemberWorkspaceGrant).mockRejectedValue(actionError);
    } else {
      vi.mocked(upsertMemberWorkspaceGrant).mockRejectedValue(actionError);
    }

    renderIntl(<SettingsPeople />, { locale });

    await screen.findAllByText("Member");
    openMemberMoreMenu("Member", locale === "zh-CN" ? "Member 的更多操作" : "More actions for Member");
    fireEvent.click(screen.getByRole("menuitem", { name: workspaceButton }));

    await screen.findByText("People Space");
    if (operation === "revoke") {
      fireEvent.click(screen.getByRole("button", { name: actionLabel }));
      const confirmDialog = await screen.findByRole("dialog", { name: /People Space/ });
      fireEvent.click(within(confirmDialog).getByRole("button", { name: actionLabel }));
    } else {
      fireEvent.change(screen.getByRole("combobox"), { target: { value: "viewer" } });
      fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "保存角色" : "Save Role" }));
    }

    expect(await screen.findByText(expected)).toBeTruthy();
    expect(screen.queryByText(code)).toBeNull();
  });

  it.each([
    {
      locale: "zh-CN",
      workspaceButton: "管理空间权限",
      expected: "空间授权更新失败：空间或角色格式无效，请刷新空间授权列表后重试。",
    },
    {
      locale: "en-US",
      workspaceButton: "Manage Space Access",
      expected: "Space grant update failed: The space or role is invalid. Refresh the space grants and try again.",
    },
  ] as const)("localizes unexpected $locale workspace-grant validation details", async ({
    locale,
    workspaceButton,
    expected,
  }) => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchMemberWorkspaceGrants).mockResolvedValue({
      user_id: "u_member",
      items: [
        {
          workspace_id: "ws_people",
          name: "People Space",
          description: null,
          status: "active",
          role: "admin",
          joined_at: "2026-06-03T00:00:00Z",
        },
      ],
    });
    vi.mocked(upsertMemberWorkspaceGrant).mockRejectedValue(
      new AdminMembersError(422, "error", "role: Input should match pattern"),
    );

    renderIntl(<SettingsPeople />, { locale });

    await screen.findAllByText("Member");
    openMemberMoreMenu("Member", locale === "zh-CN" ? "Member 的更多操作" : "More actions for Member");
    fireEvent.click(screen.getByRole("menuitem", { name: workspaceButton }));

    await screen.findByText("People Space");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "viewer" } });
    fireEvent.click(screen.getByRole("button", { name: locale === "zh-CN" ? "保存角色" : "Save Role" }));

    expect(await screen.findByText(expected)).toBeTruthy();
    expect(screen.queryByText(/Input should match pattern/)).toBeNull();
  });

  it("explains shared-identity recovery failures in English without exposing the code", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(createCredentialRecovery).mockRejectedValue(
      new AdminMembersError(
        409,
        "shared_identity_recovery_requires_self_service",
        "shared_identity_recovery_requires_self_service",
      ),
    );

    renderIntl(<SettingsPeople />, { locale: "en-US" });

    await screen.findAllByText("Member");
    openMemberMoreMenu("Member", "More actions for Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "Generate Password Reset Link" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Generate password link" }),
    );

    expect(
      await screen.findByText(/recovery link cannot be created for this account/i),
    ).toBeTruthy();
    expect(
      screen.queryByText(/shared_identity_recovery_requires_self_service/),
    ).toBeNull();
  });

  it("explains owner recovery authorization failures in Chinese without exposing the code", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(createCredentialRecovery).mockRejectedValue(
      new AdminMembersError(
        403,
        "owner_recovery_requires_owner",
        "owner_recovery_requires_owner",
      ),
    );

    renderIntl(<SettingsPeople />);

    await screen.findByText("Owner");
    openMemberMoreMenu("Owner");
    fireEvent.click(screen.getByRole("menuitem", { name: "生成密码重置链接" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "生成设置密码链接" }),
    );

    expect(
      await screen.findByText(/只有团队负责人可以为负责人账号生成恢复链接/),
    ).toBeTruthy();
    expect(screen.queryByText(/owner_recovery_requires_owner/)).toBeNull();
  });

  it("keeps the list visible but disables editing when identity lookup fails", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockRejectedValue(new Error("identity down"));

    renderIntl(<SettingsPeople />);

    await screen.findByText(/暂时无法确认你的账号身份/);
    expect(screen.getByText("Admin")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "添加成员" })).toBeNull();
  });

  it("shows an error without rendering stale rows when the initial list load fails", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockRejectedValue(new Error("backend down"));
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />);

    await screen.findByText(/成员加载失败/);
    expect(screen.queryByText("Admin")).toBeNull();
  });

  it("keeps stale data visible when refresh fails after a successful load", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers)
      .mockResolvedValueOnce(members)
      .mockRejectedValueOnce(new Error("network down"));
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />);

    await screen.findByText("Admin");
    fireEvent.click(screen.getByText("刷新"));

    await waitFor(() => expect(screen.getByText(/当前显示的是上次成功加载的成员列表/)).toBeTruthy());
    expect(screen.getByText("Admin")).toBeTruthy();
    expect(screen.queryAllByText("修改角色").length).toBe(0);
  });

  it("sends an team-level invite without workspace context", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(inviteTeamMember).mockResolvedValue({
      ok: true,
      method: "email_invite",
      invite_id: "oinv_1",
      invite_url: "http://localhost/invite?token=abc",
      email_sent: false,
    });

    renderIntl(<SettingsPeople />);

    const input = await openInviteEmail();
    fireEvent.change(input, { target: { value: "new@example.com" } });
    expect(screen.getByText("邮箱：new@example.com")).toBeTruthy();
    expect(screen.getByText("仅加入团队，不分配空间")).toBeTruthy();
    fireEvent.click(screen.getByText("发送邀请"));

    await waitFor(() => expect(inviteTeamMember).toHaveBeenCalledWith("new@example.com"));
    expect(await screen.findByText(/团队邀请已创建/)).toBeTruthy();
    expect(screen.getByText("http://localhost/invite?token=abc")).toBeTruthy();
  });

  it("requires and submits an explicit initial-space assignment when Space Admin V2 is enabled", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue({
      ...baseMe,
      capabilities: { space_admin_v2: true },
    });
    vi.mocked(fetchTeamSpaces).mockResolvedValue([
      {
        workspace_id: "ws_sales",
        name: "Sales",
        description: null,
        status: "active",
        member_count: 3,
        admin_count: 1,
        caller_membership: null,
        created_at: "2026-07-01T00:00:00Z",
        updated_at: "2026-07-01T00:00:00Z",
        revision: 1,
      },
    ]);
    vi.mocked(inviteTeamMember).mockResolvedValue({
      ok: true,
      method: "created",
      invite_id: "oinv_1",
      invite_url: "http://localhost/invite?token=abc",
      email_sent: false,
      assignment_mode: "spaces",
      assignment_revision: 1,
      initial_workspace_grants: [
        { workspace_id: "ws_sales", role: "member" },
      ],
    });

    renderIntl(<SettingsPeople />);

    const input = await openInviteEmail();
    await screen.findByLabelText("Sales");
    const teamOnly = screen.getByRole("radio", { name: "仅加入团队，暂不分配空间" });
    const spaceSearch = screen.getByRole("searchbox", { name: "搜索空间" });
    expect(teamOnly.compareDocumentPosition(spaceSearch) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(input, { target: { value: "new@example.com" } });
    expect(
      (screen.getByRole("button", { name: "发送邀请" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByLabelText("Sales"));
    expect(screen.getByText("接受后加入 1 个空间 · 空间角色：成员")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "发送邀请" }));

    await waitFor(() =>
      expect(inviteTeamMember).toHaveBeenCalledWith("new@example.com", {
        assignment_mode: "spaces",
        initial_workspace_grants: [
          { workspace_id: "ws_sales", role: "member" },
        ],
      }),
    );
  });

  it("lets an inviter remove a selected space after filtering it out of the options", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue({ ...baseMe, capabilities: { space_admin_v2: true } });
    vi.mocked(fetchTeamSpaces).mockResolvedValue(["Sales", "Research"].map((name, index) => ({
      workspace_id: `ws_${index}`,
      name,
      description: null,
      status: "active",
      member_count: 3,
      admin_count: 1,
      caller_membership: null,
      created_at: "2026-07-01T00:00:00Z",
      updated_at: "2026-07-01T00:00:00Z",
      revision: 1,
    })));

    renderIntl(<SettingsPeople />);
    await openInviteEmail();
    fireEvent.click(await screen.findByLabelText("Sales"));
    const search = screen.getByRole("searchbox", { name: "搜索空间" });
    fireEvent.change(search, { target: { value: "Research" } });

    expect(screen.queryByLabelText("Sales")).toBeNull();
    expect(screen.getByText("列表显示 1 个空间")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "移除已选空间：Sales" }));
    expect(screen.getByText("已选 0 / 20 个空间")).toBeTruthy();
    expect(document.activeElement).toBe(search);

    fireEvent.change(search, { target: { value: "No match" } });
    expect(screen.getByRole("status").textContent).toContain("没有匹配的空间");

    fireEvent.click(screen.getByText("取消"));
    fireEvent.click(screen.getByRole("button", { name: "邀请成员" }));
    expect((screen.getByRole("searchbox", { name: "搜索空间" }) as HTMLInputElement).value).toBe("");
  });

  it.each([
    [
      "feature_disabled_use_direct_create",
      "zh-CN",
      "发送邀请",
      "邀请失败：当前部署未启用邀请激活，请直接创建成员，或联系系统管理员开启该功能。",
    ],
    [
      "feature_disabled_use_direct_create",
      "en-US",
      "Send invite",
      "Invite failed: Invitation activation is not enabled. Create the member directly, or ask your system administrator to enable it.",
    ],
    [
      "seat_full",
      "zh-CN",
      "发送邀请",
      BUILD_EDITION === "business" ? "邀请失败：当前团队没有可用席位，请先移除不再使用的成员或更新许可证，再发送邀请。" : "邀请失败：当前无法执行此操作。请检查团队权限，或联系管理员。",
    ],
    [
      "seat_full",
      "en-US",
      "Send invite",
      BUILD_EDITION === "business" ? "Invite failed: This team has no available seats. Remove an inactive member or update the license before inviting someone." : "Invite failed: This action is unavailable. Check your team permissions or contact an administrator.",
    ],
    [
      "user_not_found",
      "zh-CN",
      "发送邀请",
      "邀请失败：无法邀请该邮箱，请确认邮箱地址；如问题持续，请联系系统管理员。",
    ],
    [
      "user_not_found",
      "en-US",
      "Send invite",
      "Invite failed: This email cannot be invited. Check the address and contact your system administrator if the problem continues.",
    ],
  ] as const)(
    "shows localized team-invite error %s in %s without the code",
    async (code, locale, submitLabel, message) => {
      arrangeInviteAvailable();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(inviteTeamMember).mockRejectedValue(
        new AdminMembersError(409, code, code),
      );

      renderIntl(<SettingsPeople />, { locale });

      fireEvent.change(await openInviteEmail(), {
        target: { value: "new@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(code)).toBeNull();
    },
  );

  it.each([
    [
      "zh-CN",
      "发送邀请",
      "请输入有效邮箱，例如 name@company.com。",
    ],
    [
      "en-US",
      "Send invite",
      "Enter a valid email address, such as name@company.com.",
    ],
  ] as const)(
    "validates the team-invite email in %s before calling the API",
    async (locale, submitLabel, message) => {
      arrangeInviteAvailable();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);

      renderIntl(<SettingsPeople />, { locale });

      fireEvent.change(await openInviteEmail(), {
        target: { value: "invalid-email" },
      });
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(inviteTeamMember).not.toHaveBeenCalled();
    },
  );

  it.each([
    [
      "zh-CN",
      "发送邀请",
      "请输入有效邮箱，例如 name@company.com。",
    ],
    [
      "en-US",
      "Send invite",
      "Enter a valid email address, such as name@company.com.",
    ],
  ] as const)(
    "localizes an unexpected team-invite field-validation response in %s",
    async (locale, submitLabel, message) => {
      arrangeInviteAvailable();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(inviteTeamMember).mockRejectedValue(
        new AdminMembersError(
          422,
          "error",
          "email: Value error, invalid email",
        ),
      );

      renderIntl(<SettingsPeople />, { locale });

      fireEvent.change(await openInviteEmail(), {
        target: { value: "new@example.com" },
      });
      fireEvent.click(screen.getByRole("button", { name: submitLabel }));

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(/Value error/)).toBeNull();
    },
  );

  it.each([
    [
      "resend_cooldown_1min",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：重发太频繁，请 1 分钟后再试。",
    ],
    [
      "resend_cooldown_1min",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: Please wait 1 minute before resending this invite.",
    ],
    [
      "invite_not_found",
      "zh-CN",
      "revoke",
      "撤销",
      "邀请失败：找不到这条邀请，可能已被撤销或刷新。",
    ],
    [
      "invite_not_found",
      "en-US",
      "revoke",
      "Revoke",
      "Invite failed: Invite not found. It may have been revoked or refreshed.",
    ],
    [
      "invite_status_revoked",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：这条邀请已被撤销，请刷新待处理邀请列表后再操作。",
    ],
    [
      "invite_status_revoked",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: This invite was revoked. Refresh the pending-invite list before taking another action.",
    ],
    [
      "invite_status_consumed",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：这条邀请已被使用，请刷新待处理邀请列表。",
    ],
    [
      "invite_status_consumed",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: This invite has already been used. Refresh the pending-invite list.",
    ],
    [
      "invite_status_satisfied",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：对方已通过其他方式加入，请刷新成员和邀请列表。",
    ],
    [
      "invite_status_satisfied",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: This person has already joined through another path. Refresh the member and invite lists.",
    ],
    [
      "invite_status_expired",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：这条邀请已过期，请重新创建邀请。",
    ],
    [
      "invite_status_expired",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: This invite has expired. Create a new invitation.",
    ],
    [
      "invite_not_reissuable",
      "zh-CN",
      "resend",
      "重发",
      "邀请失败：邀请状态在重发前已变化，请刷新待处理邀请列表后重试。",
    ],
    [
      "invite_not_reissuable",
      "en-US",
      "resend",
      "Resend",
      "Invite failed: The invite changed before it could be resent. Refresh the pending-invite list and try again.",
    ],
  ] as const)(
    "shows localized invite lifecycle error %s in %s",
    async (code, locale, operation, actionLabel, message) => {
      arrangeCommon();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(fetchTeamInvitesPage).mockResolvedValue({ invites: [pendingInvite], total: 1, page: 1, size: 25 });
      const error = new AdminMembersError(409, code, code);
      if (operation === "revoke") {
        vi.mocked(revokeTeamInvite).mockRejectedValue(error);
      } else {
        vi.mocked(resendTeamInvite).mockRejectedValue(error);
      }

      renderIntl(<SettingsPeople />, { locale });

      fireEvent.click(await screen.findByRole("tab", { name: /待接受邀请|Pending Invites/ }));
      await screen.findByText("pending@example.com");
      await openRowMenu(
        locale === "zh-CN"
          ? "pending@example.com 的邀请的更多操作"
          : "More Actions for the Invitation to pending@example.com",
      );
      fireEvent.click(screen.getByRole("menuitem", { name: operation === "revoke" ? `${actionLabel}…` : actionLabel }));
      if (operation === "revoke") {
        const confirmDialog = await screen.findByRole("dialog", { name: /撤销这条邀请|Revoke this invite/ });
        fireEvent.click(within(confirmDialog).getByRole("button", { name: actionLabel }));
      }

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(code)).toBeNull();
    },
  );

  it.each([
    [
      "target_user_not_found",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：找不到该成员，可能已被移出团队。",
    ],
    [
      "target_user_not_found",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: Member not found. They may have been removed from the team.",
    ],
    [
      "cross_team_target_not_allowed",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：不能操作其他团队的成员。",
    ],
    [
      "cross_team_target_not_allowed",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: You cannot manage members from another team.",
    ],
    [
      "cannot_self_deactivate",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：不能从团队移除自己，请使用账号设置中的账号删除流程，或请另一位管理员处理。",
    ],
    [
      "cannot_self_deactivate",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: You cannot remove yourself from the team. Use the account-deletion flow in your account settings, or ask another administrator for help.",
    ],
    [
      "owner_immutable",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：负责人不能被移出团队，请先转让负责人。",
    ],
    [
      "owner_immutable",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: The Owner cannot be removed from the team. Transfer ownership first.",
    ],
    [
      "last_team_admin",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：该成员是团队最后一位管理员，请先将另一位成员设为管理员。",
    ],
    [
      "last_team_admin",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: This member is the team's last Administrator. Make another member an Administrator first.",
    ],
    [
      "last_workspace_admin",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：该成员是某个空间的唯一管理员，请先授予另一位成员管理员权限。",
    ],
    [
      "last_workspace_admin",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: This member is the only admin in a space. Grant another member admin access first.",
    ],
    [
      "already_inactive",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：该成员已被移出当前团队，请刷新成员列表。",
    ],
    [
      "already_inactive",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: This member has already been removed from the current team. Refresh the member list.",
    ],
    [
      "deactivate_failed",
      "zh-CN",
      "移出团队",
      "从团队移除",
      "移除失败：无法移除该成员，请刷新成员列表后重试。",
    ],
    [
      "deactivate_failed",
      "en-US",
      "Remove from Team",
      "Remove from team",
      "Remove failed: This member could not be removed. Refresh the member list and try again.",
    ],
  ] as const)(
    "shows localized member-deactivation error %s in %s",
    async (code, locale, removeLabel, confirmLabel, message) => {
      arrangeCommon();
      vi.mocked(fetchAdminMembers).mockResolvedValue(members);
      vi.mocked(fetchMe).mockResolvedValue(baseMe);
      vi.mocked(deactivateTeamMember).mockRejectedValue(
        new AdminMembersError(409, code, code),
      );

      renderIntl(<SettingsPeople />, { locale });

      await screen.findAllByText("Member");
      openMemberMoreMenu("Member", locale === "zh-CN" ? "Member 的更多操作" : "More actions for Member");
      fireEvent.click(screen.getByRole("menuitem", { name: `${removeLabel}…` }));
      fireEvent.click(
        await screen.findByRole("button", { name: confirmLabel }),
      );

      expect(await screen.findByText(message)).toBeTruthy();
      expect(screen.queryByText(new RegExp(code))).toBeNull();
    },
  );

  it("opens deactivate confirmation and calls deactivate API", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(deactivateTeamMember).mockResolvedValue({ ok: true });

    renderIntl(<SettingsPeople />);

    await screen.findByText("Member");
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "移出团队…" }));
    await screen.findByText("移除成员并停用账号");
    const confirmButtons = screen
      .getAllByText("从团队移除")
      .map((node) => node.closest("button") as HTMLButtonElement)
      .filter((button) => !button.disabled);
    fireEvent.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(deactivateTeamMember).toHaveBeenCalledWith("u_member"));
  });

  it("starts with the member list and keeps existing local invites accessible", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchTeamInvitesPage).mockResolvedValue({ invites: [pendingInvite], total: 1, page: 1, size: 25 });

    renderIntl(<SettingsPeople />);

    expect(await screen.findByRole("searchbox", { name: "搜索成员" })).toBeTruthy();
    expect(screen.queryByLabelText("姓名")).toBeNull();
    expect(screen.getByRole("button", { name: "添加成员" })).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "待接受邀请 1" }));
    expect(screen.getByText("pending@example.com")).toBeTruthy();
  });

  it("keeps local pending invites reachable when a search has no matches", async () => {
    arrangeCommon();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchTeamInvitesPage).mockImplementation(async (opts) => ({
      invites: opts?.q ? [] : [pendingInvite],
      total: opts?.q ? 0 : 1,
      page: 1,
      size: 25,
    }));
    renderIntl(<SettingsPeople />);

    fireEvent.click(await screen.findByRole("tab", { name: "待接受邀请 1" }));
    expect(screen.getByText("pending@example.com")).toBeTruthy();
    fireEvent.change(screen.getByRole("searchbox", { name: "搜索待处理邀请邮箱" }), {
      target: { value: "nobody@example.com" },
    });
    expect(screen.queryByText("pending@example.com")).toBeNull();

    expect(await screen.findByText("未找到匹配的待处理邀请，请调整邮箱搜索词。")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "待接受邀请 · 匹配 0", selected: true })).toBeTruthy();
  });

  it("limits a SaaS invite to 20 initial spaces before submission", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue({ ...baseMe, capabilities: { space_admin_v2: true } });
    vi.mocked(fetchTeamSpaces).mockResolvedValue(Array.from({ length: 21 }, (_, index) => ({
      workspace_id: `ws_${index + 1}`,
      name: `Space ${index + 1}`,
      description: null,
      status: "active",
      member_count: 1,
      admin_count: 1,
      caller_membership: null,
      created_at: "2026-07-01T00:00:00Z",
      updated_at: "2026-07-01T00:00:00Z",
      revision: 1,
    })));

    renderIntl(<SettingsPeople />);
    await openInviteEmail();
    await screen.findByLabelText("Space 21");
    for (let index = 1; index <= 20; index += 1) {
      fireEvent.click(screen.getByLabelText(`Space ${index}`));
    }
    expect((screen.getByLabelText("Space 21") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("已选 20 / 20 个空间")).toBeTruthy();
  });

  it("describes SaaS removal as a team change without account deactivation", async () => {
    arrangeInviteAvailable();
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);

    renderIntl(<SettingsPeople />);
    await screen.findByText("Member");
    openMemberMoreMenu("Member");
    fireEvent.click(screen.getByRole("menuitem", { name: "移出团队…" }));
    expect(await screen.findByText(/释放当前团队席位并移除该团队的空间权限/)).toBeTruthy();
    expect(screen.queryByText(/停用该账号及现有登录会话/)).toBeNull();
  });
});

describe("pending invite row actions menu", () => {
  afterEach(() => {
    cleanup();
    vi.resetAllMocks();
  });

  it("lists resend and revoke (danger); resend runs directly and revoke confirms first", async () => {
    vi.mocked(getAuthCapabilities).mockResolvedValue({
      deployment_mode: "local",
      account_creation_policy: "invite_only",
      email_verification_policy: "not_required",
      auth_methods: ["password"],
      public_registration: false,
      bootstrap_required: false,
      invite_activation: false,
    });
    vi.mocked(fetchAdminMembers).mockResolvedValue(members);
    vi.mocked(fetchMe).mockResolvedValue(baseMe);
    vi.mocked(fetchTeamInvitesPage).mockResolvedValue({ invites: [pendingInvite], total: 1, page: 1, size: 25 });
    vi.mocked(resendTeamInvite).mockResolvedValue(undefined as never);
    vi.mocked(revokeTeamInvite).mockResolvedValue(undefined as never);
    renderIntl(<SettingsPeople />);
    fireEvent.click(await screen.findByRole("tab", { name: /待接受邀请/ }));
    await screen.findByText("pending@example.com");
    expect(screen.queryByRole("button", { name: "重发" })).toBeNull();
    const MENU = "pending@example.com 的邀请的更多操作";
    expect(menuItemNames(await openRowMenu(MENU))).toEqual(["重发", "撤销…"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "重发" }));
    await waitFor(() => expect(resendTeamInvite).toHaveBeenCalled());
    await chooseRowAction(MENU, "撤销…", { danger: true });
    await expectConfirmGate({
      action: vi.mocked(revokeTeamInvite),
      dialogName: /撤销这条邀请/,
      confirmName: "撤销",
      reopen: () => chooseRowAction(MENU, "撤销…"),
    });
    await waitFor(() => expect(revokeTeamInvite).toHaveBeenCalled());
  });
});
