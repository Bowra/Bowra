"use client";

// Access-request panel for no-access pages. One panel for reports, agents and skills.
//
// Rules from the approved design ("Access Request Spec"):
// - Never reveal whether the item exists: the same copy, button and receipt for missing,
//   cross-team and forbidden items; the state comes from GET /access-requests/status, which
//   answers identically for all of them.
// - The requester never learns who decided; only "a manager" plus the note, verbatim.
// - Neutral tone: no access and a declined request are not errors.
// - One primary action per state.

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Check, Clock, History, Lock } from "@/components/icons";
import { Button } from "@/components/ui/button";
import {
  accessRequestErrorCode,
  getAccessRequestStatus,
  requestAccess,
  withdrawAccessRequest,
  type AccessRequestStatus,
  type AccessRequestKind,
} from "@/lib/access-requests-api";
import { useAccessRequestsEnabled } from "@/lib/use-access-requests-enabled";

export const ACCESS_REQUEST_REASON_MAX = 280;
/** Remaining-character counts that are announced to screen readers while typing. */
const REMAINING_ANNOUNCEMENTS = new Set([20, 10, 0]);

/** What the requester sees. `reapply` = declined, cooldown over; `cooldown` = no open request
 *  but a recent denial still blocks a new one. */
export type AccessRequestPanelView =
  | "loading"
  | "idle"
  | "cooldown"
  | "pending"
  | "denied_cooldown"
  | "reapply"
  | "expired"
  | "approved"
  | "approved_pending";

type Notice = "rate_limited" | "submit_failed" | "withdraw_failed" | null;

const VIEW_TITLE_KEYS: Record<AccessRequestPanelView, string> = {
  loading: "checking",
  idle: "title_no_access",
  cooldown: "title_no_access",
  pending: "pending_title",
  denied_cooldown: "denied_title",
  reapply: "title_no_access",
  expired: "expired_title",
  approved: "approved_title",
  approved_pending: "approved_pending_title",
};

const EMPTY_STATUS: AccessRequestStatus = { state: "none", request: null, retry_after: null };

/** True when a time-limited grant has already run out. */
export function grantHasExpired(grantExpiresAt: string | null | undefined, now: number = Date.now()): boolean {
  if (!grantExpiresAt) return false;
  const ms = Date.parse(grantExpiresAt);
  return Number.isFinite(ms) && ms <= now;
}

/** Pure mapping from the server status to the panel view (exported for tests). */
export function accessRequestPanelView(status: AccessRequestStatus | null, now: number = Date.now()): AccessRequestPanelView {
  if (!status) return "loading";
  switch (status.state) {
    case "pending":
      return "pending";
    case "approved":
      // The panel only shows while the viewer lacks access; an expired grant is asked for again.
      return grantHasExpired(status.request?.grant_expires_at, now) ? "expired" : "approved";
    case "approved_pending":
      return "approved_pending";
    case "denied":
      return status.request?.retry_at || status.retry_after ? "denied_cooldown" : "reapply";
    case "expired":
      return "expired";
    default:
      return status.retry_after ? "cooldown" : "idle";
  }
}

function retryAtFrom(status: AccessRequestStatus, now: number): string | null {
  if (status.request?.retry_at) return status.request.retry_at;
  if (status.retry_after) return new Date(now + status.retry_after * 1000).toISOString();
  return null;
}

export interface AccessRequestPanelProps {
  kind: AccessRequestKind;
  resourceId: string;
  onBack?: () => void;
  /** Approved: open the item (reload the page, navigate to it). */
  onOpen?: () => void;
  className?: string;
}

