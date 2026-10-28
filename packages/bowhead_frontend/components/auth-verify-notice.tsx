"use client";

/**
 * §C (real-auth) · AuthVerifyNotice — shown on /login after a 403 email_not_verified.
 * Tells the user to check their email and offers a resend (the by-email public endpoint,
 * since they have no session yet). Information banner with a text-link resend, not a danger box;
 * after a send the link says so and stays unavailable for 60 seconds.
 */

import { useEffect, useState } from "react";

import { resendVerificationEmail } from "@/lib/auth-api";
import { MailIcon, PublicBanner, useBrandedTranslations } from "./public-shell";

export const RESEND_COOLDOWN_MS = 60_000;

export function AuthVerifyNotice({ email }: { email: string }) {
  const t = useBrandedTranslations("auth");
  const [sending, setSending] = useState(false);
  const [sentCount, setSentCount] = useState(0);
  const [err, setErr] = useState<string | null>(null);
  const cooling = useResendCooldown(sentCount);

  const resend = async () => {
    setErr(null);
    setSending(true);
    try {
      await resendVerificationEmail(email);
      setSentCount((n) => n + 1);
    } catch {
      // Public endpoint is no-leak (always 200) — a thrown error is a network/transport
      // failure, so a generic retry hint is correct here.
      setErr(t("verify.resend_failed"));
    } finally {
      setSending(false);
    }
  };

  return (
    <PublicBanner
      tone="info"
      icon={<MailIcon size={14} />}
      title={t("verify.pending_title")}
      testId="auth-verify-notice"
    >
      <span>{t("verify.pending_message", { email })}</span>
      <button
        type="button"
        onClick={() => void resend()}
        disabled={sending || cooling}
        className="pub-link-button"
      >
        {sending
          ? t("verify.resending")
          : cooling
            ? t("verify.resent")
            : t("verify.resend")}
      </button>
      {err ? <span>{err}</span> : null}
    </PublicBanner>
  );
}

/** Disabled for 60 seconds after a successful send, then back to "Resend verification email" (mail may not arrive, so resending must stay possible). */
export function useResendCooldown(sentCount: number): boolean {
  const [cooling, setCooling] = useState(false);
  useEffect(() => {
    if (sentCount === 0) return;
    setCooling(true);
    const timer = window.setTimeout(() => setCooling(false), RESEND_COOLDOWN_MS);
    return () => window.clearTimeout(timer);
  }, [sentCount]);
  return cooling;
}
