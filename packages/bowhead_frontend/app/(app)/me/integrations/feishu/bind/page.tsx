"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";

import { Check, Lock, Spinner } from "@/components/icons";
import { AppShell } from "@/components/shell/AppShell";
import { ApiError } from "@/lib/api-client";
import {
  completeFeishuBindingSession,
  fetchFeishuBindingSession,
  type FeishuBindingSession,
} from "@/lib/feishu-integration-api";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { useAppRouter } from "@/lib/use-app-router";

import styles from "./bind.module.css";
import {
  Clock as LucideClock,
  Link as LucideLink,
  TriangleAlert as LucideTriangleAlert,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

type PagePhase = "loading" | "ready" | "invalid" | "expired" | "error";
type BindingView = PagePhase | "pending" | "success" | "conflict";

export default function FeishuBindingPage() {
  return (
    <Suspense fallback={<BindingPageFallback />}>
      <FeishuBindingPageInner />
    </Suspense>
  );
}

function FeishuBindingPageInner() {
  const t = useTranslations("settings.feishu_binding");
  const locale = useLocale();
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const sessionId = searchParams.get("session")?.trim() ?? "";
  const auth = useAuthRedirect();
  const [phase, setPhase] = useState<PagePhase>("loading");
  const [session, setSession] = useState<FeishuBindingSession | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [completing, setCompleting] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (auth.status === "loading" || auth.status === "redirecting") {
      setPhase("loading");
      return;
    }
    if (auth.status === "error") {
      setPhase("error");
      return;
    }
    if (auth.status !== "authed") {
      setPhase("error");
      return;
    }
    if (!sessionId) {
      setSession(null);
      setPhase("invalid");
      return;
    }

    let cancelled = false;
    setPhase("loading");
    setSession(null);
    fetchFeishuBindingSession(sessionId)
      .then((nextSession) => {
        if (cancelled) return;
        setSession(nextSession);
        setNowMs(Date.now());
        setPhase("ready");
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setSession(null);
        setPhase(errorPhase(error));
      });
    return () => {
      cancelled = true;
    };
  }, [auth.status, retryVersion, sessionId]);

  const remainingSeconds = useMemo(
    () => secondsUntil(session?.expires_at, nowMs),
    [nowMs, session?.expires_at],
  );
  const view = bindingView(phase, session, remainingSeconds);

  useEffect(() => {
    if (view !== "pending") return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [view]);

  const completeBinding = async () => {
    if (!sessionId || !session || view !== "pending") return;
    setCompleting(true);
    try {
      const completed = await completeFeishuBindingSession(sessionId);
      setSession(completed);
      setPhase("ready");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setSession({ ...session, conflict: true, can_complete: false });
        setPhase("ready");
      } else {
        setPhase(errorPhase(error));
      }
    } finally {
      setCompleting(false);
    }
  };

  const retry = () => {
    setNowMs(Date.now());
    setRetryVersion((version) => version + 1);
  };

  const leave = () => router.push("/");

  return (
    <AppShell
      mobileTitle={t("page_title")}
      preferCompactSidebar
      mainClassName={`v2-main ${styles.main}`}
    >
      <div className={`${styles.page} fb-page`}>
        <div className={`${styles.frame} fb-frame`}>
          <div className={`${styles.context} fb-context`}>
            <span className={`${styles.eyebrow} fb-eyebrow`}>{t("eyebrow")}</span>
            <span className={styles.contextLine} aria-hidden="true" />
            <span className={`${styles.requestId} fb-request-id`}>
              {sessionId
                ? t("request_id", { id: shortenSessionId(sessionId) })
                : t("request_missing")}
            </span>
          </div>

          <section
            className={`${styles.card} fb-card`}
            aria-labelledby="feishu-binding-title"
          >
            <header className={styles.cardHeader}>
              <div className={`${styles.providerMark} fb-provider-mark`} aria-hidden="true">
                <FeishuGlyph />
              </div>
              <div>
                <p className={`${styles.provider} fb-provider`}>{t("provider")}</p>
                <h1 id="feishu-binding-title" className={`${styles.title} fb-title`}>
                  {t("page_title")}
                </h1>
              </div>
            </header>

            <div className={`${styles.divider} fb-divider`} />

            <div className={styles.liveRegion} aria-live="polite">
              {view === "loading" ? (
                <LoadingState t={t} />
              ) : view === "pending" && session ? (
                <PendingState
                  session={session}
                  locale={locale}
                  remainingSeconds={remainingSeconds}
                  completing={completing}
                  onConfirm={() => void completeBinding()}
                  onLeave={leave}
                  t={t}
                />
              ) : view === "success" && session ? (
                <ResultState
                  tone="success"
                  title={t("success_title")}
                  description={t("success_description")}
                  session={session}
                  primaryLabel={t("return_home")}
                  onPrimary={leave}
                  t={t}
                />
              ) : view === "conflict" && session ? (
                <ResultState
                  tone="conflict"
                  title={t("conflict_title")}
                  description={t("conflict_description")}
                  session={session}
                  primaryLabel={t("return_home")}
                  onPrimary={leave}
                  t={t}
                />
              ) : view === "expired" ? (
                <ResultState
                  tone="expired"
                  title={t("expired_title")}
                  description={t("expired_description")}
                  primaryLabel={t("return_home")}
                  onPrimary={leave}
                  t={t}
                />
              ) : view === "invalid" ? (
                <ResultState
                  tone="error"
                  title={t("invalid_title")}
                  description={t("invalid_description")}
                  primaryLabel={t("return_home")}
                  onPrimary={leave}
                  t={t}
                />
              ) : (
                <ResultState
                  tone="error"
                  title={t("error_title")}
                  description={t("error_description")}
                  primaryLabel={t("retry")}
                  onPrimary={retry}
                  secondaryLabel={t("return_home")}
                  onSecondary={leave}
                  t={t}
                />
              )}
            </div>
          </section>

          <p className={`${styles.footerNote} fb-footer-note`}>
            <Lock aria-hidden="true" />
            {t("privacy_note")}
          </p>
        </div>
      </div>
    </AppShell>
  );
}