export function AccessRequestPanel({ kind, resourceId, onBack, onOpen, className }: AccessRequestPanelProps) {
  const t = useTranslations("access_request_panel");
  const locale = useLocale();
  const reasonId = useId();
  const hintId = useId();
  const [status, setStatus] = useState<AccessRequestStatus | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"submit" | "withdraw" | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [announcement, setAnnouncement] = useState("");
  const [countAnnouncement, setCountAnnouncement] = useState("");
  const versionRef = useRef(0);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Set by submit / withdraw: the next settled view takes focus and is announced.
  const focusAfterChangeRef = useRef(false);

  const load = useCallback(async () => {
    const version = ++versionRef.current;
    try {
      const next = await getAccessRequestStatus(kind, resourceId);
      if (version === versionRef.current) setStatus(next);
    } catch {
      // The status read never reveals existence; on a transient failure offer the request form.
      if (version === versionRef.current) setStatus(EMPTY_STATUS);
    }
  }, [kind, resourceId]);

  useEffect(() => {
    setStatus(null);
    setNotice(null);
    setReason("");
    void load();
    return () => {
      ++versionRef.current;
    };
  }, [load]);

  const formatDate = (iso: string | null | undefined) => {
    if (!iso) return "";
    const ms = Date.parse(iso);
    if (!Number.isFinite(ms)) return "";
    return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(ms);
  };

  const submit = async () => {
    setBusy("submit");
    setNotice(null);
    try {
      const result = await requestAccess(kind, resourceId, reason.trim() || undefined);
      focusAfterChangeRef.current = true;
      setReason("");
      setStatus({
        state: "pending",
        retry_after: null,
        request: {
          request_id: result.request_id ?? "",
          kind,
          resource_id: resourceId,
          state: "pending",
          message: null,
          note: null,
          created_at: new Date().toISOString(),
          expires_at: null,
          decided_at: null,
          granted_permission: null,
          grant_expires_at: null,
          retry_after: null,
          retry_at: null,
        },
      });
      void load();
    } catch (error) {
      const code = accessRequestErrorCode(error);
      if (code === "access_request_cooldown") {
        focusAfterChangeRef.current = true;
        void load();
      } else if (code === "access_request_rate_limited") {
        setNotice("rate_limited");
      } else {
        setNotice("submit_failed");
      }
    } finally {
      setBusy(null);
    }
  };

  const withdraw = async () => {
    const requestId = status?.request?.request_id;
    if (!requestId) return;
    setBusy("withdraw");
    setNotice(null);
    try {
      await withdrawAccessRequest(requestId);
      focusAfterChangeRef.current = true;
      setStatus(EMPTY_STATUS);
      void load();
    } catch (error) {
      if (accessRequestErrorCode(error) === "access_request_not_pending") {
        focusAfterChangeRef.current = true;
        void load();
      }
      else setNotice("withdraw_failed");
    } finally {
      setBusy(null);
    }
  };

  const view = accessRequestPanelView(status);
  const viewTitleKey = VIEW_TITLE_KEYS[view];
  useEffect(() => {
    if (view === "loading" || !focusAfterChangeRef.current) return;
    focusAfterChangeRef.current = false;
    headingRef.current?.focus();
    setAnnouncement(t(viewTitleKey));
  }, [view, viewTitleKey, t]);
  const request = status?.request ?? null;
  const retryAt = status ? formatDate(retryAtFrom(status, Date.now())) : "";

  const back = (variant: "primary" | "secondary") =>
    onBack ? (
      <Button variant={variant} onClick={onBack}>
        {t("back")}
      </Button>
    ) : null;

  const requestButton = (label: string) => (
    <Button
      variant="primary"
      onClick={() => void submit()}
      disabled={busy !== null}
      loading={busy === "submit"}
      data-testid="access-request-submit"
    >
      {view === "idle" ? <Lock size={13} aria-hidden /> : null}
      {busy === "submit" ? t("requesting") : label}
    </Button>
  );

  const reasonForm = (
    <div className="acr-panel-form">
      <label className="acr-panel-label" htmlFor={reasonId}>
        {t("reason_label")}
      </label>
      <textarea
        id={reasonId}
        className="acr-panel-textarea"
        value={reason}
        maxLength={ACCESS_REQUEST_REASON_MAX}
        placeholder={t("reason_placeholder")}
        aria-describedby={hintId}
        onChange={(event) => {
          const next = event.target.value.slice(0, ACCESS_REQUEST_REASON_MAX);
          const remaining = ACCESS_REQUEST_REASON_MAX - next.length;
          if (REMAINING_ANNOUNCEMENTS.has(remaining)) setCountAnnouncement(t("reason_remaining", { count: remaining }));
          else if (remaining > 20) setCountAnnouncement("");
          setReason(next);
        }}
        disabled={busy !== null}
      />
      <div className="acr-panel-hint" id={hintId}>
        <span>{t("reason_hint")}</span>
        <span className="acr-panel-hint-count">
          {t("reason_count", { count: reason.length, max: ACCESS_REQUEST_REASON_MAX })}
        </span>
      </div>
      <span className="acr-sr-only" aria-live="polite" data-testid="access-request-count-live">
        {countAnnouncement}
      </span>
    </div>
  );

  const note = request?.note ? (
    <div className="acr-panel-quote">
      <span className="acr-panel-quote-label">{t("note_label")}</span>
      {request.note}
    </div>
  ) : null;

  let icon: ReactNode = <Lock size={20} aria-hidden />;
  let iconTone = "";
  let title = t("title_no_access");
  let body: ReactNode = t(`body_${kind}`);
  let content: ReactNode = null;
  let lead: ReactNode = null;
  let actions: ReactNode = null;

  switch (view) {
    case "loading":
      body = t("checking");
      break;
    case "idle":
      content = reasonForm;
      actions = (
        <>
          {back("secondary")}
          {requestButton(t("request"))}
        </>
      );
      break;
    case "cooldown":
      body = t("retry_at", { date: retryAt });
      actions = (
        <>
          {back("primary")}
          <Button variant="secondary" disabled>
            {t("request_again")}
          </Button>
        </>
      );
      break;
    case "pending":
      icon = <Clock size={20} aria-hidden />;
      iconTone = "is-pending";
      title = t("pending_title");
      body = t("pending_body", { date: formatDate(request?.created_at) });
      actions = (
        <>
          {back("primary")}
          {request?.request_id ? (
            <Button
              variant="ghost"
              onClick={() => void withdraw()}
              disabled={busy !== null}
              loading={busy === "withdraw"}
            >
              {busy === "withdraw" ? t("withdrawing") : t("withdraw")}
            </Button>
          ) : null}
        </>
      );
      break;
    case "denied_cooldown":
      title = t("denied_title");
      body = t("retry_at", { date: retryAt });
      lead = note;
      actions = (
        <>
          {back("primary")}
          <Button variant="secondary" disabled>
            {t("request_again")}
          </Button>
        </>
      );
      break;
    case "reapply":
      body = t("reapply_body", { date: formatDate(request?.decided_at ?? request?.created_at) });
      lead = note;
      content = reasonForm;
      actions = (
        <>
          {back("secondary")}
          {requestButton(t("request_again"))}
        </>
      );
      break;
    case "expired":
      icon = <History size={20} aria-hidden />;
      title = t("expired_title");
      body = t("expired_body");
      content = reasonForm;
      actions = (
        <>
          {back("secondary")}
          {requestButton(t("request_again"))}
        </>
      );
      break;
    case "approved":
      icon = <Check size={20} aria-hidden />;
      iconTone = "is-approved";
      title = t("approved_title");
      body = (
        <>
          {t(`approved_body_${kind}`)}
          {locale.startsWith("zh") ? "" : " "}
          {request?.grant_expires_at
            ? t("approved_until", { date: formatDate(request.grant_expires_at) })
            : t("approved_permanent")}
        </>
      );
      lead = note;
      content = reasonForm;
      actions = (
        <>
          {onOpen ? (
            <Button variant="primary" onClick={onOpen}>
              {t("open")}
            </Button>
          ) : (
            back("primary")
          )}
          <Button variant="secondary" onClick={() => void submit()} disabled={busy !== null} loading={busy === "submit"}>
            {busy === "submit" ? t("requesting") : t("request_again")}
          </Button>
        </>
      );
      break;
    case "approved_pending":
      icon = <Check size={20} aria-hidden />;
      iconTone = "is-approved";
      title = t("approved_pending_title");
      body = t("approved_pending_body");
      content = note;
      actions = back("primary");
      break;
  }

  return (
    <section
      className={["acr-panel", className].filter(Boolean).join(" ")}
      data-testid="access-request-panel"
      data-view={view}
      aria-busy={view === "loading" ? true : undefined}
    >
      <span className={["acr-panel-icon", iconTone].filter(Boolean).join(" ")} aria-hidden>
        {icon}
      </span>
      <h2 className="acr-panel-title" ref={headingRef} tabIndex={-1}>
        {title}
      </h2>
      {lead}
      <p className="acr-panel-body">{body}</p>
      {content}
      {notice ? (
        <p className="acr-panel-error" role="status">
          {t(notice)}
        </p>
      ) : null}
      {actions ? <div className="acr-panel-actions">{actions}</div> : null}
      <span className="acr-sr-only" aria-live="polite" data-testid="access-request-live">
        {announcement}
      </span>
    </section>
  );
}

/** "Who can help" for role-gated pages (team settings, admin console, space settings).
 *  Role pages never get a request button (user decision: resources only). */
export function AccessWhoCanHelp({ scope }: { scope: "team" | "space" }) {
  const t = useTranslations("access_request_panel");
  return (
    <div className="acr-who" data-testid={`access-who-can-help-${scope}`}>
      <span className="acr-who-title">{t("who_title")}</span>
      <span>{scope === "team" ? t("who_team") : t("who_space")}</span>
    </div>
  );
}

/** `AccessWhoCanHelp` only while access requests are on (reads the auth session);
 *  renders nothing otherwise. */
export function AccessWhoCanHelpWhenEnabled({ scope }: { scope: "team" | "space" }) {
  return useAccessRequestsEnabled() ? <AccessWhoCanHelp scope={scope} /> : null;
}
