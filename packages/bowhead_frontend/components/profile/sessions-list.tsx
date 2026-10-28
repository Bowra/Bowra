"use client";

/**
 * Sprint β G10 — Active sessions list with [revoke] per row.
 *
 * is_current badge on current session row.
 * If current_identified=false, hide revoke-all button (P1-S-9 safety).
 */

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


interface Props {
  items: SessionRow[];
  currentIdentified: boolean;
  onRevoked: () => void;
}


function formatTime(iso: string | null, locale: string): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(locale, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}


export function SessionsList({ items, currentIdentified, onRevoked }: Props) {
  const t = useTranslations("profile.security");
  const locale = useLocale();
  const [revoking, setRevoking] = useState<string | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleRevoke = async (sessionId: string) => {
    setRevoking(sessionId);
    setError(null);
    try {
      await revokeSession(sessionId);
      setPendingRevoke(null);
      onRevoked();
    } catch (e) {
      const msg = e instanceof SecurityError ? e.message : String(e);
      setError(msg);
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
        {items.map((s) => (
        <article
          key={s.session_id}
          className={`personal-security-session ${s.is_current ? "is-current" : ""}`}
        >
          <span className="personal-security-session__device-mark" aria-hidden="true" />
          <div className="personal-security-session__main">
            <div className="personal-security-session__title">
              <span>{s.user_agent || t("session_unknown_device")}</span>
              {s.is_current && (
                <span className="personal-security-session__current">{t("session_current_device")}</span>
              )}
            </div>
            <div className="personal-security-session__meta">
              <span>{t("session_ip")} {s.ip_masked}</span>
              <span>{t("session_last_used")} {formatTime(s.last_used_at, locale)}</span>
              <span>{t("session_expires")} {formatTime(s.expires_at, locale)}</span>
            </div>
          </div>
          {s.is_current ? null : (
            <RowActionsMenu
              label={t("session_more_actions", { device: s.user_agent || t("session_unknown_device") })}
              busy={revoking === s.session_id}
              busyLabel={t("session_revoking")}
              actions={[{
                id: "revoke",
                label: t("session_revoke"),
                danger: true,
                disabled: revoking === s.session_id,
                onSelect: () => setPendingRevoke(s.session_id),
              }]}
            />
          )}
        </article>
      ))}
      </div>
      {pendingRevoke ? (
        <ConfirmDialog
          title={t("revoke_confirm_title")}
          description={t("revoke_confirm_body")}
          confirmLabel={t("revoke_confirm_action")}
          cancelLabel={t("revoke_confirm_cancel")}
          tone="danger"
          busy={revoking === pendingRevoke}
          onCancel={() => setPendingRevoke(null)}
          onConfirm={() => void handleRevoke(pendingRevoke)}
        />
      ) : null}
    </div>
  );
}
