/**
 * Access control: group-member removal and grant revocation live in the row-end “⋯” menu and are confirmed first.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, expectConfirmGate } from "@/__tests__/row-actions";

const api = vi.hoisted(() => ({
  fetchAccessGroups: vi.fn(),
  fetchResourceGrants: vi.fn(),
  fetchGroupMembers: vi.fn(),
  removeGroupMember: vi.fn(),
  revokeResourceGrant: vi.fn(),
}));
vi.mock("@/lib/admin-access-api", () => ({
  ...api,
  addGroupMember: vi.fn(),
  createAccessGroup: vi.fn(),
  createResourceGrant: vi.fn(),
  explainPermission: vi.fn(),
}));
vi.mock("@/lib/auth/use-auth-redirect", () => ({ useAuthRedirect: () => ({ status: "authed" }) }));
vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));

import AdminAccessPage from "./page";

const T = "2026-09-20T00:00:00Z";
const GRANT = { id: "grt_1", team_id: "team", resource_kind: "kb_scope", resource_id: "scope_abcdef123456", principal_type: "group", principal_id: "grp_1", permission: "read", effect: "allow", created_by: null, created_at: T, updated_at: T, expires_at: null };
beforeEach(() => {
  api.fetchAccessGroups.mockResolvedValue({ items: [{ id: "grp_1", team_id: "team", workspace_id: null, name: "财务组", description: "", status: "active", member_count: 1, created_by: null, created_at: T, updated_at: T }] });
  api.fetchResourceGrants.mockResolvedValue({ items: [GRANT] });
  api.fetchGroupMembers.mockResolvedValue({ items: [{ group_id: "grp_1", user_id: "usr_1", role: "member", email: "li.na@example.com", display_name: null, created_at: T }] });
  api.removeGroupMember.mockResolvedValue(undefined);
  api.revokeResourceGrant.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("访问控制：移除组成员与撤销授权前确认", () => {
  it("both actions are danger items in the row menu and are confirmed first", async () => {
    renderIntl(<AdminAccessPage />);
    await chooseRowAction("li.na@example.com 的更多操作", "移除…", { danger: true });
    await expectConfirmGate({
      action: api.removeGroupMember,
      dialogName: "把 li.na@example.com 移出这个群组？",
      confirmName: "移除",
      reopen: () => chooseRowAction("li.na@example.com 的更多操作", "移除…"),
    });
    await waitFor(() => expect(api.removeGroupMember).toHaveBeenCalledTimes(1));
    await chooseRowAction(/^kb_scope .* 的更多操作$/, "撤销…", { danger: true });
    await expectConfirmGate({
      action: api.revokeResourceGrant,
      dialogName: "撤销这条授权？",
      confirmName: "撤销",
      reopen: () => chooseRowAction(/^kb_scope .* 的更多操作$/, "撤销…"),
    });
    await waitFor(() => expect(api.revokeResourceGrant).toHaveBeenCalledWith("grt_1"));
  });

  it("撤销「拒绝」授权：说明是解除禁止、对方可能重新获得访问，而不是失去权限", async () => {
    api.fetchResourceGrants.mockResolvedValue({ items: [{ ...GRANT, id: "grt_deny", effect: "deny" }] });
    renderIntl(<AdminAccessPage />);
    await openRevoke();
    const text = (await screen.findByRole("dialog", { name: "撤销这条授权？" })).textContent ?? "";
    expect(text).toContain("不再禁止");
    expect(text).toContain("重新获得访问");
    expect(text).not.toContain("失去");
    fireEvent.click(dialogButton("撤销"));
    await waitFor(() => expect(api.revokeResourceGrant).toHaveBeenCalledWith("grt_deny"));
  });

  it("撤销技能、报告上面向全团队的「允许」授权：写明改为只对单独授权的成员和群组开放、全团队授权一并撤销", async () => {
    api.fetchResourceGrants.mockResolvedValue({
      items: [{ ...GRANT, id: "grt_team", resource_kind: "team_skill", resource_id: "skl_1", principal_type: "team", principal_id: "team", permission: "use" }],
    });
    renderIntl(<AdminAccessPage />);
    await openRevoke();
    const text = (await screen.findByRole("dialog", { name: "撤销这条授权？" })).textContent ?? "";
    expect(text).toContain("面向全团队");
    expect(text).toContain("一并撤销");
  });

  it("面向全团队但不是技能、报告的授权（如知识库范围）按普通撤销说明", async () => {
    api.fetchResourceGrants.mockResolvedValue({
      items: [{ ...GRANT, id: "grt_kb_team", principal_type: "team", principal_id: "team" }],
    });
    renderIntl(<AdminAccessPage />);
    await openRevoke();
    const text = (await screen.findByRole("dialog", { name: "撤销这条授权？" })).textContent ?? "";
    expect(text).toContain("立即失去");
    expect(text).not.toContain("一并撤销");
  });

  it("成功后那一行消失，焦点回到所在分区标题", async () => {
    renderIntl(<AdminAccessPage />);
    await openRemove();
    api.fetchGroupMembers.mockResolvedValue({ items: [] });
    fireEvent.click(dialogButton("移除"));
    await waitFor(() => expect(screen.queryByText("li.na@example.com")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "成员" })));

    await openRevoke();
    api.fetchResourceGrants.mockResolvedValue({ items: [] });
    fireEvent.click(dialogButton("撤销"));
    await waitFor(() => expect(screen.queryByRole("button", { name: GRANT_MENU })).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("heading", { name: "资源授权" })));
  });

  it("撤销授权处理中：确认框按钮不可点，连点确认只发一次", async () => {
    let finish: () => void = () => {};
    api.revokeResourceGrant.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    renderIntl(<AdminAccessPage />);
    await openRevoke();
    await screen.findByRole("dialog");
    fireEvent.click(dialogButton("撤销"));
    await waitFor(() => expect(dialogButton("撤销").hasAttribute("disabled")).toBe(true));
    fireEvent.click(dialogButton("撤销"));
    expect(api.revokeResourceGrant).toHaveBeenCalledTimes(1);
    finish();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("处理中：确认框按钮不可点，连点确认只发一次", async () => {
    let finish: () => void = () => {};
    api.removeGroupMember.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    renderIntl(<AdminAccessPage />);
    await openRemove();
    await screen.findByRole("dialog");
    fireEvent.click(dialogButton("移除"));
    await waitFor(() => expect(dialogButton("移除").hasAttribute("disabled")).toBe(true));
    expect(dialogButton("取消").hasAttribute("disabled")).toBe(true);
    fireEvent.click(dialogButton("移除"));
    expect(api.removeGroupMember).toHaveBeenCalledTimes(1);
    finish();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

const GRANT_MENU = /^(?!li\.na@example\.com).* 的更多操作$/;

function openRemove(): Promise<void> {
  return chooseRowAction("li.na@example.com 的更多操作", "移除…");
}

function openRevoke(): Promise<void> {
  return chooseRowAction(GRANT_MENU, "撤销…");
}

function dialogButton(name: string): HTMLElement {
  const dialog = screen.getByRole("dialog");
  const button = Array.from(dialog.querySelectorAll("button")).find((b) => b.textContent?.trim() === name);
  if (!button) throw new Error(`no ${name} in dialog`);
  return button;
}
