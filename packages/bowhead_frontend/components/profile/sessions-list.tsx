"use client";

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import {
  SecurityError,
  type SessionRow,
  revokeSession,
} from "@/lib/security-api";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { CharIcon } from "@/components/char-icon";
import { formatDateTime } from "@/lib/locale";


interface Props {
  items: SessionRow[];
  currentIdentified: boolean;
  onRevoked: () => void;
}


function formatTime(iso: string | null, locale: string): string {
  if (!iso) return "—";
  return formatDateTime(iso, locale, { dateStyle: "medium", timeStyle: "short" }) || iso;
}

function deviceLabel(userAgent: string | null, fallback: string): string {
  const agent = userAgent ?? "";
  const browser = [
    [/\b(?:Edg|EdgiOS|EdgA|Edge)\b/i, "Edge"],
    [/\b(?:OPR|Opera)\b/i, "Opera"],
    [/\bSamsungBrowser\b/i, "Samsung Internet"],
    [/\b(?:Firefox|FxiOS)\b/i, "Firefox"],
    [/\b(?:Chrome|CriOS|Chromium)\b/i, "Chrome"],
    [/\bSafari\b/i, "Safari"],
  ] as const;
  const system = [
    [/\biPad\b/i, "iPadOS"],
    [/\biPhone\b/i, "iOS"],
    [/\bAndroid\b/i, "Android"],
    [/\bWindows\b/i, "Windows"],
    [/\b(?:Macintosh|macOS)\b/i, "macOS"],
    [/\bCrOS\b/i, "ChromeOS"],
    [/\bLinux\b/i, "Linux"],
  ] as const;
  return [browser.find(([pattern]) => pattern.test(agent))?.[1], system.find(([pattern]) => pattern.test(agent))?.[1]]
    .filter(Boolean).join(" · ") || fallback;
}

function lastActive(session: SessionRow): string {
  return session.last_used_at ?? session.created_at;
}

function groupDevices(items: SessionRow[]): Map<string, SessionRow[]> {
  const groups = new Map<string, SessionRow[]>();
  for (const session of [...items].sort((a, b) => Date.parse(lastActive(b)) - Date.parse(lastActive(a)))) {
    const key = session.device_key ?? session.session_id;
    const group = groups.get(key);
    if (group) group.push(session);
    else groups.set(key, [session]);
  }
  return groups;
}

export function SessionsList({ items, currentIdentified, onRevoked }: Props) {
  const t = useTranslations("profile.security");
  const locale = useLocale();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<{
    key: string;
    current: boolean;
    sessionIds: string[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleRevoke = async () => {
    if (!pendingRevoke) return;
    setRevoking(pendingRevoke.key);
    setError(null);
    try {
      for (const sessionId of pendingRevoke.sessionIds) {
        try {
          await revokeSession(sessionId);
        } catch (cause) {
          if (cause instanceof SecurityError && cause.status === 404) continue;
          setError(cause instanceof SecurityError ? cause.message : t("session_revoke_failed"));
          break;
        }
      }
      setPendingRevoke(null);
      onRevoked();
    } finally {
      setRevoking(null);
    }
  };

  return (
    <div className="personal-security-sessions">
      {error && (
        <div className="personal-security-sessions__notice is-error" role="alert">
          <CharIcon ch="⚠" /> {error}
        </div>
      )}

      {!currentIdentified && (
        <div className="personal-security-sessions__notice is-warning" role="alert">
          <CharIcon ch="⚠" /> {t("session_warn_unidentified")}
        </div>
      )}

      {items.length === 0 && (
        <div className="personal-security-sessions__empty">{t("session_empty")}</div>
      )}

      <div className="personal-security-sessions__list">
        {Array.from(groupDevices(items), ([key, sessions]) => {
          const [latest] = sessions;
          const current = sessions.some((session) => session.is_current);
          const sessionIds = sessions.filter((session) => !session.is_current).map((session) => session.session_id);
          const label = deviceLabel(latest.user_agent, t("session_unknown_device"));
          return (
            <article
              key={key}
              className={`personal-security-session ${current ? "is-current" : ""}`}
            >
              <span className="personal-security-session__device-mark" aria-hidden="true" />
              <div className="personal-security-session__main">
                <div className="personal-security-session__title">
                  <span>{label}</span>
                  {current && (
                    <span className="personal-security-session__current">{t("session_current_device")}</span>
                  )}
                </div>
                <div className="personal-security-session__meta">
                  {latest.ip_masked !== "unknown" && <span>{t("session_ip")} {latest.ip_masked}</span>}
                  <span>{t("session_last_used")} {formatTime(lastActive(latest), locale)}</span>
                </div>
              </div>
              {sessionIds.length ? (
                <RowActionsMenu
                  label={t("session_more_actions", { device: label })}
                  busy={revoking === key}
                  pending={revoking !== null && revoking !== key}
                  busyLabel={t("session_revoking")}
                  actions={[{
                    id: "revoke",
                    label: t(current ? "session_revoke_others_on_device" : "session_revoke"),
                    danger: true,
                    onSelect: () => setPendingRevoke({ key, current, sessionIds }),
                  }]}
                />
              ) : null}
            </article>
          );
        })}
      </div>
      {pendingRevoke ? (
        <ConfirmDialog
          title={t("revoke_confirm_title")}
          description={t(pendingRevoke.current ? "revoke_confirm_others_on_device" : "revoke_confirm_body")}
          confirmLabel={t("revoke_confirm_action")}
          cancelLabel={t("revoke_confirm_cancel")}
          tone="danger"
          busy={revoking === pendingRevoke.key}
          onCancel={() => setPendingRevoke(null)}
          onConfirm={() => void handleRevoke()}
        />
      ) : null}
    </div>
  );
}
