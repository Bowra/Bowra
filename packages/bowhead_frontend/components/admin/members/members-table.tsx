"use client";

/**
 * Sprint β G5 — admin members table.
 *
 * Per v9.2 design F-2/F-17:
 * - Owner badge + role disabled; ownership is transferred separately.
 * - Team role is displayed as a role chip.
 * - Last active column (cached_name fallback for display_name).
 */

import { useLocale, useTranslations } from "next-intl";

import { Folder, Shield, Trash } from "@/components/icons";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import {
  type SessionMenuAction,
} from "@/components/sidebar/session-action-menu";
import type { AdminMemberRow, TeamRole } from "@/lib/admin-members-api";
import { appPath } from "@/lib/base-path";


const ROLE_LABEL_KEY: Record<TeamRole, string> = {
  owner: "role_owner",
  team_admin: "role_team_admin",
  audit_admin: "role_audit_admin",
  member: "role_member",
};


const ROLE_BADGE_CLASS: Record<TeamRole, string> = {
  owner: "team-role-badge team-role-badge--creator",
  team_admin: "team-role-badge team-role-badge--admin",
  audit_admin: "team-role-badge team-role-badge--audit",
  member: "team-role-badge team-role-badge--member",
};

/** Badge tones (spec: owner accent, admin neutral, member outline). */
const ROLE_BADGE_TONE: Record<TeamRole, BadgeTone> = {
  owner: "accent",
  team_admin: "neutral",
  audit_admin: "neutral",
  member: "outline",
};


function formatTime(iso: string | null, locale: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}


interface Props {
  items: AdminMemberRow[];
  canEditRoles: boolean;
  canTransferOwnership?: boolean;
  canViewWorkspaceGrants?: boolean;
  canDeactivateMembers?: boolean;
  canRecoverCredentials?: boolean;
  canViewCredits?: boolean;
  currentUserId?: string;
  selectedIds?: string[];
  onToggleSelected?: (id: string) => void;
  onSelectPage?: (ids: string[]) => void;
  onClickPromote: (m: AdminMemberRow) => void;
  onClickTransferOwnership?: (m: AdminMemberRow) => void;
  onClickWorkspaces?: (m: AdminMemberRow) => void;
  onClickDeactivate?: (m: AdminMemberRow) => void;
  onClickRecovery?: (m: AdminMemberRow) => void;
  extraActions?: (member: AdminMemberRow) => SessionMenuAction[];
}


