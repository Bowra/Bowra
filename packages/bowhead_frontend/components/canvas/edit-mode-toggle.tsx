/**
 * EditModeToggle — report edit mode/status bar.
 *
 * P0/P1 contract: report defaults to read mode. Editing is only enabled when
 * the parent can provide a real save context.
 */

"use client";

import { useTranslations } from "next-intl";
import React from "react";

export function EditModeToggle({
  isEditMode,
  onToggle,
  onCancelEdit,
  hasUnsavedChanges = false,
  saving = false,
  error = null,
  disabled = false,
  disabledReason,
  onRetry,
}: {
  isEditMode: boolean;
  onToggle: () => void;
  onCancelEdit?: () => void;
  hasUnsavedChanges?: boolean;
  saving?: boolean;
  error?: string | null;
  disabled?: boolean;
  disabledReason?: string;
  onRetry?: () => void;
}) {
  const t = useTranslations("canvas");
  // Show status only when meaningful (saving / unsaved / saved / failure); plain viewing mode keeps only the button,
  // without the persistent full-width "查看模式 · 查看模式" bar.
  const status = error
    ? t("edit_save_failed")
    : saving
      ? t("edit_saving")
      : hasUnsavedChanges
        ? t("edit_unsaved")
        : isEditMode
          ? t("edit_saved")
          : null;
  return (
    // Move former inline styles to styles/report-canvas.css .legacy-edit-bar* (workspace and canvas batch 1)
    <div className="v2-report-edit-bar legacy-edit-bar">
      <span role="status" aria-live={error ? "off" : "polite"} aria-atomic="true" className={error ? "legacy-edit-bar-status is-error" : "legacy-edit-bar-status"}>
        {status}
      </span>
      {error && onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="report-edit-btn report-edit-btn--retry"
        >
          {t("edit_retry_save")}
        </button>
      ) : null}
      {isEditMode && onCancelEdit ? (
        <button
          type="button"
          disabled={disabled || saving}
          title={disabled ? disabledReason : undefined}
          onClick={() => {
            if (disabled || saving) return;
            onCancelEdit();
          }}
          className={[
            "v2-edit-toggle-btn report-edit-btn",
            disabled ? "report-edit-btn--unavailable" : null,
          ].filter(Boolean).join(" ")}
        >
          {t("edit_cancel")}
        </button>
      ) : null}
      <button
        type="button"
        disabled={disabled}
        title={disabled ? disabledReason : undefined}
        onClick={() => {
          if (disabled) return;
          // Batch B (P1-2): remove old AC8 confirm("退出会丢失…"); "完成" now exits only after
          // successful flush (including unblurred inline edits); failure stays in edit mode (report-view
          // handleEditToggle), so no content loss: confirmation was a false alarm. Discard via "退出编辑".
          onToggle();
        }}
        className={[
          "v2-edit-toggle-btn report-edit-btn",
          isEditMode ? "report-edit-btn--active" : disabled ? "report-edit-btn--unavailable" : null,
        ].filter(Boolean).join(" ")}
      >
        {isEditMode ? t("edit_done") : t("edit_this_report")}
      </button>
    </div>
  );
}
