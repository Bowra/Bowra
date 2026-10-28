"use client";

import { FormEvent, type ReactNode, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Check, Copy } from "@/components/icons";
import { ApiError } from "@/lib/api-client";
import { copyTextWithFallback } from "@/lib/clipboard";
import { loadDeploymentFacts } from "@/lib/settings-nav-counts";
import { useAppRouter } from "@/lib/use-app-router";
import { beginSlackInstall, fetchSlackAdminConfig, type SlackAdminConfig, updateSlackAdminConfig } from "@/lib/slack-integration-api";

import { withUiHooks } from "./ui-hook-classes";
import rawStyles from "./integration-pages.module.css";
import { CharIcon, CharIconText } from "@/components/char-icon";

// Append a global ui-integ-* hook to each class (new appearance in styles/ui-refresh-pages-settings.css).
const styles = withUiHooks(rawStyles, "ui-integ");

const empty: SlackAdminConfig = { configured: false, status: "draft", channel_answers_enabled: false };

// Inline tags used by t.rich: bold Slack-console button names, monospace code values.
const richTags = {
  strong: (chunks: ReactNode) => <strong>{chunks}</strong>,
  code: (chunks: ReactNode) => <code>{chunks}</code>,
};

export function SlackIntegrationDetail() {
  const router = useAppRouter();
  const t = useTranslations("settings.integrations.slack");
  const tCommon = useTranslations("settings.integrations.detail_common");
  const [config, setConfig] = useState<SlackAdminConfig>(empty);
  const [clientSecret, setClientSecret] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const leave = () => router.replace("/team/settings/integrations/");
    // Slack is offered only in private deployments. Check the deployment kind first so other
    // deployments never request the admin config (which answers 404 slack_local_only there).
    void loadDeploymentFacts()
      .then((facts) => {
        if (cancelled) return null;
        if (facts && !facts.local) {
          leave();
          return null;
        }
        return fetchSlackAdminConfig();
      })
      .then((next) => {
        if (cancelled || !next) return;
        setConfig(next);
        setLoading(false);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof ApiError && reason.status === 404) {
          leave();
          return;
        }
        setError(t("load_failed"));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [router, t]);

  if (loading) return null;

  const installed = config.status === "verified" || config.status === "enabled";

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setSaving(true); setError("");
    const form = new FormData(event.currentTarget);
    try {
      const next = await updateSlackAdminConfig({
        client_id: String(form.get("clientId") || ""),
        channel_answers_enabled: form.get("channels") === "on",
        status: String(form.get("status") || "draft") as SlackAdminConfig["status"],
        ...(clientSecret ? { client_secret: clientSecret } : {}),
        ...(signingSecret ? { signing_secret: signingSecret } : {}),
      });
      setConfig(next); setClientSecret(""); setSigningSecret("");
    } catch { setError(t("save_failed")); } finally { setSaving(false); }
  };
  const install = async () => {
    setInstalling(true); setError("");
    try { window.location.assign((await beginSlackInstall()).install_url); }
    catch { setError(t("install_failed")); setInstalling(false); }
  };

  return <div className={styles.page}>
    <button className={styles.backButton} type="button" onClick={() => router.push("/team/settings/integrations/")}><CharIconText ch="←">{tCommon("back")}</CharIconText></button>
    <header className={styles.pageHeader}><div><p className={styles.eyebrow}>{t("eyebrow")}</p><h1 className="ui-page-title">Slack</h1><p className="ui-page-desc">{t("description")}</p></div></header>
    {error ? <div className={`${styles.statusBanner} ${styles.statusBannerWarn}`}><strong>{t("error_title")}</strong><p>{error}</p></div> : null}
    <section className={styles.teamsHelp}>
      <div><p className={styles.eyebrow}>{t("step", { n: 1 })}</p><h2>{t("step1_title")}</h2></div>
      <p className={styles.teamsHelpNote}>{t("step1_note")}</p>
    </section>
    <section className={styles.teamsHelp}>
      <div><p className={styles.eyebrow}>{t("step", { n: 2 })}</p><h2>{t("step2_title")}</h2></div>
      <div className={styles.slackAppIntro}>
        <div><strong>{t("step2_intro_title")}</strong><p>{t("step2_intro_body")}</p></div>
        <a className={styles.primaryButton} href="https://api.slack.com/apps" target="_blank" rel="noreferrer">{t("step2_open")} <span aria-hidden="true"><CharIcon ch="↗" aria-hidden /></span></a>
      </div>
      <ol className={styles.slackAppCreationSteps}>
        <li><strong>{t("create_title")}</strong><p>{t.rich("create_body", richTags)}</p></li>
        <li><strong>{t("template_title")}</strong><p>{t.rich("template_body", richTags)}</p></li>
        <li><strong>{t("confirm_title")}</strong><p>{t.rich("confirm_body", richTags)}</p></li>
        <li><strong>{t("credentials_title")}</strong><p>{t.rich("credentials_body", richTags)}</p></li>
      </ol>
    </section>
    <form className={styles.teamsConfigCard} onSubmit={save}>
      <div className={styles.teamsCardHeader}><div><p className={styles.eyebrow}>{t("step_connection", { n: 3 })}</p><h2>{t("form_title")}</h2><p>{t("form_body")}</p></div></div>
      <div className={styles.teamsFormGrid}>
        <Field label="Client ID" hint={t("credentials_hint")}><input required name="clientId" defaultValue={config.client_id} /></Field>
        <Field label={tCommon("status_label")}><select name="status" defaultValue={config.status}><option value="draft">{tCommon("status_draft")}</option><option value="verified">{t("status_verified")}</option><option value="enabled">{tCommon("status_enabled")}</option><option value="disabled">{tCommon("status_disabled")}</option></select></Field>
        <Field label="Client Secret" hint={config.client_secret_configured ? tCommon("secret_saved_hint") : t("credentials_hint")}><input type="password" value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} /></Field>
        <Field label="Signing Secret" hint={config.signing_secret_configured ? tCommon("secret_saved_hint") : t("credentials_hint")}><input type="password" value={signingSecret} onChange={(event) => setSigningSecret(event.target.value)} /></Field>
        <label className={styles.teamsField}><span>{t("channel_answers_label")}</span><span className={styles.checkboxLabel}><input aria-label={t("channel_answers_checkbox")} name="channels" type="checkbox" defaultChecked={config.channel_answers_enabled} />{t("channel_answers_checkbox")}</span><small>{t("channel_answers_hint")}</small></label>
      </div>
      <div className={styles.teamsFormFooter}><p>{t("form_footer")}</p><button className={styles.primaryButton} disabled={saving} type="submit">{saving ? tCommon("saving") : tCommon("save")}</button></div>
    </form>
    <section className={styles.teamsHelp}>
      <div><p className={styles.eyebrow}>{t("step", { n: 4 })}</p><h2>{t("step4_title")}</h2></div>
      {config.oauth_callback_url ? <div className={styles.slackOauthAction}>
        <ol className={styles.slackSteps}>
          <li>{t.rich("step4_item1", richTags)}</li>
          <li>{t("step4_item2")}</li>
          <li>{t("step4_item3")}</li>
        </ol>
        <div className={styles.callbackList}><Url label={t("bot_redirect_label")} value={config.oauth_callback_url} /></div>
        <button className={styles.secondaryButton} type="button" disabled={installing || !config.client_secret_configured || !config.signing_secret_configured} onClick={() => void install()}>{installing ? t("installing") : t("install")}</button>
      </div> : <p className={styles.teamsHelpNote}>{t("step4_pending")}</p>}
    </section>
    <section className={styles.teamsHelp}><div><p className={styles.eyebrow}>{t("step", { n: 5 })}</p><h2>{t("step5_title")}</h2></div>
      {installed && config.event_url ? <div className={styles.teamsHelpGrid}>
        <SlackSetup title="Event Subscriptions" href={config.slack_settings?.events} action={t("events_action")} description={t.rich("events_description", richTags)} values={[{ label: "Event Request URL", value: config.event_url }, { label: t("events_subscribed_label"), value: "app_mention, message.im" }]} />
        <SlackSetup title="Slash Commands" href={config.slack_settings?.slash_commands} action={t("commands_action")} description={t("commands_description")} values={[{ label: t("command_label"), value: "/bowhead" }, { label: "Request URL", value: config.command_url || "" }]} />
        <SlackSetup title="Interactivity &amp; Shortcuts" href={config.slack_settings?.interactivity} action={t("interactivity_action")} description={t("interactivity_description")} values={[{ label: "Request URL", value: config.interaction_url || "" }]} />
      </div> : <p className={styles.teamsHelpNote}>{t("step5_pending")}</p>}
    </section>
    <section className={styles.teamsHelp}>
      <div><p className={styles.eyebrow}>{t("step_optional", { n: 6 })}</p><h2>{t("step6_title")}</h2></div>
      {config.personal_mcp_oauth_callback_url ? <div className={styles.slackPersonalMcp}>
        <p>{t("step6_intro")}</p>
        <ol className={styles.slackSteps}>
          <li>{t.rich("step6_item1", richTags)}</li>
          <li>{t.rich("step6_item2", richTags)}</li>
          <li>{t("step6_item3")}</li>
        </ol>
        <div className={styles.callbackList}><Url label={t("personal_redirect_label")} value={config.personal_mcp_oauth_callback_url} /></div>
        <div className={styles.slackScopeList} aria-label={t("personal_scopes_label")}>
          {PERSONAL_SLACK_MCP_USER_SCOPES.map((scope) => <code key={scope}>{scope}</code>)}
        </div>
        <p className={styles.teamsHelpNote}>{t("personal_scopes_note")}</p>
      </div> : <p className={styles.teamsHelpNote}>{t("step6_pending")}</p>}
    </section>
  </div>;
}

