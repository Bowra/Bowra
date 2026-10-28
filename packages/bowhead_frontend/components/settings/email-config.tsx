"use client";

import { PRODUCT_NAME } from "@/lib/product-identity";

// Team email configuration (local and SaaS). Supports three-state status, saving, test send, and clearing.

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { fetchMe } from "@/lib/auth-api";
import { ApiError } from "@/lib/api-client";
import {
  deleteEmailConfig,
  getEmailConfig,
  putEmailConfig,
  sendTestEmail,
  type EmailConfigResponse,
  type EmailTestResult,
} from "@/lib/email-config-api";
import {
  emailConfigErrorKeys,
  emailTestErrorKey,
} from "@/lib/email-config-error";
import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./email-service.module.css";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Field } from "@/components/ui/field";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";

// Append global ui-email-* hooks to classes (styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-email");

type Draft = {
  host: string;
  port: number;
  user: string;
  password: string;
  from_addr: string;
  from_name: string;
  tls: boolean;
};

type EncryptionMode = "ssl" | "starttls" | "plain";

const EMPTY: Draft = {
  host: "", port: 587, user: "", password: "",
  from_addr: "", from_name: "", tls: true,
};

function encryptionMode(draft: Draft): EncryptionMode {
  if (draft.port === 465) return "ssl";
  return draft.tls ? "starttls" : "plain";
}

function applyEncryptionMode(mode: EncryptionMode, draft: Draft): Draft {
  if (mode === "ssl") return { ...draft, port: 465, tls: true };
  if (mode === "starttls") return { ...draft, port: 587, tls: true };
  return { ...draft, port: 25, tls: false };
}

function applyPort(port: number, draft: Draft): Draft {
  if (port === 465 || port === 587) return { ...draft, port, tls: true };
  if (port === 25) return { ...draft, port, tls: false };
  return { ...draft, port };
}

