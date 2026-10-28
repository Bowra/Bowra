"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Check, Copy } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Switch } from "@/components/ui/switch";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { copyTextWithFallback } from "@/lib/clipboard";
import {
  GOOGLE_BUNDLE_IDS,
  beginGoogleAuthorization,
  clearGoogleCallbackQuery,
  deleteGoogleConnection,
  fetchGoogleConnection,
  googleCallbackNotice,
  saveGoogleConnection,
  setGoogleConnectionEnabled,
  type GoogleApiError,
  type GoogleBundleId,
  type GoogleCallbackNotice,
  type GoogleConnection,
  type GoogleConnectionState,
} from "@/lib/google-workspace-api";

import styles from "../integration-pages.module.css";

type Form = {
  name: string;
  client_id: string;
  client_secret: string;
  domain: string;
  bundles: Record<GoogleBundleId, boolean>;
  picker_api_key: string;
  /** Explicitly remove the stored Picker API key (the input itself is write-only). */
  clear_picker_api_key: boolean;
  picker_project_number: string;
};

const DEFAULT_BUNDLES: Record<GoogleBundleId, boolean> = { base: true, read_by_link: true, edit_docs: true, edit_sheets: true, calendar: false };

function formFrom(connection: GoogleConnection | null): Form {
  if (!connection) {
    return {
      name: "Google Workspace",
      client_id: "",
      client_secret: "",
      domain: "",
      bundles: { ...DEFAULT_BUNDLES },
      picker_api_key: "",
      clear_picker_api_key: false,
      picker_project_number: "",
    };
  }
  const bundles = { ...DEFAULT_BUNDLES };
  for (const bundle of connection.bundles) bundles[bundle.id] = bundle.enabled;
  return {
    name: connection.name,
    client_id: connection.client_id,
    client_secret: "",
    domain: connection.domain,
    bundles,
    picker_api_key: "",
    clear_picker_api_key: false,
    picker_project_number: connection.picker_project_number,
  };
}

