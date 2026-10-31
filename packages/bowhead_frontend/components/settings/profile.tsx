"use client";

/**
 * Settings · Profile — edit display_name / avatar / password / logout-everywhere.
 *
 * P4.5b #17: settings lacked this tab; /profile already had a page but no settings link.
 *
 * fields:
 *   - email (read-only)
 *   - display_name (editable, PATCH /api/v1/profile/me)
 *   - avatar_url (read-only, populated automatically by OAuth provider)
 *   - tier + credit (read-only, fetched from /api/v1/auth/me)
 * actions:
 *   - Change Password → POST /api/v1/auth/change-password (current password + new password twice)
 *   - Forgot Current Password → /forgot-password (email flow)
 *   - logout everywhere → POST /api/v1/auth/logout-everywhere (all devices)
 */


import { ConfigurationFieldHelp } from "@/components/settings/configuration-field-help";
import type { FieldContract } from "@/lib/configuration-field-guidance";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AuthError, fetchMe, resendCurrentUserVerificationEmail, type MeUser } from "@/lib/auth-api";
import { LanguageMenuButton } from "@/components/locale-switcher";
import { AnswerLanguageMenuButton } from "@/components/settings/answer-language-menu";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { readLocaleCookie } from "@/lib/locale-names";
import { useLanguagePreferences } from "@/lib/use-language-preferences";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { SaveBar } from "@/components/ui/save-bar";
import { getUserSettingsView, setUserSetting } from "@/lib/api";
import { apiVoid } from "@/lib/api-client";
import { AccountDeletionControl } from "@/components/settings/account-deletion";
import { useCoachAfterEdit, type CoachAfterEdit } from "@/lib/agent-coach-after-edit";

const ANSWER_STYLES = ["concise", "detailed", "technical", "business"] as const;