type Translator = ReturnType<typeof useTranslations<"settings.feishu_binding">>;

function LoadingState({ t }: { t: Translator }) {
  return (
    <div className={styles.stateBody}>
      <StatusBadge tone="loading">
        <Spinner aria-hidden="true" className={styles.spinner} />
      </StatusBadge>
      <div className={`${styles.stateCopy} fb-state-copy`}>
        <h2>{t("loading_title")}</h2>
        <p>{t("loading_description")}</p>
      </div>
      <div className={styles.skeletonGrid} aria-hidden="true">
        <span />
        <span />
      </div>
      <button className={`${styles.primaryButton} fb-primary`} type="button" disabled>
        {t("confirm")}
      </button>
    </div>
  );
}

function PendingState({
  session,
  locale,
  remainingSeconds,
  completing,
  onConfirm,
  onLeave,
  t,
}: {
  session: FeishuBindingSession;
  locale: string;
  remainingSeconds: number;
  completing: boolean;
  onConfirm: () => void;
  onLeave: () => void;
  t: Translator;
}) {
  return (
    <div className={styles.stateBody}>
      <StatusBadge tone="pending">
        <LinkGlyph />
      </StatusBadge>
      <div className={`${styles.stateCopy} fb-state-copy`}>
        <h2>{t("pending_title")}</h2>
        <p>{t("pending_description")}</p>
      </div>

      <dl className={`${styles.identityGrid} fb-identity`}>
        <IdentityRow
          label={t("feishu_email")}
          value={
            session.feishu_email
              ? maskEmail(session.feishu_email)
              : t("email_unavailable")
          }
        />
        <IdentityRow
          label={t("bowhead_account")}
          value={session.current_user_email ?? t("account_unavailable")}
        />
      </dl>

      <div className={`${styles.expiry} fb-expiry`}>
        <span>{t("time_remaining")}</span>
        <strong>{formatCountdown(remainingSeconds)}</strong>
        <small>
          {t("expires_at", { time: formatExpiry(session.expires_at, locale) })}
        </small>
      </div>

      <div className={`${styles.boundaryNote} fb-boundary-note`}>
        <Lock aria-hidden="true" />
        <span>{t("boundary_note")}</span>
      </div>

      <div className={styles.actions}>
        <button
          className={`${styles.primaryButton} fb-primary`}
          type="button"
          disabled={completing || !session.can_complete}
          onClick={onConfirm}
        >
          {completing ? (
            <>
              <Spinner aria-hidden="true" className={styles.spinner} />
              {t("completing")}
            </>
          ) : (
            t("confirm")
          )}
        </button>
        <button
          className={`${styles.secondaryButton} fb-secondary`}
          type="button"
          disabled={completing}
          onClick={onLeave}
        >
          {t("not_now")}
        </button>
      </div>
    </div>
  );
}

