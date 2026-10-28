"use client";

import {
 Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";

import { AlertTriangle, Clock } from "@/components/icons";
import { roleKey } from "@/lib/invite-role";
import {
  BusyLabel,
  CircleCheckIcon,
  LinkOffIcon,
  PublicBanner,
  PublicShell,
  PublicSteps,
  PublicSummary,
  UserIcon,
  type PublicTone,
  useBrandedTranslations,
} from "@/components/public-shell";
import {
  acceptInvite,
  continueInviteIntent,
  exchangeInviteIntent,
  getAuthCapabilities,
  InviteApiError,
  previewInvite,
  storeInviteResult,
  storeInvitePreview,
  type InviteIntentContext,
  type InviteNextAction,
  type InvitePreviewResponse,
  type InvitePreviewValid,
  type InvitePreviewStatus,
} from "@/lib/invite-api";
import { appFetch, appPath } from "@/lib/base-path";
import { appDocumentHref, useAppRouter } from "@/lib/use-app-router";

type InviteState =
  | { state: "loading" }
  | { state: "missing" }
  | {
      state: "valid";
      mode: "intent" | "legacy";
      token?: string;
      preview: InvitePreviewValid | InviteIntentContext;
      nextAction?: InviteNextAction;
    }
  | { state: "terminal"; status: Exclude<InvitePreviewStatus, "valid">; inviteType?: string }
  | { state: "error"; message: string };

export default function InvitePage() {
  return (
    <Suspense fallback={null}>
      <InvitePageInner />
    </Suspense>
  );
}

function InvitePageInner() {
  const t = useBrandedTranslations("invite");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const [inviteState, setInviteState] = useState<InviteState>({ state: "loading" });
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<string | null>(null);

  const acceptCurrentInvite = async (token?: string) => {
    setAccepting(true);
    setAcceptError(null);
    try {
      const accepted = await acceptInvite(token);
      storeInviteResult(accepted.invite_result);
      window.location.replace(appDocumentHref(
        accepted.invite_result.next_route || accepted.invite_result.ready_route || "/",
      ));
    } catch {
      setAcceptError(t("error.accept_failed"));
    } finally {
      setAccepting(false);
    }
  };

  const continueCurrentInvite = async () => {
    setAccepting(true);
    setAcceptError(null);
    try {
      const preview = await continueInviteIntent();
      storeInvitePreview(preview);
      setInviteState({
        state: "valid",
        mode: "intent",
        preview,
        nextAction: preview.next_action,
      });
      if (preview.next_action === "accept") {
        await acceptCurrentInvite();
      } else if (preview.next_action === "activate_password") {
        router.push("/invite/activate/");
      } else if (
        preview.next_action === "login_password"
        || preview.next_action === "login_federated"
        || preview.next_action === "activate_federated"
      ) {
        router.push("/login/?flow=invite&next=%2Finvite%2F%3Fresume%3D1");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      const terminalStatus = terminalStatusFromError(e);
      if (terminalStatus) {
        setInviteState({ state: "terminal", status: terminalStatus });
      } else {
        setAcceptError(message);
      }
    } finally {
      setAccepting(false);
    }
  };

  // Accept only the latest check result; invalidate on unmount.
  const runRef = useRef(0);
  // True when this page replaces the URL token with ?resume=1 (below).
  const strippedRef = useRef(false);
  useEffect(() => () => {
    runRef.current += 1;
  }, []);

  useEffect(() => {
    const fromUrl = searchParams.get("token") || searchParams.get("invite") || "";
    const isResume = searchParams.get("resume") === "1";
    // Before token exchange, this page replaces URL with ?resume=1; searchParams changes and retriggers this effect.
    // Exchange remains in flight: neither issue another continue nor discard its result—otherwise expired, revoked,
    // or used invitations get masked by continue's missing-intent result as unable to check the invitation.
    if (!fromUrl && isResume && strippedRef.current) {
      strippedRef.current = false;
      return;
    }
    const run = ++runRef.current;
    const cancelled = () => run !== runRef.current;

    setInviteState({ state: "loading" });
    void getAuthCapabilities()
      .then(async (capabilities) => {
        if (capabilities.invite_activation) {
          let preview: InviteIntentContext;
          if (fromUrl) {
            strippedRef.current = true;
            stripInviteTokenFromUrl();
            await exchangeInviteIntent(fromUrl);
            if (cancelled()) return;
            // Exchange creates only server-side intent; immediately derive next step for current visitor (set password / login / accept / switch account),
            // so the landing page can show the corresponding primary button directly.
            preview = await continueInviteIntent();
          } else if (isResume) {
            preview = await continueInviteIntent();
          } else {
            if (!cancelled()) setInviteState({ state: "missing" });
            return;
          }
          if (cancelled()) return;
          storeInvitePreview(preview);
          setInviteState({
            state: "valid",
            mode: "intent",
            preview,
            nextAction: preview.next_action,
          });
          return;
        }
        if (!fromUrl) {
          if (!cancelled()) setInviteState({ state: "missing" });
          return;
        }
        const preview: InvitePreviewResponse = await previewInvite(fromUrl);
        if (cancelled()) return;
        if (preview.status !== "valid") {
          setInviteState({ state: "terminal", status: preview.status });
          return;
        }
        storeInvitePreview(preview);
        setInviteState({ state: "valid", mode: "legacy", token: fromUrl, preview });
      })
      .catch((error: unknown) => {
        if (cancelled()) return;
        const message = error instanceof Error ? error.message : String(error);
        const terminalStatus = terminalStatusFromError(error);
        setInviteState(
          terminalStatus
            ? { state: "terminal", status: terminalStatus }
            : { state: "error", message },
        );
      });
  }, [searchParams]);

  if (inviteState.state === "valid") {
    return (
      <ValidInvite
        preview={inviteState.preview}
        accepting={accepting}
        acceptError={acceptError}
        nextAction={inviteState.nextAction}
        mode={inviteState.mode}
        onAccept={() => void acceptCurrentInvite(inviteState.token)}
        onContinue={() => {
          if (inviteState.mode === "legacy" && inviteState.token) {
            router.push(registerHrefForToken(inviteState.token));
            return;
          }
          void continueCurrentInvite();
        }}
        onLogin={() => router.push(
          inviteState.mode === "legacy" && inviteState.token
            ? loginHrefForToken(inviteState.token)
            : "/login/?flow=invite&next=%2Finvite%2F%3Fresume%3D1",
        )}
        onSwitchAccount={() => void switchAccountForInvite()}
      />
    );
  }

  if (inviteState.state === "terminal") {
    return <TerminalInvite status={inviteState.status} />;
  }

  if (inviteState.state === "missing") {
    return (
      <IssueCard
        tone="danger"
        icon={<LinkOffIcon />}
        title={t("error.missing_title")}
        body={t("error.missing_body")}
        primary={<a className="pub-button pub-button--primary" href={appPath("/login/")}>{t("action.back_to_login")}</a>}
      />
    );
  }

  if (inviteState.state === "error") {
    return (
      <IssueCard
        tone="warning"
        icon={<AlertTriangle size={20} />}
        title={t("error.check_failed_title")}
        body={t("error.check_failed_fallback")}
        primary={<button className="pub-button pub-button--primary" type="button" onClick={() => window.location.reload()}>{t("action.try_again")}</button>}
      />
    );
  }

  return (
    <IssueCard
      tone="neutral"
      delayed
      icon={<span className="pub-spinner pub-spinner--lg" />}
      title={t("landing.checking_title")}
      body={t("landing.checking_body")}
    />
  );
}

function ValidInvite({
  preview,
  accepting,
  acceptError,
  nextAction,
  mode,
  onAccept,
  onContinue,
  onLogin,
  onSwitchAccount,
}: {
  preview: InvitePreviewValid | InviteIntentContext;
  accepting: boolean;
  acceptError: string | null;
  nextAction?: InviteNextAction;
  mode: "intent" | "legacy";
  onAccept: () => void;
  onContinue: () => void;
  onLogin: () => void;
  onSwitchAccount: () => void;
}) {
  const t = useBrandedTranslations("invite");
  const isWorkspace = preview.invite_type === "workspace";
  const title = isWorkspace
    ? t("landing.space_title", {
        name: preview.workspace?.name || t("common.this_space"),
      })
    : t("landing.team_title", { name: preview.team.name });
  const lead = isWorkspace
    ? t("landing.space_lead", { team: preview.team.name })
    : t("landing.team_lead", { team: preview.team.name });
  const viewerState = preview.viewer_context?.state;
  const matching = viewerState === "logged_in_matching_email";
  const mismatch = viewerState === "logged_in_mismatched_email" || nextAction === "switch_account";
  const wrongTeam = viewerState === "logged_in_wrong_team";
  const blocked = nextAction === "identity_blocked" || nextAction === "policy_blocked";
  const modernAccept = nextAction === "accept";
  const needsLogin = nextAction === "login_password" || nextAction === "login_federated";
  const federatedActivation = nextAction === "activate_federated";
  const primaryLabel = blocked
    ? t("action.contact_admin")
    : mismatch
      ? t("action.switch_account")
      : mode === "intent" && !nextAction
        ? accepting ? t("action.continuing") : t("action.continue")
      : modernAccept || matching
        ? accepting ? t("action.accepting") : t("action.accept")
        : needsLogin
          ? t("action.login_to_continue")
          : federatedActivation
            ? t("action.continue_with_provider")
            : t("action.create_profile");
  const primaryAction = mismatch
    ? onSwitchAccount
    : modernAccept || matching
      ? onAccept
      : needsLogin || federatedActivation
        ? onLogin
        : onContinue;

  const secondary = mode === "legacy"
    ? (wrongTeam ? t("action.back_to_login") : t("action.login_instead"))
    : nextAction === "activate_password"
      ? t("action.login_instead")
      : null;

  return (
    <PublicShell
      width="invite"
      steps={<PublicSteps current={1} label={t("progress.step_1.title")} />}
      title={title}
      byline={preview.inviter_display_name
        ? t("landing.invited_by", {
            inviter: preview.inviter_display_name,
            target: isWorkspace
              ? preview.workspace?.name || t("common.this_space")
              : preview.team.name,
          })
        : undefined}
      subtitle={lead}
    >
      <InviteSummary preview={preview} />
      {mismatch ? (
        <PublicBanner tone="warning" icon={<UserIcon />}>
          {t("error.account_mismatch", {
            current: preview.viewer_context?.email_mask || t("error.another_account"),
            invited: preview.invited_email_mask,
          })}
        </PublicBanner>
      ) : null}
      {wrongTeam ? (
        <PublicBanner tone="warning">{t("error.wrong_team")}</PublicBanner>
      ) : null}
      {blocked ? (
        <PublicBanner tone="error">
          {nextAction === "identity_blocked"
            ? t("error.identity_blocked")
            : t("error.policy_blocked")}
        </PublicBanner>
      ) : null}
      {acceptError ? (
        <PublicBanner tone="error" focusKey={acceptError}>{acceptError}</PublicBanner>
      ) : null}
      <div className="pub-actions">
        <button
          className="pub-button pub-button--primary"
          type="button"
          onClick={primaryAction}
          disabled={accepting || wrongTeam || blocked}
          aria-busy={accepting || undefined}
        >
          <BusyLabel
            busy={accepting}
            idle={wrongTeam ? t("action.cannot_accept") : primaryLabel}
            pending={primaryLabel}
          />
        </button>
        {secondary ? (
          <button className="pub-button pub-button--ghost" type="button" onClick={onLogin}>
            {secondary}
          </button>
        ) : null}
      </div>
      <p className="pub-note">{t("summary.permission_note")}</p>
    </PublicShell>
  );
}

function registerHrefForToken(token: string): string {
  return `/register/?source=invite&token=${encodeURIComponent(token)}`;
}

function loginHrefForToken(token: string): string {
  const next = `/invite/?token=${encodeURIComponent(token)}&accept=1`;
  return `/login/?next=${encodeURIComponent(next)}`;
}

function stripInviteTokenFromUrl(): void {
  try {
    window.history.replaceState(null, "", appPath("/invite/?resume=1"));
  } catch {
    /* The server-side intent still prevents later steps from reading the raw token. */
  }
}

function terminalStatusFromError(
  error: unknown,
): Exclude<InvitePreviewStatus, "valid"> | null {
  const code = error instanceof InviteApiError ? error.code : undefined;
  if (code === "invite_expired") return "expired";
  if (code === "invite_revoked") return "revoked";
  if (code === "invite_consumed") return "consumed";
  if (code === "invite_invalid") return "invalid";
  return null;
}

async function switchAccountForInvite(): Promise<void> {
  try {
    await appFetch("/api/v1/auth/logout", {
      method: "POST",
      credentials: "include",
    });
  } finally {
    window.location.replace(appPath("/login/?flow=invite&next=%2Finvite%2F%3Fresume%3D1"));
  }
}

function InviteSummary({ preview }: { preview: InvitePreviewValid | InviteIntentContext }) {
  const t = useBrandedTranslations("invite");
  const locale = useLocale();
  const isWorkspace = preview.invite_type === "workspace";
  const rows: Array<{ term: string; value: string; detail?: string }> = [
    { term: t("summary.team"), value: preview.team.name },
  ];
  if (isWorkspace) {
    rows.push({
      term: t("summary.space"),
      value: preview.workspace?.name || t("common.bowhead_space"),
      detail: roleDetail(t, preview.workspace?.role),
    });
  }
  rows.push(
    { term: t("summary.email"), value: preview.invited_email_mask },
    {
      term: t("summary.expires"),
      value: formatDate(preview.expires_at, locale, t("summary.not_specified")),
    },
  );
  return <PublicSummary label={t("summary.aria")} rows={rows} />;
}

function TerminalInvite({ status }: { status: Exclude<InvitePreviewStatus, "valid"> }) {
  const t = useBrandedTranslations("invite");
  const backToLogin = (
    <a className="pub-button pub-button--primary" href={appPath("/login/")}>
      {t("action.back_to_login")}
    </a>
  );
  if (status === "expired") {
    return (
      <IssueCard
        tone="warning"
        icon={<Clock size={20} />}
        title={t("error.expired_title")}
        body={t("error.expired_body")}
        primary={backToLogin}
      />
    );
  }
  if (status === "revoked") {
    return (
      <IssueCard
        tone="danger"
        icon={<LinkOffIcon />}
        title={t("error.revoked_title")}
        body={t("error.revoked_body")}
        primary={backToLogin}
      />
    );
  }
  if (status === "consumed") {
    // The most common cause is that the invitee already accepted it, so the primary button is "Sign in".
    return (
      <IssueCard
        tone="neutral"
        icon={<CircleCheckIcon />}
        title={t("error.consumed_title")}
        body={t("error.consumed_body")}
        primary={(
          <a className="pub-button pub-button--primary" href={appPath("/login/")}>
            {t("action.sign_in")}
          </a>
        )}
      />
    );
  }
  return (
    <IssueCard
      tone="danger"
      icon={<LinkOffIcon />}
      title={t("error.invalid_title")}
      body={t("error.invalid_body")}
      primary={backToLogin}
    />
  );
}

/** Terminal states offer a single next step: 44px icon circle + title + one sentence + one primary button. */
function IssueCard({
  tone,
  icon,
  title,
  body,
  primary,
  delayed = false,
}: {
  tone: PublicTone;
  icon: ReactNode;
  title: string;
  body: string;
  primary?: ReactNode;
  delayed?: boolean;
}) {
  return (
    <PublicShell
      width="invite"
      status={{ tone, icon, delayed }}
      title={title}
      subtitle={body}
    >
      {primary ? <div className="pub-actions">{primary}</div> : null}
    </PublicShell>
  );
}

/** Role plus one sentence on what it can do (wording follows the space role matrix). */
function roleDetail(
  t: ReturnType<typeof useTranslations<"invite">>,
  role?: string | null,
): string {
  const key = roleKey(role);
  const description = key === "roles.admin"
    ? t("roles_desc.admin")
    : key === "roles.viewer"
      ? t("roles_desc.viewer")
      : t("roles_desc.member");
  return `${t(key)} · ${description}`;
}

function formatDate(
  value: string | null | undefined,
  locale: string,
  missing: string,
): string {
  if (!value) return missing;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
