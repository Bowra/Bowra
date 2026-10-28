"use client";

import {
 useEffect, useRef, useState } from "react";

import {
  AuthError,
  AuthField,
  AuthShell,
  PasswordInput,
  PasswordRules,
  passwordMeetsRules,
} from "@/components/auth-shell";
import { roleKey } from "@/lib/invite-role";
import {
  BusyLabel,
  LockIcon,
  PublicSteps,
  PublicSummary,
  useBrandedTranslations,
} from "@/components/public-shell";
import {
  activateInvite,
  continueInviteIntent,
  storeInviteResult,
  type InviteIntentContext,
} from "@/lib/invite-api";
import { appPath } from "@/lib/base-path";
import { appDocumentHref, useAppRouter } from "@/lib/use-app-router";

export default function InviteActivatePage() {
  const authT = useBrandedTranslations("auth");
  const inviteT = useBrandedTranslations("invite");
  const router = useAppRouter();
  const [context, setContext] = useState<InviteIntentContext | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ password?: string; password2?: string }>({});
  const passwordRef = useRef<HTMLInputElement>(null);
  const password2Ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void continueInviteIntent()
      .then((next) => {
        if (cancelled) return;
        if (next.next_action !== "activate_password") {
          router.replace("/invite/?resume=1");
          return;
        }
        setContext(next);
      })
      .catch(() => router.replace("/invite/?resume=1"));
    return () => {
      cancelled = true;
    };
  }, [router]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError(null);
    const errors = {
      password: !password
        ? authT("field_error.password_required")
        : passwordMeetsRules(password) ? undefined : authT("field_error.password_rules"),
      password2: !password2
        ? authT("field_error.password_confirm_required")
        : password2 !== password ? authT("error.password_mismatch") : undefined,
    };
    setFieldErrors(errors);
    if (errors.password || errors.password2) {
      (errors.password ? passwordRef : password2Ref).current?.focus();
      return;
    }
    setBusy(true);
    try {
      const response = await activateInvite({
        display_name: displayName.trim() || undefined,
        password,
      });
      storeInviteResult(response.invite_result);
      window.location.replace(appDocumentHref(
        response.invite_result.next_route || "/invite/ready",
      ));
    } catch {
      setError(inviteT("activate.error"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthShell
      width="invite"
      steps={<PublicSteps current={2} label={inviteT("progress.step_2.title")} />}
      title={inviteT("activate.title")}
      subtitle={inviteT("activate.subtitle")}
      footer={(
        <a
          href={appPath("/login/?flow=invite&next=%2Finvite%2F%3Fresume%3D1")}
          className="pub-link"
        >
          {inviteT("activate.login_instead")}
        </a>
      )}
    >
      <InviteActivationSummary context={context} />
      <form onSubmit={submit} className="pub-form" noValidate>
        <AuthError message={error} focusKey={error} />
        <div className="pub-fields">
          <AuthField label={inviteT("activate.invited_email_label")} hint={inviteT("activate.invited_email_fixed")}>
            {(control) => (
              <div className="pub-input-wrap">
                <input
                  {...control}
                  type="text"
                  readOnly
                  value={context?.invited_email_mask ?? ""}
                  className="pub-input"
                />
                <LockIcon className="pub-input-wrap__icon" aria-hidden="true" />
              </div>
            )}
          </AuthField>
          <AuthField label={authT("field.display_name")} aside={authT("field.optional")}>
            {(control) => (
              <input
                {...control}
                type="text"
                value={displayName}
                autoComplete="name"
                onChange={(event) => setDisplayName(event.target.value)}
                className="pub-input"
                placeholder={authT("register.display_name_placeholder")}
              />
            )}
          </AuthField>
          <AuthField
            label={authT("field.password")}
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
                showLabel={authT("field.show_password")}
                hideLabel={authT("field.hide_password")}
              />
            )}
          </AuthField>
          <AuthField label={authT("field.password_confirm")} error={fieldErrors.password2}>
            {(control) => (
              <PasswordInput
                control={control}
                inputRef={password2Ref}
                value={password2}
                required
                readOnly={busy}
                autoComplete="new-password"
                minLength={8}
                onChange={(value) => {
                  setPassword2(value);
                  if (fieldErrors.password2) setFieldErrors((prev) => ({ ...prev, password2: undefined }));
                }}
                onBlur={() => {
                  if (password2 && password2 !== password) {
                    setFieldErrors((prev) => ({ ...prev, password2: authT("error.password_mismatch") }));
                  }
                }}
                showLabel={authT("field.show_password")}
                hideLabel={authT("field.hide_password")}
              />
            )}
          </AuthField>
        </div>
        <button
          type="submit"
          disabled={busy || !context}
          aria-busy={busy || undefined}
          className="pub-button pub-button--primary"
        >
          <BusyLabel
            busy={busy}
            idle={inviteT("action.set_password_continue")}
            pending={inviteT("activate.creating")}
          />
        </button>
      </form>
    </AuthShell>
  );
}

function InviteActivationSummary({ context }: { context: InviteIntentContext | null }) {
  const t = useBrandedTranslations("invite");
  if (!context) {
    return <p className="pub-note">{t("activate.checking_detail")}</p>;
  }
  const isWorkspace = context.invite_type === "workspace";
  const rows: Array<{ term: string; value: string; detail?: string }> = [
    { term: t("summary.team"), value: context.team.name },
  ];
  if (isWorkspace) {
    rows.push({
      term: t("summary.space"),
      value: context.workspace?.name || t("common.invited_space"),
      detail: t(roleKey(context.workspace?.role)),
    });
  }
  return <PublicSummary label={t("summary.aria")} rows={rows} />;
}
