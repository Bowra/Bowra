"use client";

import { useCallback, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { GoogleMark, googleCallbackMessageKey } from "@/components/settings/tools/google-workspace-tool-section";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Notice } from "@/components/ui/notice";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import {
  beginGoogleAuthorization,
  clearGoogleCallbackQuery,
  disconnectGoogle,
  fetchMyGoogleAuthorization,
  googleCallbackNotice,
  type GoogleAuthorization,
  type GoogleCallbackNotice,
} from "@/lib/google-workspace-api";

function formatDate(value: string | null, locale: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * The member's own Google authorization, shown in My connections.
 *
 * Gated by the team's Google connection, independently of the personal MCP license:
 * renders nothing unless Google Workspace is available to this member's team. The
 * `google_workspace` capability from /me decides this before any request, so a
 * deployment with the feature switched off never calls `/api/v1/google/me` (a 404).
 */
export function GoogleConnectionCard() {
  const available = useOptionalAuthSession()?.me?.capabilities?.google_workspace === true;
  return available ? <GoogleConnectionCardBody /> : null;
}

function GoogleConnectionCardBody() {
  const t = useTranslations("google_workspace");
  const locale = useLocale();
  const [authorization, setAuthorization] = useState<GoogleAuthorization | null>(null);
  const [notice, setNotice] = useState<GoogleCallbackNotice | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const result = await fetchMyGoogleAuthorization();
    setAuthorization(result?.available ? result.authorization : null);
  }, []);

  useEffect(() => {
    // A failed load is shown, not swallowed: an empty card would look like "Google not available".
    void load().catch((cause: unknown) => {
      setAuthorization(null);
      setError(cause instanceof Error ? cause.message : String(cause));
    });
  }, [load]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const callback = googleCallbackNotice(window.location.search);
    if (!callback) return;
    setNotice(callback);
    clearGoogleCallbackQuery();
  }, []);

  if (!authorization) {
    if (!notice && !error) return null;
    return (
      <>
        {notice ? <Notice tone={notice.result === "connected" ? "success" : "error"}>{t(googleCallbackMessageKey(notice))}</Notice> : null}
        {error ? <Notice tone="error">{t("load_failed")}</Notice> : null}
      </>
    );
  }

  async function connect() {
    setBusy(true);
    setError("");
    try {
      window.location.assign(await beginGoogleAuthorization());
    } catch (cause) {
      setBusy(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function disconnect() {
    setBusy(true);
    setError("");
    try {
      await disconnectGoogle();
      setDisconnecting(false);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const state =
    authorization.status === "not_connected"
      ? "not_connected"
      : authorization.status === "needs_reauth"
        ? "needs_reauth"
        : authorization.needs_update
          ? "needs_update"
          : "connected";
  const granted = authorization.bundles.filter((bundle) => bundle.enabled && bundle.granted).map((bundle) => t(`bundle.${bundle.id}.label`));
  const lastUsed = formatDate(authorization.last_used_at, locale);

  return (
    <section className="personal-mcp-panel google-connection-card" aria-labelledby="google-connection-title" data-testid="google-connection-card">
      {notice ? <Notice tone={notice.result === "connected" ? "success" : "error"}>{t(googleCallbackMessageKey(notice))}</Notice> : null}
      <div className="google-connection-row">
        <GoogleMark />
        <div className="legacy-ai-flex1 legacy-ai-min0">
          <h2 id="google-connection-title">{t("member_title")}</h2>
          <p className="ui-page-desc">{t(`member_state.${state}`)}</p>
          {state !== "not_connected" && authorization.email ? <p>{t("member_email", { email: authorization.email })}</p> : null}
          {state === "connected" || state === "needs_update" ? (
            <p>{t("member_granted", { capabilities: granted.join(t("list_separator")) })}</p>
          ) : null}
          {lastUsed && state !== "not_connected" ? <p className="legacy-tools-pbi-note">{t("member_last_used", { time: lastUsed })}</p> : null}
        </div>
        <div className="google-connection-actions">
          {state === "not_connected" ? <Button variant="primary" disabled={busy} onClick={() => void connect()}>{t("member_connect")}</Button> : null}
          {state === "needs_reauth" ? <Button variant="primary" disabled={busy} onClick={() => void connect()}>{t("member_reconnect")}</Button> : null}
          {state === "needs_update" ? <Button variant="primary" disabled={busy} onClick={() => void connect()}>{t("member_update")}</Button> : null}
          {state !== "not_connected" ? <Button variant="secondary" disabled={busy} onClick={() => setDisconnecting(true)}>{t("member_disconnect")}</Button> : null}
        </div>
      </div>
      {error ? <Notice tone="error">{error}</Notice> : null}
      {disconnecting ? (
        <ConfirmDialog
          title={t("member_disconnect_title")}
          description={t("member_disconnect_description")}
          confirmLabel={t("member_disconnect")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setDisconnecting(false)}
          onConfirm={() => void disconnect()}
        />
      ) : null}
    </section>
  );
}
