"use client";

/**
 * P4.5.12 · /login—Bowhead email/password + federated identity login.
 */

import { EditionLoginButton, editionLoginAvailable, editionLoginError } from "@/edition/login";

import type { FormEvent } from "react";
import {
 useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  AuthShell, AuthField, AuthError, PasswordInput,
} from "@/components/auth-shell";
import type { AuthErrorDiagnostics } from "@/components/auth-shell";
import { AuthVerifyNotice } from "@/components/auth-verify-notice";
import {
  BusyLabel,
  FeishuMark,
  usePublicEmailDraft,
  useBrandedTranslations,
} from "@/components/public-shell";
import {
  AuthError as AuthApiError,
  AuthTransportError,
  getBootstrapStatus,
  getLoginCapabilities,
  localActivationRequired,
  loginUser,
  oauthLoginRedirect,
} from "@/lib/auth-api";
import { appPath, unprefixAppPath } from "@/lib/base-path";
import { emailFieldError } from "@/lib/public-form";
import { getAuthCapabilities, type AuthCapabilities } from "@/lib/invite-api";
import {
  normalizeErrorDiagnostics,
} from "@/lib/error-presentation";
import type {
  ErrorDiagnostics,
} from "@/lib/error-presentation";
import { safeInternalPath } from "@/lib/safe-next-path";
import { isNetworkFailure } from "@/lib/transport-error";
import { appDocumentHref } from "@/lib/use-app-router";

// layout-redesign P5: land back where the user intended after login. Read
// `?next=` (preferred) or `?return=` from the URL; read window.location directly
// (not useSearchParams) so static-export doesn't need a Suspense boundary.
// Open-redirect guard: only internal absolute paths (start with a single "/").
function postLoginDest(): string {
  if (typeof window === "undefined") return "/";
  const sp = new URLSearchParams(window.location.search);
  const dest = sp.get("next") ?? sp.get("return");
  return unprefixAppPath(safeInternalPath(dest) ?? "/");
}

function isInviteFlow(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("flow") === "invite";
}

type OAuthProvider = "feishu";

// No-account footer copy is determined solely by /api/v1/auth/capabilities:
// - public: public registration allowed → show registration link;
// - invite: invitation-only and entered from invitation → registration link carries invite parameters (legacy registration landing);
// - contact_admin: invitation-only/admin-created accounts without invitation → no dead link; ask an admin for an invitation;
// - none: local deployment or unknown capabilities → hide.
// 116 (saas + invite_only) previously always showed “立即注册”; /register/ bounced back, causing an unexplained redirect loop.
type RegistrationFooter = "public" | "invite" | "contact_admin" | "none";

function registrationFooter(capabilities: AuthCapabilities, hasInviteContext: boolean): RegistrationFooter {
  if (capabilities.public_registration) return "public";
  if (capabilities.deployment_mode === "local") return "none";
  if (hasInviteContext && capabilities.account_creation_policy === "invite_only") return "invite";
  return "contact_admin";
}

