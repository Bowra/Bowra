"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import { chartStylePreset } from "@/lib/chart/chart-style-registry";
import {
  activateTeamChartStyle,
  createTeamChartStyle,
  deactivateTeamChartStyle,
  getTeamChartStyle,
  getTeamChartStylePolicyV2,
  listTeamChartStyles,
  setTeamChartStylePolicyV2,
  type BuiltInChartStyleId,
  type TeamChartStyleDetail,
  type TeamChartStylePolicyV2,
  type TeamChartStyleSummary,
} from "@/lib/team-chart-styles-api";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";
import { useAppRouter } from "@/lib/use-app-router";
import { randomUuid } from "@/lib/random-id";

import { ChevronRight, Edit, Palette, Plus, RefreshCw } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { PromptDialog } from "@/components/ui/prompt-dialog";
import { SaveBar } from "@/components/ui/save-bar";

const BUILT_INS: BuiltInChartStyleId[] = [
  "bowhead-editorial",
  "clean-blue",
  "analytical-neutral",
];

export function TeamChartStyleLibrary() {
  const t = useTranslations("settings.appearance.chartStyleV2");
  const baseT = useTranslations("settings.appearance.chartStyle");
  const locale = useLocale();
  const router = useAppRouter();
  const [styles, setStyles] = useState<TeamChartStyleSummary[]>([]);
  const [details, setDetails] = useState<Record<string, TeamChartStyleDetail>>({});
  const [policy, setPolicy] = useState<TeamChartStylePolicyV2 | null>(null);
  const [confirmedPolicy, setConfirmedPolicy] = useState<TeamChartStylePolicyV2 | null>(null);
  const [policyEtag, setPolicyEtag] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  // Save bar shows Saving… only while saving selection scope; other busy states such as enabling/disabling styles do not count.
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshNeeded, setRefreshNeeded] = useState(false);
  const [createBase, setCreateBase] = useState<BuiltInChartStyleId | null>(null);

  /**
   * Read style catalog and selection scope. `keepDraft`: after enabling/disabling a style, retain unsaved selection scope on refresh (removing unavailable styles),
   * without returning to whole-page loading; update server-confirmed values and revision normally.
   */
  const load = useCallback(async ({ keepDraft = null, mutationSucceeded = false }: { keepDraft?: TeamChartStylePolicyV2 | null; mutationSucceeded?: boolean } = {}) => {
    if (!keepDraft && !mutationSucceeded) setLoading(true);
    setError(null);
    try {
      const [catalog, policyView] = await Promise.all([
        listTeamChartStyles(),
        getTeamChartStylePolicyV2(),
      ]);
      const entries = await Promise.all(
        catalog.items.map(async (item) => [item.id, await getTeamChartStyle(item.id)] as const),
      );
      setStyles(catalog.items);
      setDetails(Object.fromEntries(entries));
      setPolicy(keepDraft ? draftWithinChoices(keepDraft, catalog.items, policyView.policy) : policyView.policy);
      setConfirmedPolicy(policyView.policy);
      setPolicyEtag(policyView.etag);
      return true;
    } catch (caught) {
      setError(mutationSucceeded ? t("errors.saved_refresh_failed") : messageOf(caught, t("errors.load"), t));
      return false;
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const activeCustom = useMemo(
    () => styles.filter((style) => style.lifecycle === "active" && style.activeRevisionId),
    [styles],
  );
  const capacityUsed = useMemo(
    () => styles.filter((style) => style.lifecycle === "active" || style.draftEtag).length,
    [styles],
  );
  const choices = useMemo(
    () => [
      ...BUILT_INS.map((id) => ({ id, name: presetName(id, baseT), kind: "builtIn" as const })),
      ...activeCustom.map((style) => ({ id: style.id, name: style.name, kind: "teamCustom" as const })),
    ],
    [activeCustom, baseT],
  );
  const policyDirty = Boolean(policy && confirmedPolicy && JSON.stringify(policy) !== JSON.stringify(confirmedPolicy));

  const refreshSaved = async (readCatalog: boolean) => {
    const [catalog, theme] = await Promise.allSettled([
      readCatalog ? load({ keepDraft: policyDirty ? policy : null, mutationSucceeded: true }) : Promise.resolve(true),
      refreshEffectiveTheme(),
    ]);
    const refreshed = catalog.status === "fulfilled" && catalog.value === true
      && theme.status === "fulfilled" && theme.value === true;
    setRefreshNeeded(!refreshed);
    setError(refreshed ? null : t("errors.saved_refresh_failed"));
  };

  const retryRefresh = async () => {
    setBusy(true);
    try { await refreshSaved(true); } finally { setBusy(false); }
  };

  const savePolicy = async () => {
    if (!policy || !policyDirty) return;
    setBusy(true);
    setSavingPolicy(true);
    setError(null);
    setRefreshNeeded(false);
    try {
      const result = await setTeamChartStylePolicyV2(policy, policyEtag);
      setPolicy(result.policy);
      setConfirmedPolicy(result.policy);
      setPolicyEtag(result.etag);
      await refreshSaved(false);
    } catch (caught) {
      if (caught instanceof ApiError && [409, 412].includes(caught.status)) {
        setError(t("errors.conflict"));
      } else {
        setError(messageOf(caught, t("errors.policy"), t));
      }
    } finally {
      setBusy(false);
      setSavingPolicy(false);
    }
  };

  const create = async (name: string) => {
    if (!createBase) return;
    setBusy(true);
    setError(null);
    setRefreshNeeded(false);
    try {
      const result = await createTeamChartStyle({
        requestId: randomUuid(),
        basePresetId: createBase,
        source: {
          schemaVersion: 2,
          name,
          description: "",
          overrides: { schemaVersion: 2 },
        },
      });
      setCreateBase(null);
      router.push(`/team/settings/brand-appearance/chart-styles/?styleId=${encodeURIComponent(result.detail.style.id)}`);
    } catch (caught) {
      setError(messageOf(caught, t("errors.create"), t));
      setBusy(false);
    }
  };

  const toggleLifecycle = async (style: TeamChartStyleSummary) => {
    const detail = details[style.id];
    if (!detail) return;
    setBusy(true);
    setError(null);
    setRefreshNeeded(false);
    try {
      const result = style.lifecycle === "active"
        ? await deactivateTeamChartStyle(style.id, detail.headEtag)
        : await activateTeamChartStyle(style.id, detail.headEtag);
      setDetails((current) => ({ ...current, [style.id]: result.detail }));
      setStyles((current) => current.map((item) => item.id === style.id
        ? { ...item, lifecycle: result.detail.style.lifecycle, state: result.detail.state } : item));
      await refreshSaved(true);
    } catch (caught) {
      setError(messageOf(caught, t("errors.lifecycle"), t));
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = (styleId: string) => {
    if (!policy) return;
    const enabled = policy.enabledStyleIds.includes(styleId);
    if (enabled && policy.enabledStyleIds.length <= 2) return;
    const enabledStyleIds = enabled
      ? policy.enabledStyleIds.filter((id) => id !== styleId)
      : [...policy.enabledStyleIds, styleId];
    setPolicy({
      ...policy,
      enabledStyleIds,
      defaultStyleId: enabledStyleIds.includes(policy.defaultStyleId)
        ? policy.defaultStyleId
        : enabledStyleIds[0],
    });
  };

  if (loading) return <div className="cs-library-skeleton" aria-label={baseT("loading")} />;
  if (!policy) {
    return (
      <div className="cs-library-error">
        <Notice tone="error">{error ?? t("errors.load")}</Notice>
        <Button onClick={() => void load()}><RefreshCw size={14} aria-hidden /> {baseT("retry")}</Button>
      </div>
    );
  }

  return (
    <div className="cs-library" data-testid="team-chart-style-panel">
      <header className="cs-library__header">
        <div>
          <span className="cs-kicker">{baseT("independent")}</span>
          <h2>{t("libraryTitle")}</h2>
          <p>{t("libraryDescription")}</p>
        </div>
      </header>

      {error ? (
        <Notice tone="error">
          <span>{error}</span>
          <Button size="sm" disabled={busy} onClick={() => void (refreshNeeded ? retryRefresh() : load())}>{refreshNeeded ? t("retryRefresh") : baseT("reload")}</Button>
        </Notice>
      ) : null}

      <section className="cs-library__section" aria-labelledby="built-in-style-title">
        <div className="cs-library__section-heading">
          <div><h3 id="built-in-style-title">{t("builtInTitle")}</h3><p>{t("builtInDescription")}</p></div>
        </div>
        <div className="cs-built-in-grid">
          {BUILT_INS.map((presetId) => {
            const preset = chartStylePreset(presetId);
            return (
              <article key={presetId} className="cs-built-in-card">
                <PaletteStrip colors={preset.tokens.light.palette} />
                <div><strong>{presetName(presetId, baseT)}</strong><p>{presetDescription(presetId, baseT)}</p></div>
                <Button
                  size="sm"
                  onClick={() => setCreateBase(presetId)}
                  disabled={busy || capacityUsed >= 12}
                  title={capacityUsed >= 12 ? t("errors.chart_style_limit_reached") : undefined}
                >
                  <Plus size={14} aria-hidden /> {t("customize")}
                </Button>
              </article>
            );
          })}
        </div>
      </section>

      <section className="cs-library__section" aria-labelledby="team-style-title">
        <div className="cs-library__section-heading">
          <div><h3 id="team-style-title">{t("teamStylesTitle")}</h3><p>{t("teamStylesDescription")}</p></div>
          <span className="cs-count">{t("styleCount", { count: capacityUsed })}</span>
        </div>
        {styles.length ? (
          <div className="cs-style-list">
            {styles.map((style) => {
              const detail = details[style.id];
              const colors = detail?.activeRevision?.resolvedSnapshot.colors.light.categorical
                ?? chartStylePreset(style.fallbackBuiltInPresetId).tokens.light.palette;
              return (
                <article key={style.id} className="cs-style-row">
                  <PaletteStrip colors={colors} />
                  <div className="cs-style-row__body">
                    <div><strong>{style.name}</strong><Status state={style.state} t={t} /></div>
                    <span>{t("updated", { date: formatDate(style.updatedAt, locale) })}</span>
                  </div>
                  <div className="cs-style-row__actions">
                    {style.activeRevisionId ? (
                      <Button
                        size="sm"
                        onClick={() => void toggleLifecycle(style)}
                        disabled={busy || (style.lifecycle === "active"
                          ? !detail?.capabilities.deactivate.allowed
                          : !detail?.capabilities.activate.allowed)}
                      >
                        {style.lifecycle === "active" ? t("deactivate") : t("activate")}
                      </Button>
                    ) : null}
                    <Button size="sm" onClick={() => router.push(`/team/settings/brand-appearance/chart-styles/?styleId=${encodeURIComponent(style.id)}`)}>
                      <Edit aria-hidden /> {t("openEditor")} <ChevronRight size={13} aria-hidden />
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="cs-empty-state"><Palette size={20} aria-hidden /><strong>{t("emptyTitle")}</strong><span>{t("emptyDescription")}</span></div>
        )}
      </section>

      <section className="cs-library__section" aria-labelledby="policy-title">
        <div className="cs-library__section-heading">
          <div><h3 id="policy-title">{t("policyTitle")}</h3><p>{t("policyDescription")}</p></div>
          {/* The bottom Save bar saves selection-scope changes (decision 17). */}
        </div>
        <div className="cs-policy-list">
          {choices.map((choice) => {
            const enabled = policy.enabledStyleIds.includes(choice.id);
            return (
              <div key={choice.id} className="cs-policy-row" data-disabled={!enabled || undefined}>
                <PaletteStrip colors={paletteForChoice(choice.id, details)} />
                <div><strong>{choice.name}</strong><span>{choice.kind === "teamCustom" ? t("teamStyleBadge") : t("builtInBadge")}</span></div>
                <label><input type="checkbox" checked={enabled} disabled={busy || (enabled && policy.enabledStyleIds.length <= 2)} onChange={() => toggleEnabled(choice.id)} /> {baseT("enabled")}</label>
                <label><input type="radio" name="chart-style-team-default" checked={policy.defaultStyleId === choice.id} disabled={busy || !enabled} onChange={() => setPolicy({ ...policy, defaultStyleId: choice.id })} /> {baseT("default")}</label>
              </div>
            );
          })}
        </div>
        <p className="cs-policy-note">{baseT("atLeastTwo")}</p>
        <SaveBar
          dirty={policyDirty}
          saving={savingPolicy}
          saveDisabled={busy}
          saveLabel={t("savePolicy")}
          onSave={() => void savePolicy()}
          onDiscard={() => { if (confirmedPolicy) setPolicy(confirmedPolicy); }}
        />
      </section>

      {createBase ? (
        <PromptDialog
          title={t("createTitle")}
          label={t("createNameLabel")}
          initialValue={t("createNameDefault", { preset: presetName(createBase, baseT) })}
          confirmLabel={t("createAndEdit")}
          cancelLabel={t("cancel")}
          busy={busy}
          onCancel={() => setCreateBase(null)}
          onConfirm={(name) => void create(name)}
        />
      ) : null}
    </div>
  );
}

function PaletteStrip({ colors }: { colors: readonly string[] }) {
  return <span className="cs-palette-strip" aria-hidden>{colors.slice(0, 6).map((color, index) => <i key={`${color}-${index}`} style={{ background: color }} />)}</span>;
}

function Status({ state, t }: { state: TeamChartStyleSummary["state"]; t: ReturnType<typeof useTranslations> }) {
  const key = state === "draft_only" ? "draft" : state.startsWith("inactive") ? "inactive" : state.endsWith("editing") ? "editing" : "active";
  return <span className={`cs-status cs-status--${key}`}>{t(`states.${key}`)}</span>;
}

function presetName(id: BuiltInChartStyleId, t: ReturnType<typeof useTranslations>): string {
  if (id === "clean-blue") return t("presets.cleanBlue");
  if (id === "analytical-neutral") return t("presets.analyticalNeutral");
  return t("presets.bowheadEditorial");
}

function presetDescription(id: BuiltInChartStyleId, t: ReturnType<typeof useTranslations>): string {
  if (id === "clean-blue") return t("presets.cleanBlueDesc");
  if (id === "analytical-neutral") return t("presets.analyticalNeutralDesc");
  return t("presets.bowheadEditorialDesc");
}

function paletteForChoice(id: string, details: Record<string, TeamChartStyleDetail>): readonly string[] {
  if (BUILT_INS.includes(id as BuiltInChartStyleId)) return chartStylePreset(id as BuiltInChartStyleId).tokens.light.palette;
  return details[id]?.activeRevision?.resolvedSnapshot.colors.light.categorical ?? [];
}

function formatDate(value: string, locale: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function messageOf(error: unknown, fallback: string, t: ReturnType<typeof useTranslations>): string {
  if (error instanceof ApiError && error.code) {
    const known = ["chart_style_name_conflict", "chart_style_limit_reached", "chart_style_precondition_failed", "chart_style_policy_precondition_failed", "chart_style_policy_minimum", "chart_style_default_style"];
    if (known.includes(error.code)) return t(`errors.${error.code}`);
  }
  return fallback;
}

/** Remove unavailable styles (disabled or team styles without an effective revision) from unsaved scope; default falls back to the first still-enabled style. */
function draftWithinChoices(
  draft: TeamChartStylePolicyV2,
  styles: TeamChartStyleSummary[],
  server: TeamChartStylePolicyV2,
): TeamChartStylePolicyV2 {
  const selectable = new Set<string>([
    ...BUILT_INS,
    ...styles.filter((style) => style.lifecycle === "active" && style.activeRevisionId).map((style) => style.id),
  ]);
  const enabledStyleIds = draft.enabledStyleIds.filter((id) => selectable.has(id));
  if (!enabledStyleIds.length) return server;
  return {
    ...draft,
    enabledStyleIds,
    defaultStyleId: enabledStyleIds.includes(draft.defaultStyleId) ? draft.defaultStyleId : enabledStyleIds[0],
  };
}
