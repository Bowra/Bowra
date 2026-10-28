"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { fetchMe, logoutEverywhere, type MeUser } from "@/lib/auth-api";
import { ChangePasswordForm } from "@/components/profile/change-password-form";
import { OAuthOnlyState } from "@/components/profile/oauth-only-state";
import { SessionsList } from "@/components/profile/sessions-list";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { appPath } from "@/lib/base-path";
import { fetchActiveSessions, type SessionsListResp } from "@/lib/security-api";
import { CharIconText } from "@/components/char-icon";

/** Personal security settings: password, signed-in devices, and session control. */
export function SettingsSecurity() {
  const t = useTranslations("settings.profile");
  const securityT = useTranslations("profile.security");
  const [me, setMe] = useState<MeUser | null>(null);
  const [sessions, setSessions] = useState<SessionsListResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmLogoutEverywhere, setConfirmLogoutEverywhere] = useState(false);
  const [passwordExpanded, setPasswordExpanded] = useState(false);
  const passwordPanelId = useId();
  const passwordTriggerRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(() => {
    void fetchMe().then(setMe).catch((cause) => {
      setError(t("load_failed"));
    });
  }, [t]);
  const loadSessions = useCallback(() => {
    void fetchActiveSessions().then(setSessions).catch(() => setSessions(null));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadSessions(); }, [loadSessions]);

  const flash = (nextMessage: string, isError = false) => {
    if (isError) setError(nextMessage); else setMessage(nextMessage);
    if (!isError) setTimeout(() => setMessage(null), 3000);
  };
  const cancelPasswordChange = () => {
    setPasswordExpanded(false);
    window.requestAnimationFrame(() => passwordTriggerRef.current?.focus());
  };
  const handleLogoutEverywhere = async () => {
    setBusy(true);
    try {
      const result = await logoutEverywhere();
      flash(t("logout_done", { count: result.revoked_count }));
      setTimeout(() => { window.location.assign(appPath("/login/")); }, 1200);
    } catch (cause) {
      flash(t("logout_failed"), true);
    } finally {
      setBusy(false);
      setConfirmLogoutEverywhere(false);
    }
  };

  const header = <header className="personal-security-page__header">
    <h1 className="ui-page-title">{securityT("title")}</h1>
    <p className="ui-page-desc">{securityT("subtitle")}</p>
  </header>;
  // Keep the header while loading and replace the content with a skeleton (Page Layout Spec §4).
  if (!me && !error) {
    return <div className="personal-security-page">{header}<LoadingSkeleton variant="form" rows={3} label={securityT("loading")} /></div>;
  }
  if (!me) return <Notice tone="error">{error}</Notice>;

  const oauthOnly = me.has_password === false;
  return (
    <div className="personal-security-page">
      {header}
      {message ? <Notice tone="success">{message}</Notice> : null}
      {error ? <Notice tone="error">{error}</Notice> : null}
      <section className="personal-security-card">
        <div className="personal-security-card__heading">
          <h2>{t("fields.password")}</h2>
          <p>{t("password_description")}</p>
        </div>
        {oauthOnly ? <OAuthOnlyState providers={me.oauth_providers ?? []} /> : (
          <>
            <div className="personal-security-card__action">
              <a href={appPath("/forgot-password/")}><CharIconText ch="→" at="end">{t("reset_password")}</CharIconText></a>
              <button ref={passwordTriggerRef} type="button" className="settings-button settings-button--secondary settings-button--md" aria-expanded={passwordExpanded} aria-controls={passwordPanelId} onClick={() => setPasswordExpanded(true)}>
                {securityT("change_password")}
              </button>
            </div>
            <div id={passwordPanelId} className="personal-security-password-panel profile-security-page" role="region" aria-label={securityT("change_password")} hidden={!passwordExpanded}>
              {passwordExpanded ? <ChangePasswordForm autoFocusCurrent onCancel={cancelPasswordChange} onSuccess={(result) => {
                flash(securityT("change_success", { n: result.other_devices_kicked }));
                setPasswordExpanded(false);
              }} /> : null}
            </div>
          </>
        )}
      </section>
      <section className="personal-security-card">
        <div className="personal-security-card__heading">
          <h2>{securityT("active_sessions")}</h2>
          <p>{securityT("sessions_description")}</p>
        </div>
        {sessions ? <SessionsList items={sessions.items} currentIdentified={sessions.current_identified} onRevoked={loadSessions} /> : <LoadingSkeleton rows={2} label={securityT("loading")} />}
        <div className="personal-security-card__danger">
          <div><h3>{t("fields.logout_everywhere")}</h3><p>{t("logout_hint")}</p></div>
          <Button variant="danger" disabled={busy} onClick={() => setConfirmLogoutEverywhere(true)}>{t("logout_everywhere")}</Button>
        </div>
      </section>
      {confirmLogoutEverywhere ? <ConfirmDialog title={t("logout_confirm_title")} description={t("logout_confirm_desc")} confirmLabel={t("logout_everywhere")} cancelLabel={t("cancel")} busy={busy} onCancel={() => setConfirmLogoutEverywhere(false)} onConfirm={() => void handleLogoutEverywhere()} /> : null}
    </div>
  );
}
