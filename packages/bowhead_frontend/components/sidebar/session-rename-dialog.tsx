"use client";

import { useTranslations } from "next-intl";

import { PromptDialog } from "@/components/ui/prompt-dialog";
import { SESSION_TITLE_MAX_CHARS } from "@/lib/session-rename";

/**
 * The session rename dialog: the shared single-input `PromptDialog` (a centered card at every
 * width, per the menu contract) with session copy and the gateway's title limit.
 */
export function SessionRenameDialog({
  initialTitle,
  onCancel,
  onConfirm,
}: {
  initialTitle: string;
  onCancel: () => void;
  onConfirm: (title: string) => void;
}) {
  const t = useTranslations("session");
  return (
    <PromptDialog
      title={t("rename_dialog_title")}
      label={t("rename_field_label")}
      initialValue={initialTitle}
      confirmLabel={t("rename_save")}
      maxLength={SESSION_TITLE_MAX_CHARS}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}