export function SettingsEmailSection() {
  const t = useTranslations("settings.integrations");
  const userFacingError = (cause: unknown): string =>
    emailConfigErrorKeys(cause)
      .map((key) => t(key))
      .join(t("email_error_separator"));
  const [resp, setResp] = useState<EmailConfigResponse | null>(null);
  // null = confirming identity (loading skeleton).
  const [isAdmin, setIsAdmin] = useState<boolean | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [pwTouched, setPwTouched] = useState(false);
  const [fromTouched, setFromTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<EmailTestResult | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  // review-fix(P2): distinguish 403/404 (unauthorized/flag off → hidden) from transient errors (show retry),
  // or one genuine admin 5xx/timeout permanently hides whole section silently.
  const [loadFailed, setLoadFailed] = useState(false);

  // isRefresh=true (refetch after save/clear): failure **retains** previous resp, never clears entire form
  // (review-fix: successful save then GET 502 made form disappear, misleadingly suggesting save failure).
  const load = useCallback(async (isRefresh = false) => {
    try {
      const r = await getEmailConfig();
      setResp(r);
      setLoadFailed(false);
      if (r.config) {
        setDraft({
          host: r.config.host, port: r.config.port, user: r.config.user,
          password: "", from_addr: r.config.from_addr,
          from_name: r.config.from_name, tls: r.config.tls,
        });
        setPwTouched(false);
        setFromTouched(false);
      }
    } catch (e) {
      if (isRefresh) return; // Retain old resp + form; mutation success/failure already reported separately.
      setResp(null);
      // 403/404 = nonadmin/flag off → silently hide; others (transient 5xx/network) → show retry.
      const status = e instanceof ApiError ? e.status : 0;
      setLoadFailed(status !== 403 && status !== 404);
    }
  }, []);

  useEffect(() => {
    // review-fix(P1): backend is_team_admin covers only creator; promoted team_admin (team_members role)
    // also passes backend gate but would lose section under is_team_admin. Same broad check as appearance.tsx.
    void fetchMe()
      .then((u) => setIsAdmin(Boolean(
        u.is_team_admin || u.team_role === "owner" || u.team_role === "team_admin",
      )))
      .catch(() => setIsAdmin(false));
  }, []);
  useEffect(() => {
    if (isAdmin) void load();
  }, [isAdmin, load]);

  // Page-acceptance finish (A gaps): skeleton during identity/initial load.
  if (isAdmin === null) return <LoadingSkeleton variant="form" rows={4} />;
  if (!isAdmin) return null;
  // Transient load failure: give genuine admin a retry entry, rather than silently hiding.
  if (!resp) {
    if (!loadFailed) return <LoadingSkeleton variant="form" rows={4} />;
    return (
      <section className="legacy-email-load-failed ui-email-load-failed">
        <Notice tone="error" className="legacy-notice--mb10">{t("email_load_failed")}</Notice>
        <Button size="sm" onClick={() => void load()}>{t("email_retry")}</Button>
      </section>
    );
  }

  const tone: "good" | "warn" | "muted" =
    resp.configured === "team" ? "good" : resp.configured === "env" ? "warn" : "muted";
  const statusLabel =
    resp.configured === "team" ? t("email_status_team")
      : resp.configured === "env" ? t("email_status_env")
        : t("email_status_none");

  const validateDraft = () => {
    const hasPassword = pwTouched ? Boolean(draft.password.trim()) : Boolean(resp.config?.password_set);
    if (encryptionMode(draft) === "plain" && (draft.user.trim() || hasPassword)) {
      return t("email_plain_auth_error");
    }
    return null;
  };

  const updatePayload = () => ({
    host: draft.host, port: draft.port, user: draft.user,
    password: pwTouched ? draft.password : "",
    from_addr: draft.from_addr, from_name: draft.from_name,
    tls: draft.port === 465 ? true : draft.tls,
  });

  const onSave = async () => {
    setError(null);
    setSaved(false);
    setTestResult(null);
    const validationError = validateDraft();
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    try {
      await putEmailConfig(updatePayload());
      setSaved(true);
      await load(true);
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setSaving(false);
    }
  };

  const onSaveAndTest = async () => {
    setError(null);
    setSaved(false);
    setTestResult(null);
    const validationError = validateDraft();
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    setTesting(true);
    try {
      await putEmailConfig(updatePayload());
      setSaved(true);
      await load(true);
    } catch (e) {
      setError(userFacingError(e));
      setSaving(false);
      setTesting(false);
      return;
    }
    try {
      setTestResult(await sendTestEmail());
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setSaving(false);
      setTesting(false);
    }
  };

  const onTest = async () => {
    setTesting(true);
    setTestResult(null);
    setError(null);
    try {
      setTestResult(await sendTestEmail());
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setTesting(false);
    }
  };

  const onClear = async () => {
    setSaving(true);
    setError(null);
    try {
      await deleteEmailConfig();
      setDraft(EMPTY);
      setPwTouched(false);
      setFromTouched(false);
      setSaved(false);
      setTestResult(null);
      await load(true);
    } catch (e) {
      setError(userFacingError(e));
    } finally {
      setSaving(false);
    }
  };

  const applyFeishuPreset = () => {
    setFromTouched(false);
    setDraft((d) => ({
      ...d,
      host: "smtp.feishu.cn",
      port: 465,
      tls: true,
      from_name: d.from_name || PRODUCT_NAME,
      user: d.user || d.from_addr,
      from_addr: d.from_addr || d.user,
    }));
  };

  return (
    <section className={styles.formCard}>
      <div className={styles.statusRow}>
        <div>
          <p className={styles.statusTitle}>{t("email_effective_title")}</p>
          <p className={styles.statusDescription}>{t("email_effective_description")}</p>
        </div>
        <span className={`${styles.badge} ${tone === "good" ? styles.badgeGood : tone === "warn" ? styles.badgeWarn : styles.badgeMuted}`}>
          {statusLabel}
        </span>
      </div>

      {resp.configured === "none" ? (
        <Notice tone="muted" className="legacy-notice--mt12">
          {t("email_none_warning")}
        </Notice>
      ) : null}
      {resp.configured === "env" ? (
        <Notice tone="muted" className="legacy-notice--mt12">
          {t("email_env_notice")}
        </Notice>
      ) : null}

      <div className={styles.form}>
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>{t("email_connection_title")}</h2>
            <p>{t("email_connection_description")}</p>
          </div>
          <Field id="smtp-host" label={t("email_field_host")}>
            <input id="smtp-host" name="smtp-host" className="ui-input legacy-input-email" value={draft.host}
              autoComplete="off" spellCheck={false}
              placeholder="smtp.feishu.cn"
              onChange={(e) => setDraft({ ...draft, host: e.target.value })} />
            <div className={styles.preset}>
              <button type="button" onClick={applyFeishuPreset}>
                {t("email_preset_feishu")}
              </button>
            </div>
          </Field>
          <div className={styles.twoColumn}>
            <Field id="smtp-port" label={t("email_field_port")} hint={t("email_field_port_hint")}>
              <input id="smtp-port" name="smtp-port" type="number" inputMode="numeric"
                min={1} max={65535} autoComplete="off" className="ui-input legacy-input-email" value={draft.port}
                onChange={(e) => setDraft((d) => applyPort(Number(e.target.value) || 0, d))} />
            </Field>
            <Field id="smtp-encryption" label={t("email_field_encryption")}>
              <select id="smtp-encryption" name="smtp-encryption" className="ui-input legacy-input-email legacy-input-email--select"
                autoComplete="off"
                value={encryptionMode(draft)}
                onChange={(e) => setDraft((d) => applyEncryptionMode(e.target.value as EncryptionMode, d))}>
                <option value="ssl">{t("email_encryption_ssl")}</option>
                <option value="starttls">{t("email_encryption_starttls")}</option>
                <option value="plain">{t("email_encryption_plain")}</option>
              </select>
            </Field>
          </div>
          {encryptionMode(draft) === "plain" ? (
            <Notice tone="muted">
              {t("email_plain_warning")}
            </Notice>
          ) : null}
        </div>
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>{t("email_authentication_title")}</h2>
            <p>{t("email_authentication_description")}</p>
          </div>
          <Field id="smtp-user" label={t("email_field_user")}>
            <input id="smtp-user" name="smtp-user" className="ui-input legacy-input-email" value={draft.user}
              autoComplete="username" spellCheck={false}
              placeholder="bowhead@company.com"
              onChange={(e) => {
                const value = e.target.value;
                setDraft((d) => ({
                  ...d,
                  user: value,
                  from_addr: !fromTouched && (!d.from_addr || d.from_addr === d.user) ? value : d.from_addr,
                }));
              }} />
          </Field>
          <Field id="smtp-pass" label={t("email_field_password")}
            hint={resp.config?.password_set ? t("email_field_password_keep") : t("email_field_password_hint")}>
            <input id="smtp-pass" name="smtp-pass" type="password" className="ui-input legacy-input-email"
              autoComplete="new-password" spellCheck={false}
              value={pwTouched ? draft.password : ""}
              placeholder={resp.config?.password_set ? "••••••••" : ""}
              onChange={(e) => { setPwTouched(true); setDraft({ ...draft, password: e.target.value }); }} />
          </Field>
        </div>
        <div className={styles.section}>
          <div className={styles.sectionHeader}>
            <h2>{t("email_sender_title")}</h2>
            <p>{t("email_sender_description")}</p>
          </div>
          <div className={styles.twoColumn}>
            <Field id="smtp-from" label={t("email_field_from")} hint={t("email_field_from_hint")}>
              <input id="smtp-from" name="smtp-from" type="email" className="ui-input legacy-input-email"
                autoComplete="email" spellCheck={false} value={draft.from_addr}
                placeholder="no-reply@company.com"
                onChange={(e) => {
                  const value = e.target.value;
                  setFromTouched(true);
                  setDraft((d) => ({
                    ...d,
                    from_addr: value,
                  }));
                }} />
            </Field>
            <Field id="smtp-fromname" label={t("email_field_from_name")}>
              <input id="smtp-fromname" name="smtp-fromname" className="ui-input legacy-input-email"
                autoComplete="organization" value={draft.from_name}
                onChange={(e) => setDraft({ ...draft, from_name: e.target.value })} />
            </Field>
          </div>
        </div>
        <div className={styles.section}>
          {error ? <Notice tone="error">{error}</Notice> : null}
          {saved ? <Notice tone="success">{t("email_saved")}</Notice> : null}
          {testResult ? (
            <Notice tone={testResult.ok ? "success" : "error"}>
              {testResult.ok
                ? t("email_test_ok")
                : t(emailTestErrorKey(testResult.error_code))}
            </Notice>
          ) : null}

          <div className={styles.actions}>
            <Button variant="primary" onClick={() => void onSave()} disabled={saving || testing}>
              {saving ? t("email_saving") : t("email_save")}
            </Button>
            <Button onClick={() => void onSaveAndTest()} disabled={saving || testing}>
              {saving && testing ? t("email_testing") : t("email_save_and_test")}
            </Button>
            <Button onClick={() => void onTest()} disabled={testing || resp.configured === "none"}>
              {testing ? t("email_testing") : t("email_send_test")}
            </Button>
            {resp.configured === "team" ? (
              <Button variant="danger" onClick={() => setConfirmClear(true)} disabled={saving}>
                {t("email_clear")}
              </Button>
            ) : null}
            <p className={styles.actionHint}>{t("email_test_hint")}</p>
          </div>
        </div>
      </div>
      {confirmClear ? (
        <ConfirmDialog
          title={t("email_clear_confirm_title")}
          description={t("email_clear_confirm_description")}
          confirmLabel={t("email_clear_confirm")}
          cancelLabel={t("cancel")}
          busy={saving}
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => {
            setConfirmClear(false);
            void onClear();
          }}
        />
      ) : null}
    </section>
  );
}