export default function LoginPage() {
  const t = useBrandedTranslations("auth");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [errDiagnostics, setErrDiagnostics] = useState<AuthErrorDiagnostics | null>(null);
  const [publishedErrDiagnostics, setPublishedErrDiagnostics] =
    useState<ErrorDiagnostics | null>(null);
  const [verifyEmail, setVerifyEmail] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [registerHref, setRegisterHref] = useState("/register/");
  const [registration, setRegistration] = useState<RegistrationFooter>("none");
  const [oauthProviders, setOauthProviders] = useState<OAuthProvider[]>([]);
  const [oidcAvailable, setOidcAvailable] = useState(false);
  const [errTone, setErrTone] = useState<"error" | "info">("error");
  const [errFocus, setErrFocus] = useState(0);
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; password?: string }>({});
  const [ssoPending, setSsoPending] = useState<"feishu" | "oidc" | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  // Callback errors store only the error code and identity provider; the sentence is built at render time with the current t.
  // The first frame reads the server snapshot (product default brand) and the injected team brand only arrives after hydration, so the sentence must follow it; the URL parameters are already cleared on the first frame.
  const [callbackError, setCallbackError] = useState<{
    code: string;
    provider: OAuthProvider | null;
  } | null>(null);
  usePublicEmailDraft((draft) => setEmail((current) => current || draft.toLowerCase()));

  useEffect(() => {
    const oauthError = consumeOAuthCallbackError();
    if (oauthError) setCallbackError(oauthError);
    const dest = postLoginDest();
    const inviteHref = inviteRegisterHref(dest);
    setRegisterHref(inviteHref ?? "/register/");
    void getAuthCapabilities().then((capabilities) => {
      setRegistration(registrationFooter(capabilities, inviteHref !== null));
    }).catch(() => {
      setRegistration("none");
    });
    void getLoginCapabilities().then((capabilities) => {
      setOauthProviders(capabilities.oauth_providers);
      setOidcAvailable(editionLoginAvailable(capabilities));
    }).catch(() => {
      setOauthProviders([]);
      setOidcAvailable(false);
    });
    void getBootstrapStatus().then((status) => {
      if (localActivationRequired(status)) {
        window.location.replace(appPath("/activate/"));
      }
    }).catch(() => {});
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr(null);
    setCallbackError(null);
    setErrDiagnostics(null);
    setPublishedErrDiagnostics(null);
    setVerifyEmail(null);
    // Replaces native browser bubbles (unbranded, not localizable): validate required fields and email format on submit and show errors under the field.
    const emailError = emailFieldError(email, t);
    const passwordError = password ? undefined : t("field_error.password_required");
    if (emailError || passwordError) {
      setFieldErrors({ email: emailError, password: passwordError });
      (emailError ? emailRef : passwordRef).current?.focus();
      return;
    }
    setFieldErrors({});
    setBusy(true);
    const normEmail = email.trim().toLowerCase();
    try {
      await loginUser(normEmail, password);
      window.location.replace(appDocumentHref(postLoginDest()));
    } catch (ex) {
      // §C (real-auth): 403 email_not_verified → show the verify prompt + resend, not a generic error.
      if ((ex as { reasonCode?: string } | null)?.reasonCode === "email_not_verified") {
        setVerifyEmail(normEmail);
        return;
      }
      setErr(passwordLoginErrorMessage(t, ex));
      setErrTone(isPublicEntryNotice(ex) ? "info" : "error");
      setErrDiagnostics(authErrorDiagnostics(ex));
      setPublishedErrDiagnostics(publishedAuthErrorDiagnostics(ex));
      if (isCredentialError(ex)) {
        // Do not say whether the email or the password was wrong; keep the email, clear the password and focus it. The role="alert" banner is announced.
        setPassword("");
        window.setTimeout(() => passwordRef.current?.focus(), 0);
      } else {
        setErrFocus((n) => n + 1);
      }
    } finally {
      setBusy(false);
    }
  };

  const oauth = async (provider: OAuthProvider) => {
    setErr(null);
    setCallbackError(null);
    setErrDiagnostics(null);
    setPublishedErrDiagnostics(null);
    setSsoPending(provider);
    try {
      const inviteFlow = isInviteFlow();
      const { redirect_url } = inviteFlow
        ? await oauthLoginRedirect(provider, postLoginDest(), "invite")
        : await oauthLoginRedirect(provider, postLoginDest());
      window.location.href = redirect_url;
    } catch (ex) {
      const code = (ex as { reasonCode?: string } | null)?.reasonCode;
      const providerName = oauthProviderName(t, provider);
      setErr(code
        ? t(oauthErrorKey(code), { provider: providerName })
        : t("error.oauth_failed", { provider: providerName }));
      setErrDiagnostics(
        ex instanceof AuthApiError || ex instanceof AuthTransportError
          ? authErrorDiagnostics(ex)
          : null,
      );
      setPublishedErrDiagnostics(publishedAuthErrorDiagnostics(ex));
      setErrTone("error");
      setErrFocus((n) => n + 1);
      setSsoPending(null);
    }
  };

  const ssoAvailable = oauthProviders.includes("feishu") || oidcAvailable;

  return (
    <AuthShell
      title={t("login.title")}
      footer={
        registration === "public" || registration === "invite" ? (
          <span>
            {t("login.no_account")}{" "}
            <a href={appPath(registerHref)} className="pub-link">
              {t("login.register_link")}
            </a>
          </span>
        ) : registration === "contact_admin" ? (
          <span>{t("login.no_account_invite_only")}</span>
        ) : null
      }
    >
      {/* SSO above, email/password below; the buttons do not depend on brand or appearance injection and stay visible and clickable without it. */}
      {ssoAvailable ? (
        <div className="pub-sso">
          <EditionLoginButton available={oidcAvailable} pending={ssoPending === "oidc"} disabled={ssoPending !== null} next={postLoginDest()} onPending={() => setSsoPending("oidc")} onError={(error) => { setSsoPending(null); setErr(passwordLoginErrorMessage(t, error)); }} />
          {oauthProviders.includes("feishu") ? (
            <button
              type="button"
              className="pub-button"
              disabled={ssoPending !== null}
              aria-busy={ssoPending === "feishu" || undefined}
              onClick={() => oauth("feishu")}
            >
              {ssoPending === "feishu" ? null : <FeishuMark />}
              <BusyLabel busy={ssoPending === "feishu"} idle={t("login.oauth_feishu")} pending={t("login.sso_redirecting")} />
            </button>
          ) : null}
        </div>
      ) : null}
      {ssoAvailable ? <div className="pub-divider">{t("login.divider_or")}</div> : null}
      <form onSubmit={submit} className="pub-form" noValidate>
        <AuthError
          message={err ?? (callbackError ? callbackErrorMessage(t, callbackError) : null)}
          tone={errTone}
          focusKey={errFocus || undefined}
          diagnostics={errDiagnostics}
          publishedDiagnostics={publishedErrDiagnostics}
          surface="login"
          diagnosticLabels={{
            summary: t("diagnostics.summary"),
            status: t("diagnostics.status"),
            code: t("diagnostics.code"),
            technicalMessage: t("diagnostics.technical_message"),
            requestId: t("diagnostics.request_id"),
          }}
        />
        {/* The verify notice renders ABOVE the still-visible form on purpose (unlike register, which
            swaps the form out): a failed login may be the wrong account, so we keep the form for retry. */}
        {verifyEmail ? <AuthVerifyNotice email={verifyEmail} /> : null}
        <div className="pub-fields">
          <AuthField label={t("field.email")} error={fieldErrors.email}>
            {(control) => (
              <input
                {...control}
                ref={emailRef}
                type="email" value={email} required autoComplete="email"
                autoCapitalize="off" spellCheck={false} enterKeyHint="next"
                readOnly={busy}
                onChange={(e) => {
                  setEmail(e.target.value.toLowerCase());
                  if (fieldErrors.email) setFieldErrors((prev) => ({ ...prev, email: undefined }));
                }}
                onBlur={() => {
                  // On blur only validate the format; empty values are reported on submit so tabbing through does not raise errors.
                  if (email.trim()) setFieldErrors((prev) => ({ ...prev, email: emailFieldError(email, t) }));
                }}
                onKeyDown={(e) => {
                  // Return in the email field moves to the password field (ignored during IME composition); submits normally when the password is filled
                  if (e.key === "Enter" && !e.nativeEvent.isComposing && !password) {
                    e.preventDefault();
                    passwordRef.current?.focus();
                  }
                }}
                className="pub-input"
                placeholder="you@company.com"
              />
            )}
          </AuthField>
          <AuthField
            label={t("field.password")}
            error={fieldErrors.password}
            aside={(
              <a href={appPath("/forgot-password/")} className="pub-link">
                {t("login.forgot_link")}
              </a>
            )}
          >
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={passwordRef}
                readOnly={busy}
                value={password} required autoComplete="current-password"
                onChange={(value) => {
                  setPassword(value);
                  if (fieldErrors.password) setFieldErrors((prev) => ({ ...prev, password: undefined }));
                }}
                showLabel={t("field.show_password")} hideLabel={t("field.hide_password")}
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
          <BusyLabel busy={busy} idle={t("login.submit")} pending={t("login.submitting")} />
        </button>
      </form>
    </AuthShell>
  );
}