const PERSONAL_SLACK_MCP_USER_SCOPES = [
  "search:read.public", "search:read.private", "search:read.mpim", "search:read.im", "search:read.files", "search:read.users",
  "channels:history", "groups:history", "mpim:history", "im:history", "channels:read", "groups:read", "mpim:read", "im:read",
  "users:read", "users:read.email",
];

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className={styles.teamsField}><span>{label}</span>{children}{hint ? <small>{hint}</small> : null}</label>; }
function Url({ label, value }: { label: string; value: string }) {
  const t = useTranslations("settings.integrations.detail_common");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (await copyTextWithFallback(value, t("copy_fallback_prompt"))) setCopied(true);
  };

  return <div className={styles.callbackRow}>
    <span>{label}</span><code>{value}</code>
    <button className={`${styles.copyButton} ${styles.callbackCopyButton}`} type="button" aria-label={copied ? t("copied_value", { label }) : t("copy_value", { label })} onClick={() => void copy()}>
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
    </button>
  </div>;
}

function SlackSetup({ title, href, action, description, values }: { title: string; href?: string; action: string; description: ReactNode; values: { label: string; value: string }[] }) {
  return <article className={styles.slackSetupCard}><h3>{title}</h3><p>{description}</p>{href ? <a className={styles.secondaryButton} href={href} target="_blank" rel="noreferrer">{action}</a> : null}<div className={styles.callbackList}>{values.map((item) => <Url key={item.label} label={item.label} value={item.value} />)}</div></article>;
}
