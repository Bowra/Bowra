"use client";

import {
 Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import {
  AuthError,
  AuthField,
  AuthShell,
  PasswordInput,
  PasswordRules,
  passwordMeetsRules,
} from "@/components/auth-shell";
import { useResendCooldown } from "@/components/auth-verify-notice";
import {
  BusyLabel,
  MailIcon,
  PublicBanner,
  usePublicEmailDraft,
  useBrandedTranslations,
} from "@/components/public-shell";
import { getAuthCapabilities, readInvitePreview } from "@/lib/invite-api";
import {
  AuthError as AuthApiError,
  registerUser,
  resendVerificationEmail,
  type VerificationMailOutcome,
} from "@/lib/auth-api";
import { emailFieldError } from "@/lib/public-form";
import { appPath } from "@/lib/base-path";
import { appDocumentHref, useAppRouter } from "@/lib/use-app-router";

export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <RegisterInner />
    </Suspense>
  );
}

function RegisterInner() {
  const t = useBrandedTranslations("auth");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [verifyEmail, setVerifyEmail] = useState<string | null>(null);
  const [verifyMail, setVerifyMail] = useState<VerificationMailOutcome | undefined>(undefined);
  const [verifyReceipt, setVerifyReceipt] = useState<string | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [allowed, setAllowed] = useState(false);
  const [errFocus, setErrFocus] = useState(0);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string; password2?: string }>({});
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const password2Ref = useRef<HTMLInputElement>(null);
  usePublicEmailDraft((draft) => setEmail((current) => current || draft.toLowerCase()));
  const inviteToken = searchParams.get("token") || searchParams.get("invite") || "";
  const inviteMode = searchParams.get("source") === "invite" || Boolean(inviteToken);

  // Invitation activation has a dedicated entry when enabled. The legacy flow
  // finishes account creation here; redirecting it back to /invite/ creates a loop
  // before the invitation can be consumed. Preserve the original intent in both paths.
  useEffect(() => {
    void getAuthCapabilities()
      .then((capabilities) => {
        if (inviteMode) {
          if (capabilities.invite_activation || !inviteToken) {
            router.replace(
              inviteToken
                ? `/invite/?token=${encodeURIComponent(inviteToken)}`
                : "/invite/?resume=1",
            );
            return;
          }
          // Invitation determines email, avoiding user error; invitation page already stored preview in sessionStorage.
          const preview = readInvitePreview();
          const invited = preview && "invited_email" in preview ? preview.invited_email : "";
          if (typeof invited === "string" && invited) setEmail(invited.toLowerCase());
          setAllowed(true);
          return;
        }
        if (capabilities.public_registration) {
          setAllowed(true);
        } else {
          router.replace("/login/");
        }
      })
      .catch(() => router.replace("/login/"));
  }, [inviteMode, inviteToken, router]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setErr(null);
    const errors = {
      email: emailFieldError(email, t),
      password: !password
        ? t("field_error.password_required")
        : passwordMeetsRules(password) ? undefined : t("field_error.password_rules"),
      password2: password2 && password2 !== password ? t("error.password_mismatch") : !password2 ? t("field_error.password_confirm_required") : undefined,
    };
    setFieldErrors(errors);
    const first = errors.email ? emailRef : errors.password ? passwordRef : errors.password2 ? password2Ref : null;
    if (first) {
      first.current?.focus();
      return;
    }
    setBusy(true);
    const normalizedEmail = email.trim().toLowerCase();
    try {
      const response = await registerUser({
        email: normalizedEmail,
        password,
        display_name: displayName.trim() || undefined,
        // Legacy invite registration must send token, or invite_only backend rejects
        // and user never joins invited team/space.
        invite_token: inviteToken || undefined,
      });
      if (response.requires_verification) {
        setVerifyMail(response.verification_mail);
        setVerifyReceipt(response.verification_receipt);
        setVerifyEmail(normalizedEmail);
        return;
      }
      window.location.replace(appDocumentHref("/"));
    } catch (error) {
      setErr(t(registerErrorKey(error)));
      setErrFocus((n) => n + 1);
    } finally {
      setBusy(false);
    }
  };

  if (!allowed) {
    return (
      <AuthShell
        title={t("register.opening_title")}
        subtitle={t("register.opening_subtitle")}
        status={{ tone: "neutral", icon: <span className="pub-spinner pub-spinner--lg" />, delayed: true }}
      />
    );
  }

  if (verifyEmail) {
    return (
      <RegisterVerifySent
        email={verifyEmail}
        initialOutcome={verifyMail}
        receipt={verifyReceipt}
        onChangeEmail={() => {
          setVerifyEmail(null);
          setVerifyMail(undefined);
          setVerifyReceipt(undefined);
          window.setTimeout(() => emailRef.current?.focus(), 0);
        }}
      />
    );
  }

  return (
    <AuthShell
      title={t("register.title")}
      subtitle={inviteMode ? t("register.subtitle_invite") : t("register.subtitle")}
      footer={(
        <span>
          {t("register.have_account")}{" "}
          <a href={appPath("/login/")} className="pub-link">
            {t("register.login_link")}
          </a>
        </span>
      )}
    >
      <form onSubmit={submit} className="pub-form" noValidate>
        <AuthError message={err} focusKey={errFocus || undefined} />
        <div className="pub-fields">
          <AuthField label={t("field.display_name")} aside={t("field.optional")}>
            {(control) => (
              <input
                {...control}
                type="text"
                value={displayName}
                autoComplete="name"
                readOnly={busy}
                onChange={(event) => setDisplayName(event.target.value)}
                className="pub-input"
                placeholder={t("register.display_name_placeholder")}
              />
            )}
          </AuthField>
          <AuthField label={t("field.email")} error={fieldErrors.email}>
            {(control) => (
              <input
                {...control}
                ref={emailRef}
                type="email"
                value={email}
                required
                autoComplete="email"
                autoCapitalize="off"
                spellCheck={false}
                readOnly={busy}
                onChange={(event) => {
                  setEmail(event.target.value.toLowerCase());
                  if (fieldErrors.email) setFieldErrors((prev) => ({ ...prev, email: undefined }));
                }}
                onBlur={() => {
                  if (email.trim()) setFieldErrors((prev) => ({ ...prev, email: emailFieldError(email, t) }));
                }}
                className="pub-input"
                placeholder="you@company.com"
              />
            )}
          </AuthField>
          <AuthField
            label={t("field.password")}
            rules={<PasswordRules value={password} />}
            error={fieldErrors.password}
          >
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={passwordRef}
                value={password}
                required
                readOnly={busy}
                autoComplete="new-password"
                minLength={8}
                onChange={(value) => {
                  setPassword(value);
                  if (fieldErrors.password && passwordMeetsRules(value)) {
                    setFieldErrors((prev) => ({ ...prev, password: undefined }));
                  }
                }}
                showLabel={t("field.show_password")}
                hideLabel={t("field.hide_password")}
              />
            )}
          </AuthField>
          <AuthField label={t("field.password_confirm")} error={fieldErrors.password2}>
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={password2Ref}
                value={password2}
                required
                readOnly={busy}
                autoComplete="new-password"
                minLength={8}
                placeholder={t("register.confirm_placeholder")}
                onChange={(value) => {
                  setPassword2(value);
                  if (fieldErrors.password2) setFieldErrors((prev) => ({ ...prev, password2: undefined }));
                }}
                onBlur={() => {
                  // Compare as soon as the confirmation field blurs, without waiting for submit
                  if (password2 && password2 !== password) {
                    setFieldErrors((prev) => ({ ...prev, password2: t("error.password_mismatch") }));
                  }
                }}
                showLabel={t("field.show_password")}
                hideLabel={t("field.hide_password")}
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
          <BusyLabel busy={busy} idle={t("register.submit")} pending={t("register.submitting")} />
        </button>
      </form>
    </AuthShell>
  );
}