export function SettingsProfile() {
  const t = useTranslations("settings.profile");
  const legacyProfileT = useTranslations("profile");
  const [me, setMe] = useState<MeUser | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [answerStyle, setAnswerStyle] = useState("detailed");
  const [savedAnswerStyle, setSavedAnswerStyle] = useState("detailed");
  const [busy, setBusy] = useState(false);
  const [resending, setResending] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const localeT = useTranslations("locale");
  const [fieldContract, setFieldContract] = useState<FieldContract>();
  const languagePrefs = useLanguagePreferences();
  const discardConfirm = useConfirmDialog();

  const load = useCallback(() => {
    void fetchMe().then((u) => {
      setMe(u);
      setDisplayName(u.display_name || "");
    }).catch((e) => setErr(t("load_failed", { err: "" })));
  }, [t]);

  useEffect(() => { load(); }, [load]);
  // Account menu › Language › "Answer language settings" deep-links here; the row exists once the account loads.
  const accountLoaded = me !== null;
  useEffect(() => {
    if (!accountLoaded || window.location.hash !== "#answer-language") return;
    const row = document.getElementById("answer-language");
    row?.scrollIntoView({ block: "center" });
    row?.querySelector<HTMLElement>("[data-testid='answer-language-button']")?.focus({ preventScroll: true });
  }, [accountLoaded]);
  useEffect(() => {
    void getUserSettingsView().then((view) => {
      setFieldContract(view.field_contract);
      const settings = view.settings;
      const style = typeof settings.answer_style === "string"
        ? settings.answer_style
        : "detailed";
      setAnswerStyle(style);
      setSavedAnswerStyle(style);
    }).catch(() => setErr(t("style_load_failed")));
  }, []);

  const flash = (m: string, isErr = false) => {
    if (isErr) setErr(m); else setMsg(m);
    if (!isErr) setTimeout(() => setMsg(null), 3000);
  };

  const saveProfile = async () => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    const name = displayName.trim();
    const style = answerStyle;
    const nameChanged = name !== (me?.display_name || "");
    const styleChanged = style !== savedAnswerStyle;
    try {
      const [nameResult, styleResult] = await Promise.allSettled([
        nameChanged
          ? apiVoid("/api/v1/profile/me", {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ display_name: name }),
            })
          : Promise.resolve(),
        styleChanged ? setUserSetting("answer_style", style) : Promise.resolve(),
      ]);
      if (nameChanged && nameResult.status === "fulfilled") {
        setMe((current) => current ? { ...current, display_name: name } : current);
      }
      if (styleChanged && styleResult.status === "fulfilled") setSavedAnswerStyle(style);
      if (nameResult.status === "fulfilled" && styleResult.status === "fulfilled") {
        flash(t("saved"));
      } else {
        const outcomes = [];
        if (nameChanged) outcomes.push(t(nameResult.status === "fulfilled" ? "name_saved" : "name_unconfirmed"));
        if (styleChanged) outcomes.push(t(styleResult.status === "fulfilled" ? "style_saved" : "style_unconfirmed"));
        setErr(outcomes.join(" "));
      }
    } finally {
      setBusy(false);
    }
  };

  // U5: the email shows an "Unverified" badge but had no way to act on it.
  const resendVerify = async () => {
    if (!me || resending) return;
    setResending(true);
    setMsg(null);
    setErr(null);
    try {
      const result = await resendCurrentUserVerificationEmail();
      if (result.status === "already_verified") {
        setMe((current) => current ? { ...current, email_verified: true } : current);
        setMsg(t("resend_already_verified"));
      } else {
        setMsg(t("resend_sent"));
      }
    } catch (e) {
      setErr(e instanceof AuthError && e.reasonCode === "resend_too_frequent"
        ? t("resend_wait")
        : t("resend_failed", { err: "" }));
    } finally {
      setResending(false);
    }
  };

  const dirty = me
    ? (displayName.trim() || "") !== (me.display_name || "") || answerStyle !== savedAnswerStyle
    : false;
  const discard = () => {
    setDisplayName(me?.display_name || "");
    setAnswerStyle(savedAnswerStyle);
  };

  // Choosing an interface language reloads the page (D3): confirm before dropping unsaved edits in the save bar.
  const confirmLanguageSwitch = async () =>
    !dirty ||
    discardConfirm.confirm({
      title: localeT("discard_title"),
      description: localeT("discard_description"),
      confirmLabel: localeT("discard_confirm"),
      tone: "discard",
    });

  if (!me) {
    // Keep the header and replace the content with grouped-card skeletons (Page Layout Spec §4).
    if (!err) {
      return (
        <div className="legacy-profile-page">
          <h1 className="ui-page-title legacy-profile-title">{t("title")}</h1>
          <p className="ui-page-desc legacy-profile-desc">{t("subtitle")}</p>
          <LoadingSkeleton variant="form" rows={3} label={t("loading")} />
        </div>
      );
    }
    return <Notice tone="error">{err}</Notice>;
  }

  const numberFormatter = new Intl.NumberFormat(undefined);

  return (
    <div className="legacy-profile-page">
      <h1 className="ui-page-title legacy-profile-title">{t("title")}</h1>
      <p className="ui-page-desc legacy-profile-desc">
        {t("subtitle")}
      </p>

      {msg ? <Notice tone="success">{msg}</Notice> : null}
      {err ? <Notice tone="error">{err}</Notice> : null}

      <Section title={t("sections.account")}>
        <Row label={t("fields.email")}>
          <span className="legacy-profile-mono profile-email">{me.email}</span>
          {me.email_verified ? (
            <Badge tone="positive" className="legacy-profile-badge legacy-profile-badge--ok">{t("verified")}</Badge>
          ) : (
            <>
              <Badge tone="warning" className="legacy-profile-badge legacy-profile-badge--warn">{t("unverified")}</Badge>
              <Button size="sm" variant="secondary" onClick={resendVerify} disabled={busy || resending}>
                {t(resending ? "resend_sending" : "resend_verify")}
              </Button>
            </>
          )}
        </Row>
        <Row label={t("fields.display_name")}>
          <input
            aria-label={t("fields.display_name")}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={t("display_name_placeholder")}
            disabled={busy}
            className="ui-input legacy-input-profile"
          />
        </Row>
        <Row label={legacyProfileT("field.answer_style")} labelId="profile-answer-style">
          {/* A menu button like the language rows (Polish Spec: native select → menu button with a leading check). */}
          <FilterMenuButton
            value={answerStyle}
            options={ANSWER_STYLES.map((style) => ({ value: style, label: legacyProfileT(`style.${style}`) }))}
            onChange={setAnswerStyle}
            format={(value) => value}
            menuLabel={legacyProfileT("field.answer_style")}
            active={false}
            disabled={busy}
            labelledBy="profile-answer-style"
            className="ui-language-button"
            testId="answer-style-button"
          />
        </Row>
        {me.avatar_url ? (
          <Row label={t("fields.avatar")}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={me.avatar_url} alt=""
              className="legacy-profile-avatar"
              onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
            />
            <span className="legacy-profile-hint">{t("avatar_source")}</span>
          </Row>
        ) : null}
      </Section>

      <Section title={t("sections.preferences")}>
        <Row label={t("fields.language")} labelId="profile-interface-language" hint={t("language_hint")}>
          <LanguageMenuButton
            labelledBy="profile-interface-language"
            saved={
              languagePrefs.status === "ready"
                ? languagePrefs.ui
                : (readLocaleCookie() ?? "auto")
            }
            beforeSwitch={confirmLanguageSwitch}
            onSaved={languagePrefs.rememberUiLanguage}
          />
        </Row>
        <Row
          id="answer-language"
          label={<span className="configuration-field-label">{t("fields.answer_language")}<ConfigurationFieldHelp contract={fieldContract} field="user_language.output_language" label={t("fields.answer_language")} /></span>}
          labelId="profile-answer-language"
          hint={t("answer_language_hint")}
        >
          <AnswerLanguageMenuButton labelledBy="profile-answer-language" />
        </Row>
      </Section>

      <Section title={t("sections.agents")}>
        <CoachAfterEditRow />
      </Section>

      <Section title={t("sections.billing")}>
        <Row label={t("fields.tier")}>
          <Badge tone={me.tier === "ultra" ? "accent" : "neutral"}
            className={`legacy-profile-badge legacy-profile-badge--${me.tier === "ultra" ? "accent" : "neutral"}`}>
            {me.tier.toUpperCase()}
          </Badge>
          {me.is_team_admin ? <Badge tone="positive" className="legacy-profile-badge legacy-profile-badge--ok">{t("team_admin")}</Badge> : null}
        </Row>
        <Row label={t("fields.credit")}>
          <span className="legacy-profile-mono profile-credit">
            {numberFormatter.format(me.credit_balance)} / {numberFormatter.format(me.credit_quota)}
          </span>
          <span className="legacy-profile-hint">{t("credit_hint")}</span>
        </Row>
      </Section>

      <SaveBar dirty={dirty} saving={busy} onSave={() => { void saveProfile(); }} onDiscard={discard} />
      {me.capabilities?.account_deletion ? <AccountDeletionControl user={me} /> : null}
      {discardConfirm.dialog}
    </div>
  );
}