/** Google mark: four-colour "G" drawn inline (no external asset). */
export function GoogleMark() {
  return (
    <svg width="22" height="22" viewBox="0 0 48 48" aria-hidden="true" className="legacy-ai-tools-google-mark">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const t = useTranslations("google_workspace");
  const [copied, setCopied] = useState(false);
  return (
    <div className={styles.callbackRow}>
      <span>{label}</span>
      <code>{value}</code>
      <button
        className={`${styles.copyButton} ${styles.callbackCopyButton}`}
        type="button"
        aria-label={copied ? t("copied_value", { label }) : t("copy_value", { label })}
        onClick={() => void copyTextWithFallback(value, t("copy_fallback_prompt")).then((ok) => setCopied(Boolean(ok)))}
      >
        {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      </button>
    </div>
  );
}

/** Callback notice copy key for a `?google=…` return. */
export function googleCallbackMessageKey(notice: GoogleCallbackNotice): string {
  return notice.result === "connected" ? "callback.connected" : `callback.${notice.reason}`;
}

/**
 * Google Workspace on the Tools page, next to Power BI and Tavily.
 *
 * Owners and team admins register the customer's own OAuth client, choose capability
 * bundles and authorize with their own account before enabling. Hidden entirely while
 * the deployment's Google Workspace switch is off.
 */
type GoogleWorkspaceToolSectionProps = { createRequest: number; onAvailableChange?: (available: boolean, configured: boolean) => void };

/**
 * The `google_workspace` capability from /me decides availability before any request, so a
 * deployment with the feature switched off never calls `/api/v1/google/connection` (a 404).
 */
export function GoogleWorkspaceToolSection(props: GoogleWorkspaceToolSectionProps) {
  const available = useOptionalAuthSession()?.me?.capabilities?.google_workspace === true;
  return available ? <GoogleWorkspaceToolSectionBody {...props} /> : null;
}

function GoogleWorkspaceToolSectionBody({ createRequest, onAvailableChange }: GoogleWorkspaceToolSectionProps) {
  const t = useTranslations("google_workspace");
  const [state, setState] = useState<GoogleConnectionState | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<Form>(formFrom(null));
  const [confirmClientChange, setConfirmClientChange] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<GoogleCallbackNotice | null>(null);

  function failureMessage(cause: unknown, fallback: "change" | "authorize"): string {
    const code = (cause as GoogleApiError | null)?.code;
    if (code === "google_admin_authorization_required") return t("enable_requires_authorization");
    if (code === "google_public_entry_required") return t("public_entry_required");
    if (code && ["google_domain_invalid", "google_picker_project_number_invalid", "google_bundle_unknown", "google_client_secret_required", "google_connection_incomplete"].includes(code)) {
      return t("errors.invalid_config");
    }
    return t(`errors.${fallback}`);
  }

  const load = useCallback(async (mutationSucceeded = false) => {
    try {
      const next = await fetchGoogleConnection();
      setState(next);
      setError("");
      onAvailableChange?.(next !== null, Boolean(next?.connection));
    } catch (cause) {
      if (!mutationSucceeded) {
        setState(null);
        onAvailableChange?.(false, false);
      }
      setError(t(mutationSucceeded ? "errors.saved_refresh_failed" : "errors.load"));
    }
  }, [onAvailableChange, t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const callback = googleCallbackNotice(window.location.search);
    if (!callback) return;
    setNotice(callback);
    clearGoogleCallbackQuery();
  }, []);

  useEffect(() => {
    if (createRequest > 0) {
      setForm(formFrom(state?.connection ?? null));
      setError("");
      setEditing(true);
    }
    // Opening is driven by the request counter only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createRequest]);

  // Hidden only when the feature is off; a failed load must still show its error.
  if (!state && !notice && !error) return null;
  const connection = state?.connection ?? null;
  const redirectUri = connection ? connection.redirect_uri : (state?.redirect_uri ?? null);
  const javascriptOrigin = connection ? connection.javascript_origin : (state?.javascript_origin ?? null);
  // The consent-screen list follows the bundles currently ticked in the form, not the saved ones.
  const scopes = [
    ...new Set((connection?.bundles ?? state?.bundles ?? []).filter((bundle) => form.bundles[bundle.id]).flatMap((bundle) => bundle.scopes)),
  ];

  function openEdit() {
    setForm(formFrom(connection));
    setError("");
    setEditing(true);
  }

  function clientChanged(): boolean {
    if (!connection) return false;
    return form.client_id.trim() !== connection.client_id || form.client_secret.trim() !== "";
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      const saved = await saveGoogleConnection({
        name: form.name.trim() || "Google Workspace",
        client_id: form.client_id.trim(),
        // Write-only secrets: an empty input keeps the stored value.
        ...(form.client_secret.trim() ? { client_secret: form.client_secret.trim() } : {}),
        domain: form.domain.trim(),
        bundles: form.bundles,
        // Write-only: empty keeps the stored key; an explicit clear sends an empty string.
        ...(form.picker_api_key.trim() ? { picker_api_key: form.picker_api_key.trim() } : form.clear_picker_api_key ? { picker_api_key: "" } : {}),
        picker_project_number: form.picker_project_number.trim(),
      });
      setState((current) => ({ ...current, connection: saved }));
      setEditing(false);
      setConfirmClientChange(false);
      await load(true);
    } catch (cause) {
      setConfirmClientChange(false);
      setError(failureMessage(cause, "change"));
    } finally {
      setBusy(false);
    }
  }

  function requestSave() {
    if (clientChanged()) setConfirmClientChange(true);
    else void save();
  }

  async function authorize() {
    setBusy(true);
    setError("");
    try {
      window.location.assign(await beginGoogleAuthorization());
    } catch (cause) {
      setBusy(false);
      setError(failureMessage(cause, "authorize"));
    }
  }

  async function toggle() {
    if (!connection) return;
    setBusy(true);
    setError("");
    try {
      const saved = await setGoogleConnectionEnabled(connection.status !== "enabled");
      setState((current) => ({ ...current, connection: saved }));
      await load(true);
    } catch (cause) {
      setError(failureMessage(cause, "change"));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await deleteGoogleConnection();
      setState((current) => ({ ...current, connection: null }));
      setRemoving(false);
      await load(true);
    } catch (cause) {
      setError(failureMessage(cause, "change"));
    } finally {
      setBusy(false);
    }
  }

  const required = !form.client_id.trim() || !form.domain.trim() || (!connection?.client_secret_configured && !form.client_secret.trim());
  const statusLabel = !connection ? t("status_not_configured") : connection.status === "enabled" ? t("status_enabled") : t("status_configured");

  return (
    <>
      {notice ? (
        <Notice tone={notice.result === "connected" ? "success" : "error"}>{t(googleCallbackMessageKey(notice))}</Notice>
      ) : null}
      {connection ? (
        <div
          className={`legacy-ai-tools-builtin ai-tools-builtin legacy-ai-tools-builtin--wrap${connection.status === "enabled" ? "" : " is-off"}`}
          data-testid="google-workspace-tool"
        >
          <GoogleMark />
          <div className="legacy-ai-flex1 legacy-ai-min0">
            <strong>{connection.name}</strong>
            <div className="legacy-ai-tools-builtin-desc ai-tools-builtin-desc">
              {statusLabel} · {connection.domain} · {t("authorized_members", { count: connection.authorized_members })}
            </div>
          </div>
          <RowActionsMenu
            label={t("more_actions", { name: connection.name })}
            actions={[
              { id: "edit", label: t("edit"), onSelect: openEdit },
              { id: "authorize", label: t("authorize_self"), onSelect: () => void authorize() },
              { id: "delete", label: t("delete"), danger: true, onSelect: () => setRemoving(true) },
            ]}
          />
          <Switch
            checked={connection.status === "enabled"}
            aria-label={t("toggle", { name: connection.name })}
            busy={busy}
            onCheckedChange={() => void toggle()}
            className={`legacy-switch-tools${connection.status === "enabled" ? " is-on" : ""}${busy ? " is-inert" : ""}`}
          >
            <span className={`legacy-switch-tools-knob${connection.status === "enabled" ? " is-on" : ""}`} />
          </Switch>
        </div>
      ) : null}
      {error && !editing ? <Notice tone="error">{error}</Notice> : null}
      {editing ? (
        <Dialog title={connection ? t("edit_title") : t("add_title")} onClose={() => setEditing(false)}>
          <p>{t("intro")}</p>
          <h3>{t("step_client")}</h3>
          <p>{t("step_client_help")}</p>
          {redirectUri && javascriptOrigin ? (
            <div className={styles.callbackList}>
              <CopyRow label={t("redirect_uri")} value={redirectUri} />
              <CopyRow label={t("javascript_origin")} value={javascriptOrigin} />
            </div>
          ) : (
            <Notice tone="info">{t("public_entry_required")}</Notice>
          )}
          <Field id="google-name" label={t("connection_name")}>
            <input id="google-name" className="ui-input" value={form.name} maxLength={60} onChange={(event) => setForm({ ...form, name: event.target.value })} />
          </Field>
          <Field id="google-client-id" label={t("client_id")}>
            <input id="google-client-id" className="ui-input" value={form.client_id} onChange={(event) => setForm({ ...form, client_id: event.target.value })} autoComplete="off" />
          </Field>
          <Field id="google-client-secret" label={t("client_secret")}>
            <input
              id="google-client-secret"
              className="ui-input"
              type="password"
              autoComplete="new-password"
              value={form.client_secret}
              placeholder={connection?.client_secret_configured ? t("secret_configured_placeholder") : ""}
              onChange={(event) => setForm({ ...form, client_secret: event.target.value })}
            />
          </Field>
          <Field id="google-domain" label={t("domain")}>
            <input id="google-domain" className="ui-input" value={form.domain} placeholder="example.com" onChange={(event) => setForm({ ...form, domain: event.target.value })} />
          </Field>
          <p className="legacy-tools-pbi-note">{t("domain_help")}</p>
          <h3>{t("step_bundles")}</h3>
          <div role="group" aria-label={t("step_bundles")}>
            {GOOGLE_BUNDLE_IDS.map((id) => (
              <label key={id} className="legacy-tools-google-bundle">
                <input
                  type="checkbox"
                  checked={form.bundles[id]}
                  disabled={id === "base"}
                  onChange={(event) => setForm({ ...form, bundles: { ...form.bundles, [id]: event.target.checked } })}
                />
                <span>
                  <strong>{t(`bundle.${id}.label`)}</strong>
                  <span className="legacy-tools-pbi-note">{t(`bundle.${id}.help`)}</span>
                </span>
              </label>
            ))}
          </div>
          <h3>{t("step_scopes")}</h3>
          <p>{t("step_scopes_help")}</p>
          <ul className="legacy-tools-google-scopes">
            {scopes.map((scope) => (
              <li key={scope}><code>{scope}</code></li>
            ))}
          </ul>
          <h3>{t("step_picker")}</h3>
          <Field id="google-picker-key" label={t("picker_api_key")}>
            <input
              id="google-picker-key"
              className="ui-input"
              type="password"
              autoComplete="new-password"
              value={form.picker_api_key}
              placeholder={connection?.picker_api_key_configured ? t("secret_configured_placeholder") : ""}
              onChange={(event) => setForm({ ...form, picker_api_key: event.target.value, clear_picker_api_key: false })}
            />
          </Field>
          {connection?.picker_api_key_configured && !form.picker_api_key.trim() ? (
            <label className="legacy-tools-google-bundle">
              <input
                type="checkbox"
                checked={form.clear_picker_api_key}
                onChange={(event) => setForm({ ...form, clear_picker_api_key: event.target.checked })}
              />
              <span>{t("picker_api_key_clear")}</span>
            </label>
          ) : null}
          <Field id="google-picker-project" label={t("picker_project_number")}>
            <input id="google-picker-project" className="ui-input" inputMode="numeric" value={form.picker_project_number} onChange={(event) => setForm({ ...form, picker_project_number: event.target.value })} />
          </Field>
          <p className="legacy-tools-pbi-note">{t("picker_help")}</p>
          <p className="legacy-tools-pbi-note">{t("after_save_help")}</p>
          {error ? <Notice tone="error">{error}</Notice> : null}
          <div className="legacy-tools-pbi-actions">
            <Button variant="secondary" onClick={() => setEditing(false)}>{t("cancel")}</Button>
            <Button variant="primary" disabled={busy || required} onClick={requestSave}>{t("save")}</Button>
          </div>
        </Dialog>
      ) : null}
      {confirmClientChange ? (
        <ConfirmDialog
          title={t("client_change_title")}
          description={t("client_change_description")}
          confirmLabel={t("client_change_confirm")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setConfirmClientChange(false)}
          onConfirm={() => void save()}
        />
      ) : null}
      {removing && connection ? (
        <ConfirmDialog
          title={t("delete_title", { name: connection.name })}
          description={t("delete_description")}
          confirmLabel={t("delete")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setRemoving(false)}
          onConfirm={() => void remove()}
        />
      ) : null}
    </>
  );
}