/**
 * Verification step after registration: result card + resend (disabled for 60 seconds) +
 * "Wrong email? Go back and edit". When the server reports the mail was not queued, the card says
 * the account exists, explains what to check and shows the diagnostic reference instead of
 * claiming a mail was sent. A resend backed by the register receipt replaces that outcome. Once
 * the receipt has expired the server answers with a plain no-leak `{ok: true}`; that replaces an
 * earlier `not_sent` warning with a neutral notice that claims neither success nor failure.
 */
function RegisterVerifySent({
  email,
  initialOutcome,
  receipt,
  onChangeEmail,
}: {
  email: string;
  initialOutcome?: VerificationMailOutcome;
  receipt?: string;
  onChangeEmail: () => void;
}) {
  const t = useBrandedTranslations("auth");
  const [sending, setSending] = useState(false);
  const [sentCount, setSentCount] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  // "unknown": a resend after a not_sent outcome returned no outcome (receipt expired or missing).
  const [outcome, setOutcome] = useState<VerificationMailOutcome | "unknown" | undefined>(initialOutcome);
  const cooling = useResendCooldown(sentCount);
  const unknown = outcome === "unknown";
  const notSentOutcome = outcome !== "unknown" && outcome?.status === "not_sent" ? outcome : undefined;
  const notSent = notSentOutcome !== undefined;
  const resend = async () => {
    setErr(null);
    setSending(true);
    try {
      const result = await resendVerificationEmail(email, receipt);
      if (result.verification_mail) setOutcome(result.verification_mail);
      else setOutcome((current) => (current === "unknown" || current?.status === "not_sent" ? "unknown" : current));
      setSentCount((n) => n + 1);
    } catch (error) {
      setErr(
        error instanceof AuthApiError && error.reasonCode === "resend_too_frequent"
          ? t("verify.resend_too_frequent")
          : t("verify.resend_failed"),
      );
    } finally {
      setSending(false);
    }
  };
  const notSentMessage = notSentOutcome
    ? t(
        notSentOutcome.reason === "public_entry_required" ? "verify.not_sent_app_address" : "verify.not_sent_email_settings",
        { email, ref: notSentOutcome.reference ?? "-" },
      )
    : null;
  return (
    <AuthShell
      announce
      status={{ tone: "neutral", icon: <MailIcon /> }}
      title={notSent || unknown ? t("verify.account_ready_title") : t("verify.sent_title")}
      subtitle={notSent || unknown ? undefined : t("verify.pending_message", { email })}
      footer={(
        <span>
          {t("register.wrong_email")}{" "}
          <button type="button" className="pub-link-button" onClick={onChangeEmail}>
            {t("register.change_email")}
          </button>
        </span>
      )}
    >
      {notSentMessage ? (
        <PublicBanner tone="warning" focusKey={sentCount || undefined}>
          {notSentMessage}
        </PublicBanner>
      ) : null}
      {unknown ? (
        <PublicBanner tone="info" focusKey={sentCount || undefined}>
          {t("verify.resend_requested")}
        </PublicBanner>
      ) : null}
      {err ? <PublicBanner tone="error" focusKey={err}>{err}</PublicBanner> : null}
      <div className="pub-actions">
        <button
          type="button"
          className="pub-button"
          disabled={sending || cooling}
          aria-busy={sending || undefined}
          onClick={() => void resend()}
        >
          <BusyLabel
            busy={sending}
            idle={cooling && !unknown ? t("verify.resent") : t("verify.resend")}
            pending={t("verify.resending")}
          />
        </button>
      </div>
    </AuthShell>
  );
}

/** Map error codes to a sentence plus a next step; never show the raw backend message. */
function registerErrorKey(error: unknown) {
  if (!(error instanceof AuthApiError)) return "error.register_network" as const;
  switch (error.reasonCode) {
    case "password_length_invalid":
      return "error.password_length_invalid" as const;
    case "password_letter_required":
      return "error.password_letter_required" as const;
    case "password_digit_required":
      return "error.password_digit_required" as const;
    case "password_too_common":
      return "error.password_too_common" as const;
    case "rate_limited":
      return "error.register_rate_limited" as const;
    default:
      break;
  }
  if (error.status === 409) return "error.register_email_taken" as const;
  if (error.status === 429) return "error.register_rate_limited" as const;
  if (error.status === 403) return "error.register_disabled" as const;
  return "error.register_failed" as const;
}