function callbackErrorMessage(
  t: ReturnType<typeof useTranslations<"auth">>,
  error: { code: string; provider: OAuthProvider | null },
): string {
  const editionError = editionLoginError(error.code);
  if (editionError) return t(editionError);
  return t(oauthErrorKey(error.code), { provider: oauthProviderName(t, error.provider) });
}

function isCredentialError(error: unknown): boolean {
  return error instanceof AuthApiError
    && (error.reasonCode === "invalid_credentials" || error.status === 401);
}

/** Legacy gateway 421 "entry not registered": unregistered entries now sign in normally, so show a neutral notice and never say "blocked". */
function isPublicEntryNotice(error: unknown): boolean {
  return error instanceof AuthApiError && error.reasonCode === "public_entry_not_registered";
}

function passwordLoginErrorMessage(
  t: ReturnType<typeof useTranslations<"auth">>,
  error: unknown,
): string {
  const reasonCode = (
    error as { reasonCode?: string } | null
  )?.reasonCode;
  if (reasonCode === "request_timed_out") {
    return t("error.login_request_timed_out");
  }
  if (reasonCode === "service_unreachable") {
    return t("error.login_network_error");
  }
  if (!(error instanceof AuthApiError)) {
    return t(isNetworkFailure(error) ? "error.login_network_error" : "error.login_failed");
  }
  if (error.reasonCode === "public_entry_not_registered") {
    return t("error.public_entry_not_registered");
  }
  if (error.reasonCode === "invalid_credentials" || error.status === 401) {
    return t("error.invalid_credentials");
  }
  if (error.reasonCode === "rate_limited" || error.status === 429) {
    return t("error.login_rate_limited");
  }
  if (error.reasonCode === "account_unavailable" || error.status === 403) {
    return t("error.account_unavailable");
  }
  if (error.status >= 500) {
    return t("error.login_service_unavailable");
  }
  return t("error.login_failed");
}

