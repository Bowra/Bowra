"use client";

/**
 * P4.5.12 · /forgot-password — submit an email address to ask the backend to send a reset email.
 *
 * The backend ALWAYS returns ok (never leaks whether the email exists), so this page only shows the uniform "check your email" result card.
 */

import {
 useRef, useState } from "react";

import {
  AuthShell, AuthField, AuthError,
} from "@/components/auth-shell";
import {
  BusyLabel,
  MailIcon,
  usePublicEmailDraft,
  useBrandedTranslations,
} from "@/components/public-shell";
import { AuthError as AuthApiError, forgotPassword } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { emailFieldError } from "@/lib/public-form";

export default function ForgotPage() {
  const t = useBrandedTranslations("auth");
  const [email, setEmail] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [errFocus, setErrFocus] = useState(0);
  const [fieldError, setFieldError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  usePublicEmailDraft((draft) => setEmail((current) => current || draft.toLowerCase()));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr(null);
    const invalid = emailFieldError(email, t);
    setFieldError(invalid);
    if (invalid) {
      emailRef.current?.focus();
      return;
    }
    setBusy(true);
    const normalized = email.trim().toLowerCase();
    try {
      await forgotPassword(normalized);
      setSentTo(normalized);
    } catch (ex) {
      setErr(
        ex instanceof AuthApiError
          ? ex.reasonCode === "rate_limited"
            ? t("error.forgot_rate_limited")
            : ex.reasonCode === "email_not_configured"
              ? t("error.forgot_email_not_configured")
            : ex.reasonCode === "email_delivery_unavailable"
              ? t("error.forgot_delivery_unavailable")
              : t("error.forgot_failed")
          : t("error.forgot_failed"),
      );
      setErrFocus((n) => n + 1);
    } finally {
      setBusy(false);
    }
  };

  if (sentTo) {
    // Never reveal whether the email exists ("if ... is registered"). One primary button (back to sign-in) plus "use a different email".
    return (
      <AuthShell
        announce
        status={{ tone: "success", icon: <MailIcon /> }}
        title={t("forgot.sent_title")}
        subtitle={t("forgot.sent_message", { email: sentTo })}
      >
        <div className="pub-actions">
          <a href={appPath("/login/")} className="pub-button pub-button--primary">
            {t("forgot.back_to_login")}
          </a>
          <button
            type="button"
            className="pub-button pub-button--ghost"
            onClick={() => {
              setSentTo(null);
              window.setTimeout(() => emailRef.current?.focus(), 0);
            }}
          >
            {t("forgot.try_another_email")}
          </button>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={t("forgot.title")}
      subtitle={t("forgot.subtitle")}
      footer={
        <a href={appPath("/login/")} className="pub-link">
          {t("forgot.back_to_login")}
        </a>
      }
    >
      <form onSubmit={submit} className="pub-form" noValidate>
        <AuthError message={err} focusKey={errFocus || undefined} />
        <div className="pub-fields">
          <AuthField label={t("field.email")} error={fieldError}>
            {(control) => (
              <input
                {...control}
                ref={emailRef}
                type="email" value={email} required autoComplete="email"
                autoCapitalize="off" spellCheck={false}
                readOnly={busy}
                onChange={(e) => {
                  setEmail(e.target.value.toLowerCase());
                  if (fieldError) setFieldError(undefined);
                }}
                onBlur={() => {
                  if (email.trim()) setFieldError(emailFieldError(email, t));
                }}
                className="pub-input"
                placeholder="you@company.com"
              />
            )}
          </AuthField>
        </div>
        <button
          type="submit"
          disabled={busy}
          aria-busy={busy || undefined}
          className="pub-button pub-button--primary"
        >
          <BusyLabel busy={busy} idle={t("forgot.submit")} pending={t("forgot.submitting")} />
        </button>
      </form>
    </AuthShell>
  );
}