/** Saves on selection, like the language rows: the setting is independent of the profile save bar. */
function CoachAfterEditRow() {
  const t = useTranslations("settings.profile");
  const setting = useCoachAfterEdit();
  return (
    <Row label={t("fields.coach_after_edit")} labelId="profile-coach-after-edit" hint={t("coach_after_edit_hint")}>
      <FilterMenuButton
        value={setting.value ?? "check"}
        options={(["check", "ask"] as const).map((value) => ({ value, label: t(`coach_after_edit_${value}`) }))}
        onChange={(value) => { void setting.change(value as CoachAfterEdit); }}
        format={(value) => value}
        menuLabel={t("fields.coach_after_edit")}
        active={false}
        disabled={setting.value === null || setting.saving}
        labelledBy="profile-coach-after-edit"
        className="ui-language-button"
        testId="coach-after-edit-button"
      />
      {setting.failed ? <span className="legacy-profile-hint" role="alert">{t("coach_after_edit_failed")}</span> : null}
    </Row>
  );
}

/** Grouping (decision 15): headings outside the cards, rows inside (label left, control right, decision 16). */
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="ui-group profile-group">
      <div className="ui-group-head">
        <h2 className="ui-group-title">{title}</h2>
      </div>
      <div className="ui-group-card">{children}</div>
    </section>
  );
}

function Row({
  label,
  children,
  id,
  labelId,
  hint,
}: {
  label: React.ReactNode;
  children: React.ReactNode;
  /** Deep-link anchor (e.g. #answer-language from the account menu). */
  id?: string;
  labelId?: string;
  /** One-line explanation under the label (Header, Language & Paging Spec §1b). */
  hint?: string;
}) {
  return (
    <div id={id} className="legacy-profile-row ui-group-row" data-has-hint={hint ? "true" : undefined}>
      <span className="legacy-profile-label ui-group-label">
        <span id={labelId}>{label}</span>
        {hint ? <small className="ui-group-hint">{hint}</small> : null}
      </span>
      <div className="legacy-profile-control ui-group-control">
        {children}
      </div>
    </div>
  );
}