function ResultState({
  tone,
  title,
  description,
  session,
  primaryLabel,
  onPrimary,
  secondaryLabel,
  onSecondary,
  t,
}: {
  tone: "success" | "conflict" | "expired" | "error";
  title: string;
  description: string;
  session?: FeishuBindingSession;
  primaryLabel: string;
  onPrimary: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
  t: Translator;
}) {
  return (
    <div
      className={styles.stateBody}
      role={tone === "error" || tone === "conflict" ? "alert" : undefined}
    >
      <StatusBadge tone={tone}>
        {tone === "success" ? (
          <Check aria-hidden="true" />
        ) : (
          <StateGlyph tone={tone} />
        )}
      </StatusBadge>
      <div className={`${styles.stateCopy} fb-state-copy`}>
        <h2>{title}</h2>
        <p>{description}</p>
      </div>

      {session ? (
        <dl className={`${styles.identityGrid} fb-identity`}>
          <IdentityRow
            label={t("feishu_email")}
            value={
              session.feishu_email
                ? maskEmail(session.feishu_email)
                : t("email_unavailable")
            }
          />
          <IdentityRow
            label={t("bowhead_account")}
            value={session.current_user_email ?? t("account_unavailable")}
          />
        </dl>
      ) : null}

      {tone === "conflict" ? (
        <p className={`${styles.privacyCallout} fb-privacy-callout`}>{t("conflict_privacy")}</p>
      ) : null}

      <div className={styles.actions}>
        <button
          className={`${styles.primaryButton} fb-primary`}
          type="button"
          onClick={onPrimary}
        >
          {primaryLabel}
        </button>
        {secondaryLabel && onSecondary ? (
          <button
            className={`${styles.secondaryButton} fb-secondary`}
            type="button"
            onClick={onSecondary}
          >
            {secondaryLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function IdentityRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.identityRow}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function StatusBadge({
  tone,
  children,
}: {
  tone: "loading" | "pending" | "success" | "conflict" | "expired" | "error";
  children: React.ReactNode;
}) {
  return (
    <span className={`${styles.statusBadge} ${styles[tone]}`}>{children}</span>
  );
}

function BindingPageFallback() {
  return <div className={styles.fallback} aria-busy="true" />;
}

function bindingView(
  phase: PagePhase,
  session: FeishuBindingSession | null,
  remainingSeconds: number,
): BindingView {
  if (phase !== "ready") return phase;
  if (!session) return "error";
  if (remainingSeconds <= 0) return "expired";
  if (session.conflict) return "conflict";
  if (session.already_bound_to_current_user || session.state === "bound")
    return "success";
  return "pending";
}

function errorPhase(error: unknown): PagePhase {
  if (error instanceof ApiError) {
    if (error.status === 404) return "invalid";
    if (error.status === 410) return "expired";
  }
  return "error";
}

function secondsUntil(expiresAt: string | undefined, nowMs: number): number {
  if (!expiresAt) return 0;
  const expiryMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiryMs)) return 0;
  return Math.max(0, Math.ceil((expiryMs - nowMs) / 1_000));
}

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function formatExpiry(expiresAt: string, locale: string): string {
  const date = new Date(expiresAt);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function maskEmail(email: string): string {
  const [local, domain] = email.split("@");
  if (!local || !domain) return email;
  const visible = local.slice(0, Math.min(2, local.length));
  return `${visible}${"•".repeat(Math.max(3, local.length - visible.length))}@${domain}`;
}

function shortenSessionId(sessionId: string): string {
  if (sessionId.length <= 14) return sessionId;
  return `${sessionId.slice(0, 8)}…${sessionId.slice(-4)}`;
}

function FeishuGlyph() {
  return (
    <svg viewBox="0 0 28 28" fill="none">
      <path
        d="M8 5.5h8.5a5 5 0 0 1 0 10H12l-4 4v-4.6a4.7 4.7 0 0 1 0-9.4Z"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <path
        d="M11 10.5h8.5a4 4 0 0 1 0 8H18l-3.5 3v-3"
        stroke="currentColor"
        strokeWidth="1.8"
      />
    </svg>
  );
}

function LinkGlyph() {
  return (
    <LucideGlyph icon={LucideLink} size={22} aria-hidden="true" />
  );
}

function StateGlyph({ tone }: { tone: "conflict" | "expired" | "error" }) {
  if (tone === "expired") {
    return (
      <LucideGlyph icon={LucideClock} size={22} aria-hidden="true" />
    );
  }
  return (
    <LucideGlyph icon={LucideTriangleAlert} size={22} aria-hidden="true" />
  );
}