function authErrorDiagnostics(error: unknown): AuthErrorDiagnostics {
  if (error instanceof AuthApiError) {
    return {
      status: error.status,
      code: error.reasonCode,
      technicalMessage: error.technicalMessage,
      requestId: error.requestId,
    };
  }
  if (error instanceof AuthTransportError) {
    return {
      code: error.reasonCode,
    };
  }
  const reasonCode = safeDiagnosticCode(
    (error as { reasonCode?: unknown } | null)?.reasonCode,
  );
  return {
    code: reasonCode ?? (isNetworkFailure(error) ? "service_unreachable" : undefined),
  };
}

function publishedAuthErrorDiagnostics(
  error: unknown,
): ErrorDiagnostics | null {
  if (
    error instanceof AuthApiError ||
    error instanceof AuthTransportError
  ) {
    return normalizeErrorDiagnostics(error.diagnostics) ?? null;
  }
  return null;
}

function safeDiagnosticCode(value: unknown): string | undefined {
  return typeof value === "string" &&
    /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/.test(value)
    ? value
    : undefined;
}

function inviteRegisterHref(dest: string): string | null {
  try {
    const url = new URL(dest, window.location.origin);
    if (url.pathname !== "/invite" && url.pathname !== "/invite/") return null;
    const token = url.searchParams.get("token") || url.searchParams.get("invite");
    const params = new URLSearchParams({ source: "invite" });
    if (token) params.set("token", token);
    return `/register/?${params.toString()}`;
  } catch {
    return dest.startsWith("/invite") ? "/register/?source=invite" : null;
  }
}

