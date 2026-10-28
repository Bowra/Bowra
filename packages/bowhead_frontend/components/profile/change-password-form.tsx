"use client";

/**
 * Sprint β G10 — change password form.
 *
 * 5-rule pwd meter + old/new/confirm fields.
 *
 * P0-U-3 copy fix: success toast "Other N devices signed out · This device remains usable".
 * NOT "You will sign in again" — current device gets new token automatically.
 */

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";

import {
  SecurityError,
  changePassword,
  type ChangePasswordResp,
} from "@/lib/security-api";
import { CharIcon } from "@/components/char-icon";


function _scorePassword(p: string): { score: number; rules: string[] } {
  const rules: string[] = [];
  let score = 0;
  if (p.length >= 8) {
    score++;
    rules.push("len8");
  }
  if (/[A-Z]/.test(p) && /[a-z]/.test(p)) {
    score++;
    rules.push("mix-case");
  }
  if (/\d/.test(p)) {
    score++;
    rules.push("digit");
  }
  if (/[^A-Za-z0-9]/.test(p)) {
    score++;
    rules.push("symbol");
  }
  if (p.length >= 12) score++;
  return { score, rules };
}


interface Props {
  onSuccess: (result: ChangePasswordResp) => void;
  onCancel?: () => void;
  autoFocusCurrent?: boolean;
}


export function ChangePasswordForm({
  onSuccess,
  onCancel,
  autoFocusCurrent = false,
}: Props) {
  const t = useTranslations("profile.security");
  const authErrorT = useTranslations("auth.error");
  const fieldId = useId();
  const oldPasswordId = `${fieldId}-old`;
  const newPasswordId = `${fieldId}-new`;
  const confirmPasswordId = `${fieldId}-confirm`;
  const oldPasswordRef = useRef<HTMLInputElement>(null);
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<ShownError | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const { score, rules } = _scorePassword(newPwd);
  const mismatch = confirmPwd.length > 0 && confirmPwd !== newPwd;
  const canSubmit =
    oldPwd.length > 0 &&
    newPwd.length >= 8 &&
    rules.includes("digit") &&
    /[A-Za-z]/.test(newPwd) &&
    confirmPwd === newPwd &&
    !submitting;

  useEffect(() => {
    if (autoFocusCurrent) oldPasswordRef.current?.focus();
  }, [autoFocusCurrent]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    setSuccessMsg(null);
    try {
      const r = await changePassword({
        old_password: oldPwd,
        new_password: newPwd,
      });
      setSuccessMsg(
        t("change_success", { n: r.other_devices_kicked }),
      );
      setOldPwd("");
      setNewPwd("");
      setConfirmPwd("");
      onSuccess(r);
    } catch (e) {
      const msg = e instanceof SecurityError
        ? e.code === "oauth_user_no_password"
          ? t("err_oauth_no_password")
          : e.code === "wrong_old_password"
              ? t("err_wrong_old_password")
            : e.code === "concurrent_change_in_progress"
              ? t("err_concurrent_change")
              : e.code === "too_many_password_attempts"
                ? t("err_too_many_attempts")
                : e.code === "password_length_invalid"
                  ? authErrorT("password_length_invalid")
                  : e.code === "password_letter_required"
                    ? authErrorT("password_letter_required")
                    : e.code === "password_digit_required"
                      ? authErrorT("password_digit_required")
                      : e.code === "password_too_common"
                        ? authErrorT("password_too_common")
                : t("err_generic")
        : t("err_generic");
      // Do not append error/status codes to this sentence; with diagnostics enabled, put them in the Diagnostics collapsible area.
      setError(shownError(msg, e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="change-password-form" onSubmit={handleSubmit}>
      <div className="field">
        <label htmlFor={oldPasswordId}>{t("current_password")}</label>
        <input
          ref={oldPasswordRef}
          id={oldPasswordId}
          type="password"
          autoComplete="current-password"
          value={oldPwd}
          onChange={(e) => setOldPwd(e.target.value)}
          required
          maxLength={200}
        />
      </div>

      <div className="field">
        <label htmlFor={newPasswordId}>{t("new_password")}</label>
        <input
          id={newPasswordId}
          type="password"
          autoComplete="new-password"
          value={newPwd}
          onChange={(e) => setNewPwd(e.target.value)}
          required
          minLength={8}
          maxLength={200}
        />
        <div className="pwd-meter" data-score={score}>
          <div className="pwd-meter__bar" style={{ width: `${(score / 5) * 100}%` }} />
        </div>
        <ul className="pwd-rules">
          <li className={rules.includes("len8") ? "ok" : ""}>{t("rule_len8")}</li>
          <li className={rules.includes("mix-case") ? "ok" : ""}>{t("rule_mixcase")}</li>
          <li className={rules.includes("digit") ? "ok" : ""}>{t("rule_digit")}</li>
          <li className={rules.includes("symbol") ? "ok" : ""}>{t("rule_symbol")}</li>
        </ul>
      </div>

      <div className="field">
        <label htmlFor={confirmPasswordId}>{t("confirm_new_password")}</label>
        <input
          id={confirmPasswordId}
          type="password"
          autoComplete="new-password"
          value={confirmPwd}
          onChange={(e) => setConfirmPwd(e.target.value)}
          required
          maxLength={200}
        />
        {mismatch && (
          <div className="field__hint field__hint--error">{t("mismatch")}</div>
        )}
      </div>

      <div className="field__hint">
        {t("logout_note_prefix")}<strong>{t("logout_note_strong")}</strong>
        {" — "}
        {t("logout_note_suffix")}
      </div>

      {error && (
        <div className="error-banner" role="alert">
          <CharIcon ch="⚠" /> {error.message}
        </div>
      )}
      {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
      {successMsg && (
        <div className="success-banner" role="status">
          <CharIcon ch="✓" /> {successMsg}
        </div>
      )}

      <div className="change-password-form__actions">
        <button
          type="submit"
          className="btn btn--primary"
          disabled={!canSubmit}
        >
          {submitting ? t("submitting") : t("submit")}
        </button>
        {onCancel ? (
          <button
            type="button"
            className="btn btn--ghost"
            disabled={submitting}
            onClick={onCancel}
          >
            {t("cancel_change")}
          </button>
        ) : null}
      </div>
    </form>
  );
}
