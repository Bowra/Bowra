"use client";

import {
 Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { AuthField } from "@/components/auth-shell";
import {
  BusyLabel,
  MailIcon,
  PublicBanner,
  PublicShell,
  PublicSteps,
  PublicSummary,
  useBrandedTranslations,
} from "@/components/public-shell";
import { resendVerificationEmail } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { readInvitePreview, readInviteResult, readInviteVerifyEmail } from "@/lib/invite-api";
import { safeInternalPath } from "@/lib/safe-next-path";

export default function InviteVerifyPendingPage() {
  return (
    <Suspense fallback={null}>
      <InviteVerifyPendingInner />
    </Suspense>
  );
}

function InviteVerifyPendingInner() {
  const t = useBrandedTranslations("invite");
  const searchParams = useSearchParams();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const context = useMemo(() => {
    const result = readInviteResult();
    const preview = readInvitePreview();
    const email = readInviteVerifyEmail();
    const workspaceName = result?.workspace?.name || preview?.workspace?.name || "";
    const teamName = result?.team?.name || preview?.team?.name || "";
    const maskedEmail = preview?.invited_email_mask || "";
    return { result, preview, email, workspaceName, teamName, maskedEmail };
  }, []);
  const [email, setEmail] = useState(context.email);

  const resend = async () => {
    const resendEmail = email.trim().toLowerCase();
    if (!resendEmail) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      await resendVerificationEmail(resendEmail);
      setNotice(t("verify.sent"));
    } catch {
      setError(t("verify.send_failed"));
    } finally {
      setBusy(false);
    }
  };

  const readyRoute = safeInternalPath(searchParams.get("next"))
    || safeInternalPath(context.result?.ready_route || "");
  const isWorkspace = context.result?.invite_type === "workspace"
    || context.preview?.invite_type === "workspace"
    || readyRoute?.includes("state=workspace_ready");
  const target = isWorkspace
    ? context.workspaceName || t("verify.invited_space")
    : context.teamName || t("verify.bowhead_team");
  const postVerifyRoute = readyRoute
    ? `/onboarding/profile/?source=invite&next=${encodeURIComponent(readyRoute)}`
    : "/onboarding/profile/?source=invite";
  const loginHref = `/login/?next=${encodeURIComponent(postVerifyRoute)}`;

  return (
    <PublicShell
      width="invite"
      steps={<PublicSteps current={2} label={t("progress.step_2.title")} />}
      status={{ tone: "neutral", icon: <MailIcon /> }}
      title={t("verify.title")}
      subtitle={t("verify.lead", { target })}
      footer={(
        <a className="pub-link" href={appPath(loginHref)}>
          {t("verify.go_to_login")}
        </a>
      )}
    >
      <PublicSummary
        label={t("verify.summary_aria")}
        rows={[
          {
            term: t("summary.status"),
            value: isWorkspace ? t("verify.space_pending") : t("verify.team_pending"),
          },
          { term: t("summary.next"), value: t("verify.next") },
          { term: t("summary.destination"), value: target },
        ]}
      />
      <p className="pub-note">
        {context.maskedEmail
          ? t("verify.invited_email", { email: context.maskedEmail })
          : t("verify.same_email")}
      </p>
      {!context.email ? (
        <div className="pub-fields">
          <AuthField label={t("verify.registration_email")}>
            {(control) => (
              <input
                {...control}
                type="email"
                value={email}
                autoComplete="email"
                autoCapitalize="off"
                spellCheck={false}
                onChange={(e) => setEmail(e.target.value.toLowerCase())}
                placeholder="you@company.com"
                className="pub-input"
              />
            )}
          </AuthField>
        </div>
      ) : null}
      {notice ? <PublicBanner tone="success" focusKey={notice}>{notice}</PublicBanner> : null}
      {error ? <PublicBanner tone="error" focusKey={error}>{error}</PublicBanner> : null}
      <div className="pub-actions">
        <button
          className="pub-button pub-button--primary"
          type="button"
          disabled={busy || !email.trim()}
          aria-busy={busy || undefined}
          onClick={resend}
        >
          <BusyLabel busy={busy} idle={t("verify.resend")} pending={t("action.sending")} />
        </button>
      </div>
    </PublicShell>
  );
}
