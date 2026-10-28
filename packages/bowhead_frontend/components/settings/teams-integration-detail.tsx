"use client";

import { FormEvent, type ReactNode, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { useAppRouter } from "@/lib/use-app-router";
import { fetchTeamsAdminConfig, TeamsAdminConfig, updateTeamsAdminConfig } from "@/lib/teams-integration-api";

import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./integration-pages.module.css";
import { CharIconText } from "@/components/char-icon";

// Append a global ui-integ-* hook to each class (new appearance in styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-integ");

const empty: TeamsAdminConfig = { configured: false, status: "draft", allowed_scopes: ["personal"] };

// Inline tags used by t.rich: monospace API paths and scope names.
const richTags = { code: (chunks: ReactNode) => <code>{chunks}</code> };

export function TeamsIntegrationDetail() {
  const router = useAppRouter();
  const t = useTranslations("settings.integrations.teams");
  const tCommon = useTranslations("settings.integrations.detail_common");
  const [config, setConfig] = useState<TeamsAdminConfig>(empty);
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [helpOpen, setHelpOpen] = useState(false);

  useEffect(() => { void fetchTeamsAdminConfig().then(setConfig).catch(() => setError(t("load_failed"))); }, [t]);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setSaving(true); setError("");
    const form = new FormData(event.currentTarget);
    try {
      setConfig(await updateTeamsAdminConfig({
        entra_tenant_id: String(form.get("tenant") || ""), microsoft_app_id: String(form.get("app") || ""), app_type: "SingleTenant",
        sso_auth_app_id: String(form.get("ssoApp") || ""), sso_audience: String(form.get("audience") || ""),
        sso_connection_name: String(form.get("connection") || ""), teams_manifest_id: String(form.get("manifest") || ""),
        status: String(form.get("status") || "draft") as TeamsAdminConfig["status"], ...(secret ? { app_secret: secret } : {}),
      })); setSecret("");
    } catch { setError(t("save_failed")); } finally { setSaving(false); }
  };

  return <div className={styles.page}>
    <button className={styles.backButton} type="button" onClick={() => router.push("/team/settings/integrations/")}><CharIconText ch="←">{tCommon("back")}</CharIconText></button>
    <header className={styles.pageHeader}>
      <div><p className={styles.eyebrow}>{t("eyebrow")}</p><h1 className="ui-page-title">Microsoft Teams</h1><p className="ui-page-desc">{t("description")}</p></div>
      <button className={styles.secondaryButton} type="button" onClick={() => setHelpOpen(!helpOpen)}>{helpOpen ? t("help_hide") : t("help_show")}</button>
    </header>
    {helpOpen ? <TeamsHelp /> : null}
    {error ? <div className={`${styles.statusBanner} ${styles.statusBannerWarn}`}><strong>{t("error_title")}</strong><p>{error}</p></div> : null}
    <form className={styles.teamsConfigCard} onSubmit={save}>
      <div className={styles.teamsCardHeader}><div><p className={styles.eyebrow}>{t("connection_eyebrow")}</p><h2>{t("card_title")}</h2><p>{t("card_body")}</p></div><span className={styles.teamsScopeBadge}>{t("scope_badge")}</span></div>
      <div className={styles.teamsFormGrid}>
        <Field label={t("tenant_label")} hint="Azure Entra Directory (tenant) ID"><input required name="tenant" defaultValue={config.entra_tenant_id} /></Field>
        <Field label="Azure Bot App ID" hint="Application (client) ID"><input required name="app" defaultValue={config.microsoft_app_id} /></Field>
        <Field label="Bot client secret" hint={config.app_secret_configured ? tCommon("secret_saved_hint") : t("secret_hint")}><input type="password" value={secret} onChange={(event) => setSecret(event.target.value)} /></Field>
        <Field label="Teams manifest ID" hint={t("manifest_hint")}><input required name="manifest" defaultValue={config.teams_manifest_id} /></Field>
        <Field label="SSO App ID"><input name="ssoApp" defaultValue={config.sso_auth_app_id} /></Field>
        <Field label="SSO audience" hint={t("audience_hint")}><input required name="audience" defaultValue={config.sso_audience} /></Field>
        <Field label="Azure Bot OAuth connection"><input required name="connection" defaultValue={config.sso_connection_name} /></Field>
        <Field label={tCommon("status_label")}><select name="status" defaultValue={config.status}><option value="draft">{tCommon("status_draft")}</option><option value="verified">{t("status_verified")}</option><option value="enabled">{tCommon("status_enabled")}</option><option value="disabled">{tCommon("status_disabled")}</option></select></Field>
      </div>
      <div className={styles.teamsFormFooter}><p>{t("footer")}</p><button className={styles.primaryButton} disabled={saving} type="submit">{saving ? tCommon("saving") : tCommon("save")}</button></div>
    </form>
  </div>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className={styles.teamsField}><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label>; }

function TeamsHelp() { const t = useTranslations("settings.integrations.teams"); return <section className={styles.teamsHelp}><div><p className={styles.eyebrow}>{t("help_eyebrow")}</p><h2>{t("help_title")}</h2></div><div className={styles.teamsHelpGrid}>
  <article><h3>{t("prep_title")}</h3><ul><li>{t("prep_1")}</li><li>{t("prep_2")}</li><li>{t("prep_3")}</li></ul></article>
  <article><h3>{t("azure_title")}</h3><ol><li>{t("azure_1")}</li><li>{t("azure_2")}</li><li>{t.rich("azure_3", richTags)}</li><li>{t.rich("azure_4", richTags)}</li></ol></article>
  <article><h3>{t("publish_title")}</h3><ol><li>{t.rich("publish_1", richTags)}</li><li>{t("publish_2")}</li><li>{t("publish_3")}</li><li>{t("publish_4")}</li></ol></article>
</div><p className={styles.teamsHelpNote}>{t("help_warning")}</p></section>; }
