/**
 * Sprint β G5 — PromoteModal unit tests.
 *
 * Covers:
 * - Owner row → grayed body, no submit button
 * - Self-demote-last-admin block banner + submit disabled
 * - Required reason for demote-to-member
 * - Wires changeTeamRole and onSuccess
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { PromoteModal } from "./promote-modal";
import * as api from "@/lib/admin-members-api";

const baseMember = {
  user_id: "u_target",
  email: "t@example.com",
  display_name: "T",
  tier: "pro",
  is_owner: false,
  team_role: "member" as const,
  promoted_at: null,
  created_at: "2026-05-01T00:00:00Z",
  last_active_at: null,
};

describe("PromoteModal", () => {
  beforeEach(() => {
    vi.spyOn(api, "changeTeamRole").mockResolvedValue({
      ok: true,
      method: "changed",
      old_role: "member",
      new_role: "team_admin",
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders info-only banner for owner (no submit)", () => {
    renderIntl(
      <PromoteModal
        member={{ ...baseMember, is_owner: true, team_role: "owner" }}
        currentRole="owner"
        isOwner
        isSelf={false}
        onlyRemainingTeamAdmin={false}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    );
    expect(screen.getByText(/负责人是团队的唯一最高权限角色/)).toBeTruthy();
    expect(screen.queryByText("确认修改角色")).toBeNull();
  });

  it("blocks self-demote of last team_admin", () => {
    renderIntl(
      <PromoteModal
        member={baseMember}
        currentRole="team_admin"
        isOwner={false}
        isSelf
        onlyRemainingTeamAdmin
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "成员" }));
    expect(screen.getByText(/唯一的管理员/)).toBeTruthy();
    expect(screen.getByText("确认修改角色").closest("button")).toHaveProperty("disabled", true);
  });

  it("blocks last team_admin from changing self to audit admin", () => {
    renderIntl(
      <PromoteModal
        member={baseMember}
        currentRole="team_admin"
        isOwner={false}
        isSelf
        onlyRemainingTeamAdmin
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "审计管理员" }));
    expect(screen.getByText(/唯一的管理员/)).toBeTruthy();
    expect(screen.getByText("确认修改角色").closest("button")).toHaveProperty("disabled", true);
  });

  it("requires ≥10 chars reason when demoting to member", () => {
    renderIntl(
      <PromoteModal
        member={baseMember}
        currentRole="team_admin"
        isOwner={false}
        isSelf={false}
        onlyRemainingTeamAdmin={false}
        onClose={() => {}}
        onSuccess={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "成员" }));
    const submit = screen.getByText("确认修改角色").closest("button")!;
    expect(submit).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText(/原因/), { target: { value: "short" } });
    expect(submit).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText(/原因/), {
      target: { value: "用户离职 — 收回 admin 权限" },
    });
    expect((submit as HTMLButtonElement).disabled).toBe(false);
  });

  it("calls changeTeamRole then onSuccess on submit", async () => {
    const onSuccess = vi.fn();
    renderIntl(
      <PromoteModal
        member={baseMember}
        currentRole="member"
        isOwner={false}
        isSelf={false}
        onlyRemainingTeamAdmin={false}
        onClose={() => {}}
        onSuccess={onSuccess}
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: "管理员" }));
    fireEvent.click(screen.getByText("确认修改角色"));
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(api.changeTeamRole).toHaveBeenCalledWith(
      "u_target",
      "team_admin",
      undefined,
    );
  });

  it.each([
    ["owner_immutable", 422, "负责人不能直接修改角色", "Transfer ownership to change the Owner"],
    ["last_team_admin_cannot_self_demote", 422, "当前最后一位管理员", "last Administrator"],
    ["cross_team_target_not_allowed", 403, "目标用户不在你的团队", "Target user is not in your team"],
    ["target_user_not_found", 404, "目标用户不存在", "Target user was not found"],
    ["insufficient_role", 403, "只有团队负责人可以修改团队角色", "Only the team owner can change team roles"],
    ["team_membership_required", 403, "你已不在当前团队中", "no longer a member of the current team"],
    ["user_not_active", 401, "当前账号已不可用", "Your account is unavailable"],
    ["user_inactive", 403, "当前账号已不可用", "Your account is unavailable"],
    ["auth_required", 401, "登录状态已失效", "sign-in session has expired"],
    ["invalid_token", 401, "登录状态已失效", "sign-in session has expired"],
    ["password_changed_relogin_required", 401, "登录状态已失效", "sign-in session has expired"],
    ["email_not_verified", 403, "请先完成邮箱验证", "Verify your email"],
    ["public_entry_team_mismatch", 421, "当前访问入口与团队不匹配", "entry does not match the current team"],
    ["over_grace", 402, "团队授权已过期", "team license has expired"],
    ["suspended", 403, "当前团队已被暂停", "team is suspended"],
    ["needs_license", 403, "尚未完成授权激活", "has not completed license activation"],
    ["tamper", 403, "部署授权验证失败", "License verification failed"],
    ["clock_tamper", 402, "服务器时间异常", "server clock is invalid"],
    ["entitlement_unavailable", 503, "暂时无法确认许可证状态", "license status cannot be verified"],
  ])(
    "maps role-change error %s to bilingual page messages without exposing the code",
    async (code, status, zhMessage, enMessage) => {
      for (const { locale, roleLabel, reasonLabel, submitLabel, expected } of [
        {
          locale: "zh-CN",
          roleLabel: "成员",
          reasonLabel: /原因/,
          submitLabel: "确认修改角色",
          expected: zhMessage,
        },
        {
          locale: "en-US",
          roleLabel: "Member",
          reasonLabel: /Reason/,
          submitLabel: "Confirm role change",
          expected: enMessage,
        },
      ] as const) {
        vi.mocked(api.changeTeamRole).mockRejectedValueOnce(
          new api.AdminMembersError(status, code, code),
        );
        renderIntl(
          <PromoteModal
            member={baseMember}
            currentRole="team_admin"
            isOwner={false}
            isSelf={false}
            onlyRemainingTeamAdmin={false}
            onClose={() => {}}
            onSuccess={() => {}}
          />,
          { locale },
        );
        fireEvent.click(screen.getByRole("radio", { name: roleLabel }));
        fireEvent.change(screen.getByLabelText(reasonLabel), {
          target: { value: "role change requested by owner" },
        });
        fireEvent.click(screen.getByText(submitLabel));
        await waitFor(() => expect(screen.getByText(new RegExp(expected, "i"))).toBeTruthy());
        expect(screen.queryByText(code)).toBeNull();
        cleanup();
      }
    },
  );

  it.each([
    ["zh-CN", "确认修改角色", "成员", /原因/, "角色或原因格式无效"],
    ["en-US", "Confirm role change", "Member", /Reason/, "The role or reason is invalid"],
  ] as const)(
    "localizes framework validation errors in %s",
    async (locale, submitLabel, roleLabel, reasonLabel, expected) => {
      vi.mocked(api.changeTeamRole).mockRejectedValueOnce(
        new api.AdminMembersError(422, "error", "new_role: Input should match pattern"),
      );
      renderIntl(
        <PromoteModal
          member={baseMember}
          currentRole="team_admin"
          isOwner={false}
          isSelf={false}
          onlyRemainingTeamAdmin={false}
          onClose={() => {}}
          onSuccess={() => {}}
        />,
        { locale },
      );
      fireEvent.click(screen.getByRole("radio", { name: roleLabel }));
      fireEvent.change(screen.getByLabelText(reasonLabel), {
        target: { value: "role change requested by owner" },
      });
      fireEvent.click(screen.getByText(submitLabel));
      await waitFor(() => expect(screen.getByText(new RegExp(expected, "i"))).toBeTruthy());
      expect(screen.queryByText(/Input should match pattern/)).toBeNull();
    },
  );

  it.each([
    ["zh-CN", "确认修改角色", "成员", /原因/, "角色修改失败"],
    ["en-US", "Confirm role change", "Member", /Reason/, "The role could not be changed"],
  ] as const)(
    "uses a localized safe fallback for unknown errors in %s",
    async (locale, submitLabel, roleLabel, reasonLabel, expected) => {
      vi.mocked(api.changeTeamRole).mockRejectedValueOnce(
        new api.AdminMembersError(500, "future_role_error", "internal constraint detail"),
      );
      renderIntl(
        <PromoteModal
          member={baseMember}
          currentRole="team_admin"
          isOwner={false}
          isSelf={false}
          onlyRemainingTeamAdmin={false}
          onClose={() => {}}
          onSuccess={() => {}}
        />,
        { locale },
      );
      fireEvent.click(screen.getByRole("radio", { name: roleLabel }));
      fireEvent.change(screen.getByLabelText(reasonLabel), {
        target: { value: "role change requested by owner" },
      });
      fireEvent.click(screen.getByText(submitLabel));
      await waitFor(() => expect(screen.getByText(new RegExp(expected, "i"))).toBeTruthy());
      expect(screen.queryByText(/future_role_error|internal constraint detail/)).toBeNull();
    },
  );
});
