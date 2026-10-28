"use client";

/**
 * Sprint β G5 — promote/demote role modal.
 *
 * From→to picker + reason (≥10 chars recommended, hard required for
 * critical actions like demoting team_admin).
 *
 * Blocks:
 * - Owner: render only as info (no submit, panel grayed).
 * - Self-demote-last-admin: shows block banner.
 *
 * Confirms after selecting a new role and satisfying required reason/guard checks.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle, Shield } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import {
  AdminMembersError,
  type AdminMemberRow,
  type TeamRole,
  changeTeamRole,
} from "@/lib/admin-members-api";


const ROLE_OPTIONS: { value: TeamRole; labelKey: string; descKey: string }[] = [
  { value: "team_admin", labelKey: "role_team_admin", descKey: "opt_team_admin_desc" },
  { value: "audit_admin", labelKey: "role_audit_admin", descKey: "opt_audit_admin_desc" },
  { value: "member", labelKey: "role_member", descKey: "opt_member_desc" },
];

const ROLE_LABEL_KEY: Record<TeamRole, string> = {
  owner: "role_owner",
  team_admin: "role_team_admin",
  audit_admin: "role_audit_admin",
  member: "role_member",
};

const ROLE_ERROR_KEY: Record<string, string> = {
  owner_immutable: "err_owner_immutable",
  last_team_admin_cannot_self_demote: "err_last_admin",
  last_team_admin_demote_blocked: "err_last_admin",
  cross_team_target_not_allowed: "err_cross_team",
  cross_team_blocked: "err_cross_team",
  target_user_not_found: "err_target_not_found",
  insufficient_role: "err_owner_required",
  team_membership_required: "err_team_membership_required",
  user_not_active: "err_account_unavailable",
  user_inactive: "err_account_unavailable",
  auth_required: "err_sign_in_again",
  invalid_token: "err_sign_in_again",
  password_changed_relogin_required: "err_sign_in_again",
  email_not_verified: "err_email_not_verified",
  public_entry_team_mismatch: "err_public_entry_mismatch",
  readonly_team: "err_readonly",
  team_readonly: "err_readonly",
  over_grace: "err_over_grace",
  suspended: "err_suspended",
  needs_license: "err_needs_license",
  tamper: "err_license_invalid",
  clock_tamper: "err_clock_invalid",
  entitlement_unavailable: "err_entitlement_unavailable",
};


interface Props {
  member: AdminMemberRow;
  currentRole: TeamRole;
  isOwner: boolean;
  isSelf: boolean;
  onlyRemainingTeamAdmin: boolean;
  onClose: () => void;
  onSuccess: () => void;
}


export function PromoteModal({
  member,
  currentRole,
  isOwner,
  isSelf,
  onlyRemainingTeamAdmin,
  onClose,
  onSuccess,
}: Props) {
  const t = useTranslations("admin.members_promote");
  const [newRole, setNewRole] = useState<TeamRole | null>(null);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isDemoteToMember = newRole === "member";
  const reasonRequired = isDemoteToMember || (isSelf && newRole !== currentRole);
  const reasonValid = !reasonRequired || reason.trim().length >= 10;

  const blockSelfDemoteLastAdmin =
    isSelf && onlyRemainingTeamAdmin && currentRole === "team_admin" && newRole && newRole !== "team_admin";

  const canSubmit =
    !isOwner &&
    newRole !== null &&
    newRole !== currentRole &&
    reasonValid &&
    !blockSelfDemoteLastAdmin &&
    !submitting;

  const handleSubmit = async () => {
    if (!canSubmit || !newRole) return;
    setSubmitting(true);
    setError(null);
    try {
      await changeTeamRole(member.user_id, newRole, reason.trim() || undefined);
      onSuccess();
    } catch (e) {
      const msg = e instanceof AdminMembersError
        ? e.status === 422 && e.code === "error"
          ? t("err_invalid_request")
          : t(ROLE_ERROR_KEY[e.code] ?? "err_generic")
        : t("err_generic");
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      title={isOwner ? t("title_owner") : t("title_change_role")}
      onClose={onClose}
      busy={submitting}
      width={560}
      footer={(
        <>
          <Button onClick={onClose} disabled={submitting}>
            {isOwner ? t("close") : t("cancel")}
          </Button>
          {!isOwner && (
            <Button
              variant="primary"
              onClick={handleSubmit}
              disabled={!canSubmit}
            >
              {submitting ? t("submitting") : t("confirm_change_role")}
            </Button>
          )}
        </>
      )}
    >
      <div className="target-info">
        <div className="target-info__avatar" aria-hidden="true">
          {(member.display_name || member.email).slice(0, 1).toUpperCase()}
        </div>
        <div className="target-info__body">
          <div className="target-info__name">
            {member.display_name || member.email}
          </div>
          <div className="target-info__email">{member.email}</div>
          <div className="target-info__role">
            {t("current_label")} <strong>{t(ROLE_LABEL_KEY[currentRole])}</strong>
            {isSelf && <span className="team-role-badge team-role-badge--self">{t("yourself")}</span>}
          </div>
        </div>
      </div>

      {isOwner && (
        <Notice tone="muted">
          <span className="promote-inline-icon"><Shield size={14} /></span>
          {t("owner_info")}
        </Notice>
      )}

      {!isOwner && (
        <>
          <fieldset className="role-picker-field">
            <legend>{t("pick_new_role")}</legend>
            <div className="role-picker">
              {ROLE_OPTIONS.map((opt) => (
                <label
                  key={opt.value}
                  className={`role-picker__item ${
                    newRole === opt.value ? "role-picker__item--active" : ""
                  } ${opt.value === currentRole ? "role-picker__item--current" : ""}`}
                >
                  <input
                    aria-label={t(opt.labelKey)}
                    type="radio"
                    name="newRole"
                    value={opt.value}
                    checked={newRole === opt.value}
                    onChange={() => setNewRole(opt.value)}
                    disabled={opt.value === currentRole}
                  />
                  <span className="role-picker__control" aria-hidden="true" />
                  <span className="role-picker__content">
                    <span className="role-picker__label">
                      <strong>{t(opt.labelKey)}</strong>
                      {opt.value === currentRole && (
                        <span className="team-role-badge team-role-badge--current">{t("current_badge")}</span>
                      )}
                    </span>
                    <span className="role-picker__desc">{t(opt.descKey)}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          {blockSelfDemoteLastAdmin && (
            <Notice tone="error">
              <span className="promote-inline-icon"><AlertTriangle size={14} /></span>
              {t("block_self_demote")}
            </Notice>
          )}

          <div className="promote-reason-field">
            <label htmlFor="prom-reason">
              {t("reason_label")}
              {reasonRequired ? (
                <span className="required">{t("reason_required")}</span>
              ) : (
                <span className="muted">{t("reason_optional")}</span>
              )}
            </label>
            <textarea
              id="prom-reason"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t("reason_placeholder")}
            />
            {reasonRequired && !reasonValid && reason.length > 0 && (
              <div className="promote-reason-field__hint promote-reason-field__hint--error">
                {t("reason_min_hint", { n: reason.trim().length })}
              </div>
            )}
          </div>

          {error && (
            <Notice tone="error">
              <span className="promote-inline-icon"><AlertTriangle size={14} /></span>
              {error}
            </Notice>
          )}
        </>
      )}
    </Dialog>
  );
}