function consumeOAuthCallbackError(): {
  code: string;
  provider: OAuthProvider | null;
} | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const code = params.get("oauth_error");
  if (!code) return null;
  const rawProvider = params.get("oauth_provider");
  const provider = rawProvider === "feishu"
    ? rawProvider
    : null;
  params.delete("oauth_error");
  params.delete("oauth_provider");
  const query = params.toString();
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`,
  );
  return { code, provider };
}

function oauthProviderName(
  t: ReturnType<typeof useTranslations<"auth">>,
  provider: OAuthProvider | null,
): string {
  if (provider === "feishu") return t("login.provider_feishu");
  return t("login.provider_generic");
}

function oauthErrorKey(code: string) {
  const editionError = editionLoginError(code);
  if (editionError) return editionError;
  switch (code) {
    case "invalid_oauth_flow":
      return "error.invalid_oauth_flow" as const;
    case "oauth_provider_unsupported":
      return "error.oauth_provider_unsupported" as const;
    case "oauth_not_configured":
      return "error.oauth_not_configured" as const;
    case "oauth_state_invalid":
      return "error.oauth_state_invalid" as const;
    case "public_entry_team_mismatch":
      return "error.generic" as const;
    case "oauth_authorization_incomplete":
      return "error.oauth_authorization_incomplete" as const;
    case "oauth_token_exchange_failed":
      return "error.oauth_token_exchange_failed" as const;
    case "oauth_userinfo_failed":
      return "error.oauth_userinfo_failed" as const;
    case "oauth_identity_missing":
      return "error.oauth_identity_missing" as const;
    case "provider_email_untrusted":
      return "error.provider_email_untrusted" as const;
    case "federated_identity_conflict":
      return "error.federated_identity_conflict" as const;
    case "invite_activation_failed":
      return "error.invite_activation_failed" as const;
    case "oauth_registration_disabled":
      return "error.oauth_registration_disabled" as const;
    case "whitelist_domain_no_team_provisioned":
      return "error.whitelist_domain_no_team_provisioned" as const;
    case "rate_limited":
      return "error.oauth_rate_limited" as const;
    case "feature_disabled":
      return "error.oauth_feature_disabled" as const;
    case "trusted_idp_required":
      return "error.oauth_trusted_idp_required" as const;
    case "invite_expired":
      return "error.oauth_invite_expired" as const;
    case "invite_revoked":
      return "error.oauth_invite_revoked" as const;
    case "invite_consumed":
      return "error.oauth_invite_consumed" as const;
    case "invite_invalid":
      return "error.oauth_invite_invalid" as const;
    case "invite_intent_missing":
      return "error.oauth_invite_intent_missing" as const;
    case "invite_email_mismatch":
      return "error.oauth_invite_email_mismatch" as const;
    case "invite_cross_team_not_allowed":
      return "error.oauth_invite_cross_team" as const;
    case "invite_accept_failed":
      return "error.oauth_invite_accept_failed" as const;
    case "identity_blocked":
      return "error.oauth_identity_blocked" as const;
    case "policy_blocked":
      return "error.oauth_policy_blocked" as const;
    case "seat_exceeded":
      return "error.oauth_seat_exceeded" as const;
    case "over_grace":
      return "error.oauth_over_grace" as const;
    case "suspended":
      return "error.oauth_suspended" as const;
    case "needs_license":
      return "error.oauth_needs_license" as const;
    case "tamper":
      return "error.oauth_tamper" as const;
    case "clock_tamper":
      return "error.oauth_clock_tamper" as const;
    default:
      return "error.oauth_callback_failed" as const;
  }
}
