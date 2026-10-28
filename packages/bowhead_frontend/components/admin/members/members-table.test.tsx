import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import { MembersTable } from "./members-table";

const member = {
  user_id: "u_member",
  email: "member@example.com",
  display_name: "Member",
  tier: "pro",
  is_owner: false,
  team_role: "member" as const,
  promoted_at: null,
  created_at: "2026-06-03T00:00:00Z",
  last_active_at: "2026-06-30T02:00:00Z",
};

describe("MembersTable", () => {
  afterEach(cleanup);

  it("lays out the tier/activity column and reserves the action column", () => {
    renderIntl(<MembersTable items={[member]} canEditRoles onClickPromote={vi.fn()} />);

    expect(screen.getByRole("columnheader", { name: "套餐 / 最后活跃" })).toBeTruthy();
    expect(document.querySelector(".team-members-table__actions-column")).toBeTruthy();
    const tierActivity = screen.getByText("pro").closest("td");
    expect(tierActivity?.classList.contains("team-members-table__tier-activity")).toBe(true);
    expect(tierActivity?.children).toHaveLength(2);
  });

  it("reads the reason of a disabled More action to assistive tech", async () => {
    renderIntl(
      <MembersTable
        items={[{ ...member, is_owner: true }]}
        canEditRoles={false}
        onClickPromote={vi.fn()}
        canDeactivateMembers
        onClickDeactivate={vi.fn()}
      />,
    );

    const menu = await openRowMenu("Member 的更多操作");
    const item = within(menu).getByRole("menuitem", { name: "移出团队…" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    const describedBy = item.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toBe(
      "负责人不能移除，请先转让负责人",
    );
  });

  it("keeps the reserved action column for English labels", () => {
    renderIntl(
      <MembersTable
        items={[member]}
        canEditRoles
        canTransferOwnership
        onClickPromote={vi.fn()}
        onClickTransferOwnership={vi.fn()}
      />,
      { locale: "en-US" },
    );

    expect(screen.getByRole("columnheader", { name: "Tier / Last active" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "More actions for Member" })).toBeTruthy();
  });

  it("lets the mobile list select only removable members on this page", () => {
    const onSelectPage = vi.fn();
    renderIntl(
      <MembersTable
        items={[member, { ...member, user_id: "u_owner", is_owner: true, team_role: "owner" }]}
        canEditRoles
        canDeactivateMembers
        currentUserId="u_owner"
        selectedIds={[]}
        onSelectPage={onSelectPage}
        onClickPromote={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "选择本页可移出的成员" }));
    expect(onSelectPage).toHaveBeenCalledWith(["u_member"]);
  });

  it("clears the mobile page selection without submitting removal", () => {
    const onSelectPage = vi.fn();
    renderIntl(
      <MembersTable
        items={[member]}
        canEditRoles
        canDeactivateMembers
        selectedIds={["u_member"]}
        onSelectPage={onSelectPage}
        onClickPromote={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "取消选择本页" }));
    expect(onSelectPage).toHaveBeenCalledWith([]);
  });
});

describe("MembersTable row “⋯” menu", () => {
  afterEach(cleanup);

  const renderAll = (overrides: Partial<typeof member> = {}) => {
    const handlers = {
      onClickPromote: vi.fn(),
      onClickTransferOwnership: vi.fn(),
      onClickWorkspaces: vi.fn(),
      onClickDeactivate: vi.fn(),
      onClickRecovery: vi.fn(),
      onClickExtra: vi.fn(),
    };
    renderIntl(
      <MembersTable
        items={[{ ...member, ...overrides }]}
        canEditRoles
        canViewCredits
        canTransferOwnership
        canViewWorkspaceGrants
        canDeactivateMembers
        canRecoverCredentials
        extraActions={(member) => [{ id: "details", label: "Member details", icon: null, onSelect: () => handlers.onClickExtra(member) }]}
        {...handlers}
      />,
    );
    return handlers;
  };

  it("rows keep only content; role, credits and the other actions share one menu with danger items last", async () => {
    renderAll();
    expect(screen.queryByRole("button", { name: "修改 Member 的角色" })).toBeNull();
    expect(screen.queryByRole("link", { name: "查看额度" })).toBeNull();
    const menu = await openRowMenu("Member 的更多操作");
    expect(menuItemNames(menu)).toEqual([
      "修改角色", "查看额度", "管理空间权限", "生成密码重置链接", "Member details", "转让负责人…", "移出团队…",
    ]);
    expect(menu.querySelector(".ui-row-menu-sep")).toBeTruthy();
    expect(within(menu).getByRole("menuitem", { name: "查看额度" }).getAttribute("href")).toContain("/admin/credits/users?user_id=u_member");
  });

  it("each item calls its own handler", async () => {
    const handlers = renderAll();
    const cases: [string, keyof typeof handlers][] = [
      ["修改角色", "onClickPromote"],
      ["管理空间权限", "onClickWorkspaces"],
      ["生成密码重置链接", "onClickRecovery"],
      ["Member details", "onClickExtra"],
      ["转让负责人…", "onClickTransferOwnership"],
      ["移出团队…", "onClickDeactivate"],
    ];
    for (const [label, handler] of cases) {
      await chooseRowAction("Member 的更多操作", label);
      expect(handlers[handler], label).toHaveBeenCalledWith(expect.objectContaining({ user_id: "u_member" }));
      for (const [, other] of cases) if (other !== handler) expect(handlers[other]).not.toHaveBeenCalled();
      handlers[handler].mockClear();
    }
  });

  it("disables Change role for the owner and always shows the reason", async () => {
    const handlers = renderAll({ is_owner: true, team_role: "owner" as never });
    const menu = await openRowMenu("Member 的更多操作");
    const item = within(menu).getByRole("menuitem", { name: "修改角色" });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    expect(document.getElementById(item.getAttribute("aria-describedby") ?? "")?.textContent).toBe("负责人可通过转让操作变更");
    fireEvent.click(item);
    expect(handlers.onClickPromote).not.toHaveBeenCalled();
  });
});
