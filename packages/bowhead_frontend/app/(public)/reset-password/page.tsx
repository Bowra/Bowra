"use client";

/**
 * P4.5.12 · /reset-password?flow=...&token=... — set a new password using a recovery token.
 *
 * flow and token are frozen from the first URL query. An invalid link shows only a result card and the next step (no unusable form);
 * on completion a result card + button, with no automatic redirect: self-service reset "Enter {app name}", admin recovery link "Go to sign-in".
 */

import {
 Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import {
  AuthShell,
  AuthField,
  AuthError,
  PasswordInput,
  PasswordRules,
  passwordMeetsRules,
} from "@/components/auth-shell";
import { useBrand } from "@/components/brand-provider";
import {
  BusyLabel,
  CircleCheckIcon,
  LinkOffIcon,
  useBrandedTranslations,
} from "@/components/public-shell";
import {
  AuthError as AuthApiError,
  completeCredentialRecovery,
  exchangeCredentialRecovery,
  resetPassword,
} from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";

type RecoveryFlow = "self_service" | "admin";

type RecoveryInitialization =
  | { status: "checking"; flow: RecoveryFlow; token: string }
  | { status: "invalid_link" }
  | { status: "ready_self_service"; token: string }
  | { status: "ready_admin" }
  | { status: "error" };

export default function ResetPage() {
  return (
    <Suspense fallback={null}>
      <ResetInner />
    </Suspense>
  );
}

function ResetInner() {
  const t = useBrandedTranslations("auth");
  const { brand } = useBrand();
  const searchParams = useSearchParams();
  const [recovery, setRecovery] = useState<RecoveryInitialization>(() => {
    const token = searchParams?.get("token") || "";
    const flow = searchParams?.get("flow");
    if (!token || (flow !== "self_service" && flow !== "admin")) {
      return { status: "invalid_link" };
    }
    return { status: "checking", flow, token };
  });
  const adminExchangeRef = useRef<ReturnType<
    typeof exchangeCredentialRecovery
  > | null>(null);
  const [pwd, setPwd] = useState("");
  const [pwd2, setPwd2] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ loginRequired: boolean } | null>(null);
  const [errFocus, setErrFocus] = useState(0);
  const [fieldErrors, setFieldErrors] = useState<{ pwd?: string; pwd2?: string }>({});
  const pwdRef = useRef<HTMLInputElement>(null);
  const pwd2Ref = useRef<HTMLInputElement>(null);

  const showRecoveryError = useCallback(
    (caught: unknown) => {
      const code =
        caught instanceof AuthApiError ? caught.reasonCode : undefined;
      setErrorCode(code ?? null);
      switch (code) {
        case "recovery_invalid_or_expired":
          setErr(t("error.recovery_invalid_or_expired"));
          return;
        case "recovery_intent_missing":
          setErr(t("error.recovery_intent_missing"));
          return;
        case "rate_limited":
        case "rate_limit":
          setErr(t("error.recovery_rate_limited"));
          return;
        case "feature_disabled":
          setErr(t("error.recovery_feature_disabled"));
          return;
        case "public_entry_team_mismatch":
          setErr(t("error.recovery_public_entry_mismatch"));
          return;
        case "password_length_invalid":
          setErr(t("error.password_length_invalid"));
          return;
        case "password_letter_required":
          setErr(t("error.password_letter_required"));
          return;
        case "password_digit_required":
          setErr(t("error.password_digit_required"));
          return;
        case "password_too_common":
          setErr(t("error.password_too_common"));
          return;
        default:
          setErr(t("error.reset_failed"));
      }
    },
    [t],
  );

  useEffect(() => {
    if (recovery.status === "invalid_link") {
      stripRecoveryTokenFromUrl();
      return;
    }
    if (recovery.status !== "checking") {
      return;
    }
    stripRecoveryTokenFromUrl();
    if (recovery.flow === "self_service") {
      setRecovery({ status: "ready_self_service", token: recovery.token });
      return;
    }

    let cancelled = false;
    const exchange =
      adminExchangeRef.current ?? exchangeCredentialRecovery(recovery.token);
    adminExchangeRef.current = exchange;
    void exchange
      .then(() => {
        if (!cancelled) {
          setRecovery({ status: "ready_admin" });
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          showRecoveryError(caught);
          setRecovery({ status: "error" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [recovery, showRecoveryError]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr(null);
    setErrorCode(null);
    if (
      recovery.status !== "ready_self_service" &&
      recovery.status !== "ready_admin"
    ) {
      return;
    }
    const errors = {
      pwd: !pwd
        ? t("field_error.password_required")
        : passwordMeetsRules(pwd) ? undefined : t("field_error.password_rules"),
      pwd2: !pwd2
        ? t("field_error.password_confirm_required")
        : pwd2 !== pwd ? t("error.password_mismatch") : undefined,
    };
    setFieldErrors(errors);
    if (errors.pwd || errors.pwd2) {
      (errors.pwd ? pwdRef : pwd2Ref).current?.focus();
      return;
    }
    setBusy(true);
    try {
      let loginRequired = false;
      if (recovery.status === "ready_self_service") {
        await resetPassword(recovery.token, pwd);
      } else {
        const result = await completeCredentialRecovery(pwd);
        loginRequired = result.login_required;
      }
      setDone({ loginRequired });
    } catch (ex) {
      showRecoveryError(ex);
      setErrFocus((n) => n + 1);
    } finally {
      setBusy(false);
    }
  };

  const linkUnusable =
    recovery.status === "invalid_link" ||
    errorCode === "recovery_invalid_or_expired" ||
    errorCode === "recovery_intent_missing";

  if (done) {
    // The done state does not auto-redirect (1.8 s is too short for screen readers): one button whose label matches its target.
    return (
      <AuthShell
        announce
        status={{ tone: "success", icon: <CircleCheckIcon /> }}
        title={t("reset.done_title")}
        subtitle={done.loginRequired ? t("reset.done_login_required") : t("reset.done_signed_in")}
      >
        <div className="pub-actions">
          {done.loginRequired ? (
            <a href={appPath("/login/")} className="pub-button pub-button--primary">
              {t("reset.go_to_login")}
            </a>
          ) : (
            <a href={appPath("/")} className="pub-button pub-button--primary">
              {t("reset.enter_app", { applicationName: brand.applicationName })}
            </a>
          )}
        </div>
      </AuthShell>
    );
  }

  if (linkUnusable) {
    // Invalid, expired or used link: show only the result card and the next step, not an unusable form.
    return (
      <AuthShell
        announce={recovery.status !== "invalid_link"}
        status={{ tone: "warning", icon: <LinkOffIcon /> }}
        title={t("reset.invalid_title")}
        subtitle={recovery.status === "invalid_link" || !err ? t("reset.invalid_body") : err}
      >
        <div className="pub-actions">
          <a href={appPath("/forgot-password/")} className="pub-button pub-button--primary">
            {t("reset.request_new_link")}
          </a>
        </div>
      </AuthShell>
    );
  }

  if (recovery.status === "checking" || recovery.status === "error") {
    return (
      <AuthShell
        status={recovery.status === "checking"
          ? { tone: "neutral", icon: <span className="pub-spinner pub-spinner--lg" />, delayed: true }
          : { tone: "warning", icon: <LinkOffIcon /> }}
        title={recovery.status === "checking" ? t("reset.checking_title") : t("reset.unavailable_title")}
        subtitle={recovery.status === "checking" ? undefined : err ?? t("error.reset_failed")}
        footer={
          <a href={appPath("/login/")} className="pub-link">
            {t("reset.back_to_login")}
          </a>
        }
      />
    );
  }

  return (
    <AuthShell
      title={t("reset.title")}
      footer={
        <a href={appPath("/login/")} className="pub-link">
          {t("reset.back_to_login")}
        </a>
      }
    >
      <form onSubmit={submit} className="pub-form" noValidate>
        <AuthError message={err} focusKey={errFocus || undefined} />
        <div className="pub-fields">
          <AuthField
            label={t("field.new_password")}
            rules={<PasswordRules value={pwd} />}
            error={fieldErrors.pwd}
          >
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={pwdRef}
                value={pwd}
                required
                readOnly={busy}
                autoComplete="new-password"
                minLength={8}
                onChange={(value) => {
                  setPwd(value);
                  if (fieldErrors.pwd && passwordMeetsRules(value)) {
                    setFieldErrors((prev) => ({ ...prev, pwd: undefined }));
                  }
                }}
                showLabel={t("field.show_password")}
                hideLabel={t("field.hide_password")}
              />
            )}
          </AuthField>
          <AuthField label={t("field.password_confirm")} error={fieldErrors.pwd2}>
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={pwd2Ref}
                value={pwd2}
                required
                readOnly={busy}
                autoComplete="new-password"
                minLength={8}
                onChange={(value) => {
                  setPwd2(value);
                  if (fieldErrors.pwd2) setFieldErrors((prev) => ({ ...prev, pwd2: undefined }));
                }}
                onBlur={() => {
                  if (pwd2 && pwd2 !== pwd) {
                    setFieldErrors((prev) => ({ ...prev, pwd2: t("error.password_mismatch") }));
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
          <BusyLabel busy={busy} idle={t("reset.submit")} pending={t("reset.submitting")} />
        </button>
      </form>
    </AuthShell>
  );
}

function stripRecoveryTokenFromUrl(): void {
  try {
    window.history.replaceState(null, "", appPath("/reset-password/"));
  } catch {
    /* The recovery input is already frozen in component state. */
  }
}
