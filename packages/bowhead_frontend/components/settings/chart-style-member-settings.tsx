"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import { patchChartAccessibility } from "@/lib/api";
import { getBrandSnapshot, subscribeBrandSnapshot } from "@/lib/brand-runtime";
import {
  chartThemeBaseForMode,
  resolveChartThemeForEffectiveV2,
} from "@/lib/chart-theme";
import {
  getChartStyleOptionsV2,
  setChartStyleSelectionV2,
  type ChartStyleOptionV2,
} from "@/lib/team-chart-styles-api";
import { refreshEffectiveTheme } from "@/lib/use-theme-config";

import { Check, RefreshCw } from "@/components/icons";
import { ChartStylePreview } from "@/components/settings/chart-style-editor/chart-style-preview";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { Switch } from "@/components/ui/switch";

type SaveStatus = "idle" | "saving" | "saved" | "error" | "refresh_failed";

/** Follow Team card value in the radio group (selection is null when selected); team style IDs cannot equal this string. */
const FOLLOW_TEAM = "__follow_team__";

export function PersonalChartStyleSettings() {
  const t = useTranslations("settings.appearance.chartStyle");
  const v2 = useTranslations("settings.appearance.chartStyleV2");
  const bundle = useSyncExternalStore(subscribeBrandSnapshot, getBrandSnapshot, getBrandSnapshot);
  const effective = bundle.chartStyle?.schemaVersion === 2 ? bundle.chartStyle : null;
  const [options, setOptions] = useState<ChartStyleOptionV2[]>([]);
  const [selected, setSelected] = useState<string | null>(effective?.selection.savedStyleId ?? null);
  const [accessibility, setAccessibility] = useState(effective?.accessibility ?? null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<SaveStatus>("idle");
  const selectionChain = useRef<Promise<void>>(Promise.resolve());
  const accessibilityChain = useRef<Promise<void>>(Promise.resolve());
  const operationRevision = useRef(0);
  const selectionRevision = useRef(0);
  const contrastRevision = useRef(0);
  const colorRevision = useRef(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await getChartStyleOptionsV2();
      setOptions(response.items);
    } catch {
      setError(t("loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!effective || status === "saving" || status === "refresh_failed" || status === "error") return;
    setSelected(effective.selection.savedStyleId);
    setAccessibility(effective.accessibility);
  }, [effective, status]);

  const queue = useCallback((
    chain: { current: Promise<void> },
    revisionRef: { current: number },
    run: () => Promise<void>,
    rollback: () => void,
  ) => {
    const revision = ++revisionRef.current;
    const operationId = ++operationRevision.current;
    setError(null);
    setStatus("saving");
    const operation = chain.current
      .catch(() => undefined)
      .then(run)
      .then(async () => {
        try {
          const refreshed = await refreshEffectiveTheme();
          if (operationId === operationRevision.current) setStatus(refreshed ? "saved" : "refresh_failed");
        } catch {
          if (operationId === operationRevision.current) setStatus("refresh_failed");
        }
      })
      .catch(() => {
        if (revision !== revisionRef.current) return;
        rollback();
        if (operationId === operationRevision.current) setStatus("error");
      });
    chain.current = operation;
  }, []);

  if (loading && !effective) return <div className="cs-member-skeleton" aria-label={t("loading")} />;
  if (!effective || !accessibility) {
    return (
      <div className="cs-member-error">
        <Notice tone="error">{error ?? t("loadFailed")}</Notice>
        <Button size="sm" onClick={() => void load()}><RefreshCw size={14} aria-hidden /> {t("retry")}</Button>
      </div>
    );
  }

  const effectiveSelectedId = selected && options.some((option) => option.id === selected)
    ? selected
    : effective.teamPolicy.defaultStyleId;

  const updateSelection = (next: string | null) => {
    const previous = selected;
    setSelected(next);
    queue(
      selectionChain,
      selectionRevision,
      () => setChartStyleSelectionV2(next),
      () => setSelected(previous),
    );
  };
  const updateAccessibility = (field: "contrast" | "colorVision", value: string) => {
    const previous = accessibility;
    const next = { ...accessibility, [field]: value } as typeof accessibility;
    setAccessibility(next);
    queue(
      accessibilityChain,
      field === "contrast" ? contrastRevision : colorRevision,
      () => patchChartAccessibility({ [field]: value }),
      () => setAccessibility((current) => current ? { ...current, [field]: previous[field] } : current),
    );
  };
  const resetAll = async () => {
    setSelected(null);
    setAccessibility({
      ...accessibility,
      contrast: "standard",
      colorVision: "standard",
    });
    setStatus("saving");
    setError(null);
    const operationId = ++operationRevision.current;
    const results = await Promise.allSettled([
      selectionChain.current.then(() => setChartStyleSelectionV2(null)),
      accessibilityChain.current.then(() => patchChartAccessibility({ contrast: null, colorVision: null })),
    ]);
    if (operationId !== operationRevision.current) return;
    if (results.some((result) => result.status === "rejected")) {
      setStatus("error");
      setError(t("resetUnconfirmed"));
      return;
    }
    try {
      const refreshed = await refreshEffectiveTheme();
      if (operationId === operationRevision.current) setStatus(refreshed ? "saved" : "refresh_failed");
    } catch {
      if (operationId === operationRevision.current) setStatus("refresh_failed");
    }
  };

  const refresh = async () => {
    const operationId = ++operationRevision.current;
    try {
      const refreshed = await refreshEffectiveTheme();
      if (refreshed && operationId === operationRevision.current) {
        setError(null);
        setStatus("idle");
      }
    } catch {
      // Keep the existing outcome until a read actually reconciles current state.
    }
  };

  return (
    <section className="cs-member" data-testid="personal-chart-style-panel">
      <header className="cs-member__header">
        <div><span className="cs-kicker">{t("independent")}</span><h2>{t("title")}</h2><p>{t("personalDesc")}</p></div>
        <div className="cs-page__actions">
          <Button size="sm" onClick={() => void resetAll()} disabled={status === "saving"}>{t("resetPersonal")}</Button>
          {status !== "idle" ? <span className={`cs-save-state cs-save-state--${status}`} role="status">{status === "saving" ? t("saving") : status === "saved" || status === "refresh_failed" ? t("saved") : t("failedShort")}</span> : null}
        </div>
      </header>
      {error ? <Notice tone="error">{error}</Notice> : null}
      {status === "refresh_failed" || status === "error" ? <Notice tone="error">
        {status === "refresh_failed" ? t("savedRefreshFailed") : error ? null : t("saveUnconfirmed")}
        <Button size="sm" onClick={() => void refresh()}>{t("refreshStatus")}</Button>
      </Notice> : null}
      {effective.selection.unavailableReason ? <Notice tone="info">{t("unavailable")}</Notice> : null}
      {/* Selection immediately saves the personal chart style ⇒ arrows only move focus; Space / Enter / click selects. */}
      <RadioGroup
        className="cs-member-grid"
        orientation="both"
        activation="manual"
        aria-label={t("title")}
        value={selected ?? FOLLOW_TEAM}
        onValueChange={(value) => updateSelection(value === FOLLOW_TEAM ? null : value)}
      >
        <StyleCard
          value={FOLLOW_TEAM}
          selected={selected === null}
          title={t("followTeam")}
          description={v2("followTeamDescription")}
          colors={effective.modeColors.light.categoricalSeriesPalette}
          badge={v2("teamDefaultBadge")}
        />
        {options.map((option) => (
          <StyleCard
            key={option.id}
            value={option.id}
            selected={selected === option.id}
            title={option.kind === "builtIn" ? builtInName(option.id, t) : option.name}
            description={option.kind === "builtIn" ? builtInDescription(option.id, t) : option.description || v2("teamStyleDescription")}
            colors={option.resolvedPresetSnapshot.colors.light.categorical}
            badge={option.kind === "builtIn" ? v2("builtInBadge") : v2("teamStyleBadge")}
          />
        ))}
      </RadioGroup>

      <div className="cs-member__accessibility">
        <h3>{t("accessibility")}</h3>
        <SwitchRow
          label={t("contrast")}
          description={t("contrastDesc")}
          checked={accessibility.contrast === "high"}
          onChange={(checked) => updateAccessibility("contrast", checked ? "high" : "standard")}
        />
        <SwitchRow
          label={t("colorSafe")}
          description={t("colorSafeDesc")}
          checked={accessibility.colorVision === "color-safe"}
          onChange={(checked) => updateAccessibility("colorVision", checked ? "color-safe" : "standard")}
        />
      </div>

      <ChartStylePreview
        themeForMode={(mode) => resolveChartThemeForEffectiveV2(
          effective,
          chartThemeBaseForMode(mode),
          mode,
        )}
        view={
          effective.style.id !== effectiveSelectedId || status === "saving"
            ? "loading"
            : "candidate"
        }
      />
    </section>
  );
}

function StyleCard({
  value,
  selected,
  title,
  description,
  colors,
  badge,
}: {
  value: string;
  selected: boolean;
  title: string;
  description: string;
  colors: readonly string[];
  badge: string;
}) {
  return (
    <Radio value={value} className="cs-member-card" data-selected={selected || undefined}>
      <span className="cs-palette-strip" aria-hidden>{colors.slice(0, 6).map((color, index) => <i key={`${color}-${index}`} style={{ background: color }} />)}</span>
      <span className="cs-member-card__title"><strong>{title}</strong>{selected ? <Check size={15} aria-hidden /> : null}</span>
      <span className="cs-member-card__description">{description}</span>
      <span className="cs-member-card__badge">{badge}</span>
    </Radio>
  );
}

function SwitchRow({ label, description, checked, onChange }: { label: string; description: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <div className="cs-switch-row">
      <div><strong>{label}</strong><span>{description}</span></div>
      <Switch aria-label={label} checked={checked} data-checked={checked || undefined} onCheckedChange={onChange}><span /></Switch>
    </div>
  );
}

function builtInName(id: string, t: ReturnType<typeof useTranslations>): string {
  if (id === "clean-blue") return t("presets.cleanBlue");
  if (id === "analytical-neutral") return t("presets.analyticalNeutral");
  return t("presets.bowheadEditorial");
}

function builtInDescription(id: string, t: ReturnType<typeof useTranslations>): string {
  if (id === "clean-blue") return t("presets.cleanBlueDesc");
  if (id === "analytical-neutral") return t("presets.analyticalNeutralDesc");
  return t("presets.bowheadEditorialDesc");
}
