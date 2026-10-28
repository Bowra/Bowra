"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";

import {
  confirmAgentLocalization,
  getAgentLocalizations,
  saveAgentLocalization,
  suggestAgentLocalization,
  type AgentLocalizationManager,
  type AgentLocalizedListing,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { DEFAULT_LOCALE, SUPPORTED_LOCALES, type Locale } from "@/lib/locale";

function otherLocale(locale: string): Locale {
  return locale === DEFAULT_LOCALE
    ? SUPPORTED_LOCALES.find((candidate) => candidate !== locale)!
    : DEFAULT_LOCALE;
}

export function AgentLocalizationPanel({
  agentId,
  archived,
  parentDirty,
  refreshToken,
  onLocalizationChanged,
}: {
  agentId: string;
  archived: boolean;
  parentDirty: boolean;
  refreshToken: string;
  onLocalizationChanged: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const [manager, setManager] = useState<AgentLocalizationManager | null>(null);
  const [available, setAvailable] = useState(true);
  const [sourceLocale, setSourceLocale] = useState<string>("en-US");
  const [targetLocale, setTargetLocale] = useState<string>("en-US");
  const [displayName, setDisplayName] = useState("");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState<"suggest" | "save" | "confirm" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const value = await getAgentLocalizations(agentId);
      setManager(value);
      setAvailable(true);
      setSourceLocale(value.source_locale);
      setTargetLocale((current) => current === value.source_locale ? otherLocale(value.source_locale) : current);
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        setAvailable(false);
        return;
      }
      setError(cause instanceof Error ? cause.message : t("localization_load_failed"));
    }
  }, [agentId, t]);

  useEffect(() => { void load(); }, [load, refreshToken]);

  const selected = useMemo(
    () => manager?.items.find((item) => item.locale === targetLocale) ?? null,
    [manager?.items, targetLocale],
  );

  useEffect(() => {
    setDisplayName(selected?.fields.display_name ?? "");
    setSummary(selected?.fields.summary ?? "");
  }, [selected?.id, selected?.row_version, targetLocale]);

  const replaceItem = (item: AgentLocalizedListing) => {
    setManager((current) => current ? {
      ...current,
      items: [...current.items.filter((candidate) => candidate.locale !== item.locale), item]
        .sort((left, right) => left.locale.localeCompare(right.locale)),
    } : current);
  };

  const draftDirty = Boolean(
    selected
      ? displayName.trim() !== selected.fields.display_name || summary.trim() !== selected.fields.summary
      : displayName.trim() || summary.trim(),
  );
  const needsSave = draftDirty || selected?.review_state === "stale";
  const formBlocked = archived || parentDirty || busy !== null || sourceLocale === targetLocale;

  const handleMutationError = async (cause: unknown, fallbackKey: string) => {
    if (cause instanceof ApiError && [
      "catalog_localization_source_changed",
      "catalog_localization_version_conflict",
    ].includes(cause.code ?? "")) {
      await load();
      setError(t("localization_refresh_required"));
      return;
    }
    setError(cause instanceof Error ? cause.message : t(fallbackKey as never));
  };

  const suggest = async () => {
    if (!manager) return;
    setBusy("suggest");
    setError(null);
    setNotice(null);
    try {
      const item = await suggestAgentLocalization(agentId, {
        source_locale: sourceLocale,
        target_locale: targetLocale,
        expected_source_hash: manager.source_hash,
        expected_row_version: selected?.row_version ?? null,
      });
      replaceItem(item);
      onLocalizationChanged();
      setNotice(t("localization_ai_ready"));
    } catch (cause) {
      await handleMutationError(cause, "localization_suggest_failed");
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!manager) return;
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const item = await saveAgentLocalization(agentId, targetLocale, {
        source_locale: sourceLocale,
        fields: { display_name: displayName.trim(), summary: summary.trim() },
        expected_source_hash: manager.source_hash,
        expected_row_version: selected?.row_version ?? null,
      });
      replaceItem(item);
      onLocalizationChanged();
      setNotice(t("localization_draft_saved"));
    } catch (cause) {
      await handleMutationError(cause, "localization_save_failed");
    } finally {
      setBusy(null);
    }
  };

  const confirm = async () => {
    if (!selected) return;
    setBusy("confirm");
    setError(null);
    setNotice(null);
    try {
      const item = await confirmAgentLocalization(agentId, targetLocale, selected.row_version);
      replaceItem(item);
      onLocalizationChanged();
      setNotice(t("localization_confirmed"));
    } catch (cause) {
      await handleMutationError(cause, "localization_confirm_failed");
    } finally {
      setBusy(null);
    }
  };

  if (!available) return null;

  return (
    <section className="am-localization-section" aria-labelledby="agent-localization-title">
      <header className="am-localization-header">
        <div>
          <span>{t("localization_eyebrow")}</span>
          <h2 id="agent-localization-title">{t("localization_title")}</h2>
          <p>{t("localization_body")}</p>
        </div>
        <div className="am-localization-route" aria-label={t("localization_language_route")}>
          <select
            aria-label={t("localization_source_language")}
            value={sourceLocale}
            disabled={archived || busy !== null || Boolean(manager?.items.length)}
            title={manager?.items.length ? t("localization_source_locked") : undefined}
            onChange={(event) => {
              const next = event.target.value;
              setSourceLocale(next);
              if (next === targetLocale) setTargetLocale(otherLocale(next));
            }}
          >
            {SUPPORTED_LOCALES.map((locale) => <option key={locale} value={locale}>{t(`locale_${locale}` as never)}</option>)}
          </select>
          <span aria-hidden="true">→</span>
          <select
            aria-label={t("localization_target_language")}
            value={targetLocale}
            disabled={archived || busy !== null}
            onChange={(event) => setTargetLocale(event.target.value)}
          >
            {SUPPORTED_LOCALES.filter((locale) => locale !== sourceLocale).map((locale) => <option key={locale} value={locale}>{t(`locale_${locale}` as never)}</option>)}
          </select>
        </div>
      </header>

      {!manager ? <div className="am-localization-loading" aria-busy="true">{t("loading")}</div> : (
        <>
          <div className="am-localization-grid">
            <article className="am-localization-source">
              <div className="am-localization-card-heading"><span>{t("localization_original")}</span><em>{t(`locale_${sourceLocale}` as never)}</em></div>
              <h3>{manager.source.display_name}</h3>
              <p>{manager.source.summary}</p>
            </article>
            <article className="am-localization-editor">
              <div className="am-localization-card-heading">
                <span>{t("localization_translation")}</span>
                {selected ? <em data-state={selected.review_state}>{t(`localization_state_${selected.review_state}` as never)}</em> : <em>{t("localization_state_new")}</em>}
              </div>
              <label><span>{t("localization_name")}</span><input value={displayName} maxLength={120} disabled={archived || busy !== null} onChange={(event) => setDisplayName(event.target.value)} /></label>
              <label><span>{t("localization_summary")}</span><textarea rows={4} value={summary} maxLength={4000} disabled={archived || busy !== null} onChange={(event) => setSummary(event.target.value)} /></label>
              {selected?.generation_source === "llm" ? <small>{t("localization_ai_draft_note")}</small> : null}
            </article>
          </div>

          {parentDirty ? <p className="am-localization-guidance">{t("localization_save_parent_first")}</p> : null}
          <p className="am-localization-guidance">{t("localization_release_note")}</p>
          {error ? <p className="am-release-error" role="alert">{error}</p> : null}
          {notice ? <p className="am-release-notice" role="status">{notice}</p> : null}
          <div className="am-localization-actions">
            <button type="button" disabled={formBlocked} onClick={() => void suggest()}>{busy === "suggest" ? t("localization_suggesting") : t("localization_suggest")}</button>
            <button type="button" disabled={formBlocked || !needsSave || !displayName.trim() || !summary.trim()} onClick={() => void save()}>{busy === "save" ? t("localization_saving") : t("localization_save")}</button>
            <button type="button" className="am-action-primary" disabled={formBlocked || !selected || draftDirty || selected.review_state === "confirmed" || selected.review_state === "stale"} onClick={() => void confirm()}>{busy === "confirm" ? t("localization_confirming") : t("localization_confirm")}</button>
          </div>
        </>
      )}
    </section>
  );
}
