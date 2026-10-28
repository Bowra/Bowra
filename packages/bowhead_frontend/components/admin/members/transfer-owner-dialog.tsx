"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { AlertTriangle, Shield } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Notice } from "@/components/ui/notice";
import {
  AdminMembersError,
  type AdminMemberRow,
  transferTeamOwnership,
} from "@/lib/admin-members-api";

const ERROR_KEY: Record<string, string> = {
  owner_transfer_same_user: "err_same_user",
  owner_transfer_target_not_active: "err_target_not_active",
  owner_transfer_conflict: "err_conflict",
  readonly_team: "err_readonly",
  team_readonly: "err_readonly",
  insufficient_role: "err_owner_required",
};

export function TransferOwnerDialog({
  member,
  onClose,
  onSuccess,
}: {
  member: AdminMemberRow;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const t = useTranslations("admin.members_transfer_owner");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = member.display_name || member.email;

  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await transferTeamOwnership(member.user_id);
      onSuccess();
    } catch (cause) {
      setError(
        cause instanceof AdminMembersError
          ? t(ERROR_KEY[cause.code] ?? "err_generic")
          : t("err_generic"),
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      title={t("title")}
      onClose={onClose}
      busy={submitting}
      width={560}
      footer={(
        <>
          <Button onClick={onClose} disabled={submitting}>{t("cancel")}</Button>
          <Button variant="danger" onClick={() => void submit()} disabled={submitting}>
            {submitting ? t("submitting") : t("confirm")}
          </Button>
        </>
      )}
    >
      <Notice tone="error">
        <span className="promote-inline-icon"><AlertTriangle size={14} /></span>
        {t("warning")}
      </Notice>
      <div className="target-info">
        <div className="target-info__avatar" aria-hidden="true">
          {name.slice(0, 1).toUpperCase()}
        </div>
        <div className="target-info__body">
          <div className="target-info__name">{name}</div>
          <div className="target-info__email">{member.email}</div>
          <div className="target-info__role"><Shield size={14} /> {t("new_owner")}</div>
        </div>
      </div>
      <p>{t("body", { name })}</p>
      {error ? <Notice tone="error">{error}</Notice> : null}
    </Dialog>
  );
}