export function MembersTable({
  items,
  canEditRoles,
  canTransferOwnership = false,
  canViewWorkspaceGrants = false,
  canDeactivateMembers = false,
  canRecoverCredentials = false,
  canViewCredits = false,
  currentUserId = "",
  selectedIds = [],
  onToggleSelected,
  onSelectPage,
  onClickPromote,
  onClickTransferOwnership,
  onClickWorkspaces,
  onClickDeactivate,
  onClickRecovery,
  extraActions,
}: Props) {
  const t = useTranslations("admin.members_table");
  const locale = useLocale();
  const selectableIds = items.filter((member) => !member.is_owner && member.user_id !== currentUserId)
    .map((member) => member.user_id);
  const allPageSelected = selectableIds.length > 0
    && selectableIds.every((id) => selectedIds.includes(id));
  if (items.length === 0) {
    return <div className="team-members-empty">{t("empty")}</div>;
  }

  return (
    <div className="team-members-table-wrap">
      {canDeactivateMembers && onSelectPage && selectableIds.length ? (
        <button
          type="button"
          className="team-members-mobile-select-page"
          aria-pressed={allPageSelected}
          onClick={() => onSelectPage(allPageSelected ? [] : selectableIds)}
        >
          {t(allPageSelected ? "clear_page" : "select_page")}
        </button>
      ) : null}
      <table className="team-members-table ui-table" aria-label={t("aria_list")}>
        <colgroup>
          <col />
          <col className="team-members-table__role-column" />
          <col className="team-members-table__tier-activity-column" />
          <col className="team-members-table__actions-column" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col"><div className="team-members-user-heading">
              {canDeactivateMembers && onSelectPage && selectableIds.length ? <input type="checkbox"
                aria-label={t("select_page")}
                checked={allPageSelected}
                onChange={(event) => onSelectPage(event.target.checked ? selectableIds : [])} /> : null}
              <span>{t("col_user")}</span>
            </div></th>
            <th scope="col">{t("col_role")}</th>
            <th scope="col">{t("col_tier_activity")}</th>
            <th scope="col" className="text-right">{t("col_actions")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((m) => {
            const label = m.display_name || m.email;
            const isOwner = m.is_owner;
            const isSelf = Boolean(currentUserId) && m.user_id === currentUserId;
            const roleLabel = t(ROLE_LABEL_KEY[m.team_role]);
            const moreActions: SessionMenuAction[] = [];
            if (canTransferOwnership && !isOwner && onClickTransferOwnership) {
              moreActions.push({
                id: "transfer-ownership",
                label: t("transfer_ownership"),
                icon: <Shield size={12} />,
                danger: true,
                onSelect: () => onClickTransferOwnership(m),
              });
            }
            if (canViewWorkspaceGrants && onClickWorkspaces) {
              moreActions.push({
                id: "workspace-grants",
                label: t("workspace_grants"),
                icon: <Folder size={12} />,
                onSelect: () => onClickWorkspaces(m),
              });
            }
            if (canDeactivateMembers && onClickDeactivate) {
              moreActions.push({
                id: "deactivate",
                label: t("deactivate"),
                icon: <Trash size={12} />,
                danger: true,
                disabled: isOwner || isSelf,
                title: isOwner
                  ? t("owner_deactivate_title")
                  : isSelf
                    ? t("self_deactivate_title")
                    : t("deactivate"),
                onSelect: () => onClickDeactivate(m),
              });
            }
            if (canRecoverCredentials && onClickRecovery) {
              moreActions.push({
                id: "credential-recovery",
                label: t("credential_recovery"),
                icon: <Shield size={12} />,
                disabled: isSelf,
                title: isSelf ? t("self_recovery_title") : t("credential_recovery"),
                onSelect: () => onClickRecovery(m),
              });
            }
            if (extraActions) moreActions.push(...extraActions(m));

            return (
              <tr key={m.user_id} className={isOwner ? "row--creator" : ""}>
                <td data-label={t("col_user")}>
                  <div className="member-cell">
                    {canDeactivateMembers && onToggleSelected ? <input type="checkbox"
                      aria-label={t("select_member", { name: label })}
                      checked={selectedIds.includes(m.user_id)}
                      disabled={isOwner || isSelf}
                      onChange={() => onToggleSelected(m.user_id)} /> : null}
                    <div className="member-cell__avatar" aria-hidden="true" data-self={isSelf || undefined}>
                      {label.slice(0, 1).toUpperCase()}
                    </div>
                    <div className="member-cell__body">
                      <div className="member-cell__name">
                        {label}
                      </div>
                      {m.display_name ? <div className="member-cell__email">{m.email}</div> : null}
                    </div>
                  </div>
                </td>
                <td data-label={t("col_role")}>
                  <Badge tone={ROLE_BADGE_TONE[m.team_role]} className={ROLE_BADGE_CLASS[m.team_role]}>
                    {roleLabel}
                  </Badge>
                </td>
                <td
                  data-label={t("col_tier_activity")}
                  className="team-members-table__tier-activity"
                  aria-label={`${t("col_tier")}: ${m.tier}; ${t("col_last_active")}: ${formatTime(m.last_active_at, locale)}`}
                >
                  <span className="team-members-meta-item">
                    <span className="team-members-meta-label">{t("col_tier")}</span>
                    <span>{m.tier}</span>
                  </span>
                  <span className="team-members-meta-item">
                    <span className="team-members-meta-label">{t("col_last_active")}</span>
                    <span>{formatTime(m.last_active_at, locale)}</span>
                  </span>
                </td>
                <td data-label={t("col_actions")} className="text-right">
                  <RowActionsMenu
                    label={t("more_actions_for", { name: label })}
                    actions={[
                      ...(canEditRoles
                        ? [{
                            id: "change-role",
                            label: t("change_role"),
                            disabled: isOwner,
                            disabledReason: isOwner ? t("owner_immutable_title") : undefined,
                            onSelect: () => onClickPromote(m),
                          }]
                        : []),
                      ...(canViewCredits
                        ? [{
                            id: "view-credit",
                            label: t("view_credit"),
                            href: appPath(`/admin/credits/users?user_id=${encodeURIComponent(m.user_id)}`),
                          }]
                        : []),
                      ...moreActions.map((action) => ({
                        id: action.id,
                        label: action.label,
                        danger: action.danger,
                        disabled: action.disabled,
                        disabledReason: action.disabled ? action.title : undefined,
                        onSelect: action.onSelect,
                      })),
                    ]}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
