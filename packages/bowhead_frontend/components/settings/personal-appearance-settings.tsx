"use client";

import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useTranslations } from "next-intl";

import {
  AlertTriangle,
  Building,
  Check,
  ChevronDown,
  Moon,
  RefreshCw,
  Sun,
  UserIcon,
} from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import { Segment, SegmentedControl as UiSegmentedControl } from "@/components/ui/segmented-control";
import { PersonalChartStyleSettings } from "@/components/settings/chart-style-settings";
import { PersonalColorPicker } from "@/components/settings/personal-color-picker";
import { usePersonalAppearance } from "@/components/settings/personal-appearance-provider";
import { PersonalPaletteLibrary } from "@/components/settings/personal-palette-library";
import { fetchMe } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import type {
  PersonalAccent,
  PersonalAppearanceChange,
  PersonalAppearanceOverrides,
  PersonalAppearanceRegistryItem,
  PersonalAppearanceResource,
  PersonalAppearanceSource,
  PersonalBackground,
} from "@/lib/personal-appearance";
import {
  TEAM_SETTINGS_ROLES,
  teamRoleForUser,
} from "@/lib/settings-navigation";
import { appNavigationAllowed, navigateAppDocument } from "@/lib/use-app-router";
import {
  resolveThemeMode,
  type ThemeDensity,
  type ThemeMode,
  type ThemeMotion,
  type ThemeRadius,
  type TypographyProfileId,
  type TypeScale,
} from "@/lib/theme-config";

import styles from "./personal-appearance-settings.module.css";

type Option<T extends string> = {
  value: T;
  label: string;
  icon?: ReactNode;
};

export function PersonalAppearanceSettings() {
  const t = useTranslations("settings.appearance");
  const {
    snapshot,
    status,
    errorCode,
    pendingFields,
    failedFields,
    load,
    patch,
    compile,
    reset,
  } = usePersonalAppearance();
  const titleRef = useRef<HTMLHeadingElement | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    void load().catch(() => undefined);
    const focusFrame = window.requestAnimationFrame(() =>
      titleRef.current?.focus(),
    );
    return () => window.cancelAnimationFrame(focusFrame);
  }, [load]);

  const runPatch = useCallback(
    async (changes: PersonalAppearanceChange) => {
      await patch(changes);
    },
    [patch],
  );

  const controlsBusy = ["loading", "resetting", "undoing"].includes(status);
  const destructiveBusy = controlsBusy || status === "saving";
  const fieldStatus = (field: keyof PersonalAppearanceOverrides) =>
    pendingFields.has(field)
      ? ("saving" as const)
      : failedFields.has(field)
        ? ("error" as const)
        : undefined;

  if (!snapshot) {
    // While loading, replace only the content with grouped-card skeletons and keep the header (Page Layout Spec §4); failure shows the notice/retry.
    if (!errorCode) {
      return (
        <div className={`${styles.page} pa-page`}>
          <PageHeading titleRef={titleRef} />
          <LoadingSkeleton variant="form" rows={4} label={t("loadingPersonal")} />
        </div>
      );
    }
    return (
      <div className={styles.page}>
        <PageHeading titleRef={titleRef} />
        <div className={styles.loading} role={errorCode ? "alert" : "status"}>
          <span className={styles.loadingMark} aria-hidden />
          <strong>{errorCode ? t("loadFailed") : t("loadingPersonal")}</strong>
          {errorCode ? (
            <button type="button" onClick={() => void load(true)}>
              {t("saveState.retry")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  const resource = snapshot.resource;
  const resolvedMode = resolveThemeMode(resource.effective.mode);
  const baseIncompatible = resource.degradations.some(
    (item) => item.reason === "base_incompatible",
  );

  return (
    <div className={`${styles.page} pa-page`} data-testid="personal-appearance-v3">
      <PageHeading titleRef={titleRef} />

      {status === "conflict" || status === "error" ? (
        <div className={styles.issue} role="alert">
          <AlertTriangle size={16} aria-hidden />
          <span>{status === "conflict" ? t("conflict") : t("saveFailed")}</span>
          <button type="button" onClick={() => void load(true)}>
            <RefreshCw size={13} aria-hidden />
            {t("saveState.retry")}
          </button>
        </div>
      ) : null}

      {resource.degradations.length ? (
        <div className={styles.issue} role="status">
          <AlertTriangle size={16} aria-hidden />
          <span>{t(baseIncompatible ? "baseIncompatible" : "degraded")}</span>
        </div>
      ) : null}

      <AppearanceSection
        eyebrow={t("sections.themeEyebrow")}
        title={t("sections.theme")}
        description={t("sections.themeDesc")}
      >
        <PersonalPaletteLibrary
          resource={resource}
          etag={snapshot.etag}
          busy={controlsBusy}
          saving={
            pendingFields.has("accent") || pendingFields.has("background")
          }
          failed={failedFields.has("accent") || failedFields.has("background")}
          compile={compile}
          onApply={(palette) => runPatch(palette)}
        />

        <SettingRow
          testId="personal-appearance-mode"
          compact
          title={t("mode.title")}
          description={t("mode.desc")}
          source={resource.sources.mode}
          hasOverride={resource.overrides.mode !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("mode")}
          onClear={() => void runPatch({ mode: null }).catch(() => undefined)}
        >
          <SegmentedControl
            label={t("mode.title")}
            options={modeOptions(t)}
            value={resource.effective.mode}
            disabled={controlsBusy}
            onChange={(mode) => void runPatch({ mode }).catch(() => undefined)}
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-accent"
          compact
          title={t("accentV3.title")}
          description={t("accentV3.desc")}
          source={resource.sources.accent}
          hasOverride={resource.overrides.accent !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("accent")}
          onClear={() => void runPatch({ accent: null }).catch(() => undefined)}
        >
          <AccentChoices
            resource={resource}
            etag={snapshot.etag}
            mode={resolvedMode}
            busy={controlsBusy}
            compile={compile}
            onChange={(accent) => runPatch({ accent })}
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-background"
          title={t("background.title")}
          description={t("background.desc")}
          source={resource.sources.background}
          hasOverride={resource.overrides.background !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("background")}
          onClear={() =>
            void runPatch({ background: null }).catch(() => undefined)
          }
        >
          <BackgroundChoices
            resource={resource}
            etag={snapshot.etag}
            busy={controlsBusy}
            compile={compile}
            onChange={(background) => runPatch({ background })}
          />
        </SettingRow>

        <CurrentEffect resource={resource} mode={resolvedMode} />
      </AppearanceSection>

      <AppearanceSection
        eyebrow={t("sections.readingEyebrow")}
        title={t("sections.reading")}
        description={t("sections.readingDesc")}
      >
        <SettingRow
          testId="personal-appearance-typography"
          title={t("typography.title")}
          description={t("typography.desc")}
          source={resource.sources.typographyProfile}
          hasOverride={resource.overrides.typographyProfile !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("typographyProfile")}
          onClear={() =>
            void runPatch({ typographyProfile: null }).catch(() => undefined)
          }
        >
          <SegmentedControl
            label={t("typography.title")}
            options={typographyOptions(t)}
            value={resource.effective.typographyProfile.id}
            disabled={controlsBusy}
            onChange={(id) =>
              void runPatch({
                typographyProfile: { type: "preset", id },
              }).catch(() => undefined)
            }
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-type-scale"
          title={t("typeScale.title")}
          description={t("typeScale.desc")}
          source={resource.sources.typeScale}
          hasOverride={resource.overrides.typeScale !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("typeScale")}
          onClear={() =>
            void runPatch({ typeScale: null }).catch(() => undefined)
          }
        >
          <SegmentedControl
            label={t("typeScale.title")}
            options={typeScaleOptions(t)}
            value={resource.effective.typeScale}
            disabled={controlsBusy}
            onChange={(typeScale) =>
              void runPatch({ typeScale }).catch(() => undefined)
            }
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-density"
          title={t("density.title")}
          description={t("density.desc")}
          source={resource.sources.density}
          hasOverride={resource.overrides.density !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("density")}
          onClear={() =>
            void runPatch({ density: null }).catch(() => undefined)
          }
        >
          <SegmentedControl
            label={t("density.title")}
            options={densityOptions(t)}
            value={resource.effective.density}
            disabled={controlsBusy}
            onChange={(density) =>
              void runPatch({ density }).catch(() => undefined)
            }
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-radius"
          title={t("radius.title")}
          description={t("radius.desc")}
          source={resource.sources.radius}
          hasOverride={resource.overrides.radius !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("radius")}
          onClear={() => void runPatch({ radius: null }).catch(() => undefined)}
        >
          <SegmentedControl
            label={t("radius.title")}
            options={radiusOptions(t)}
            value={resource.effective.radius}
            disabled={controlsBusy}
            onChange={(radius) =>
              void runPatch({ radius }).catch(() => undefined)
            }
          />
        </SettingRow>

        <SettingRow
          testId="personal-appearance-motion"
          title={t("motion.title")}
          description={t("motion.desc")}
          source={resource.sources.motion}
          hasOverride={resource.overrides.motion !== undefined}
          busy={controlsBusy}
          saveStatus={fieldStatus("motion")}
          onClear={() => void runPatch({ motion: null }).catch(() => undefined)}
        >
          <SegmentedControl
            label={t("motion.title")}
            options={motionOptions(t)}
            value={resource.effective.motion}
            disabled={controlsBusy}
            onChange={(motion) =>
              void runPatch({ motion }).catch(() => undefined)
            }
          />
        </SettingRow>
      </AppearanceSection>

      <AppearanceSection
        eyebrow={t("sections.chartEyebrow")}
        title={t("sections.chart")}
        description={t("sections.chartDesc")}
      >
        <PersonalChartStyleSettings />
      </AppearanceSection>

      <section className={`${styles.dangerZone} pa-danger-zone`}>
        <div>
          <strong>{t("resetAll.title")}</strong>
          <p>{t("resetAll.desc")}</p>
        </div>
        <button
          type="button"
          disabled={destructiveBusy}
          onClick={() => setConfirmReset(true)}
        >
          {t("resetAll.action")}
        </button>
      </section>

      {confirmReset ? (
        <ResetConfirmation
          busy={status === "resetting"}
          onCancel={() => setConfirmReset(false)}
          onConfirm={async () => {
            await reset();
            setConfirmReset(false);
          }}
        />
      ) : null}
    </div>
  );
}

function PageHeading({
  titleRef,
}: {
  titleRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const t = useTranslations("settings.appearance");
  const [canViewTeamAppearance, setCanViewTeamAppearance] = useState(false);

  useEffect(() => {
    let active = true;
    void fetchMe()
      .then((me) => {
        const role = teamRoleForUser(me);
        if (active) {
          setCanViewTeamAppearance(
            Boolean(role && TEAM_SETTINGS_ROLES.includes(role)),
          );
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  return (
    <header className={`${styles.pageHeading} pa-heading`}>
      <span className={`${styles.scopeIcon} pa-scope-icon`} aria-hidden>
        <UserIcon size={16} />
      </span>
      <div>
        <h1 ref={titleRef} tabIndex={-1} className="ui-page-title">
          {t("titleV3")}
        </h1>
        <p className="ui-page-desc">{t("subtitleV3")}</p>
        {canViewTeamAppearance ? (
          <a
            className={`${styles.teamAppearanceLink} pa-team-link`}
            href={appPath("/team/settings/brand-appearance/")}
            onClick={(event) => {
              if (!shouldHandleAppLinkClick(event)) return;
              if (!appNavigationAllowed("/team/settings/brand-appearance/", () => navigateAppDocument("/team/settings/brand-appearance/"))) {
                event.preventDefault();
              }
            }}
          >
            <Building size={13} aria-hidden />
            {t("viewTeamAppearance")}
          </a>
        ) : null}
      </div>
    </header>
  );
}

function CurrentEffect({
  resource,
  mode,
}: {
  resource: PersonalAppearanceResource;
  mode: "light" | "dark";
}) {
  const t = useTranslations("settings.appearance");
  const tokens = resource.effective.colorScheme[mode];
  const previewStyle = {
    "--preview-page": tokens.background,
    "--preview-surface": tokens.surface,
    "--preview-line": tokens.line,
    "--preview-ink": tokens.ink,
    "--preview-ink-2": tokens.ink2,
    "--preview-accent": tokens.accent,
    "--preview-accent-ink": tokens.accentInk,
    "--preview-focus": tokens.focus,
  } as CSSProperties;
  return (
    <section
      className={`${styles.currentEffect} pa-current-effect`}
      style={previewStyle}
      data-testid="personal-appearance-preview"
    >
      <div className={styles.currentEffectHead}>
        <div>
          <span>{t("currentEffect.eyebrow")}</span>
          <strong>{t("currentEffect.title")}</strong>
        </div>
        <span className={styles.modeBadge}>
          {mode === "dark" ? <Moon size={13} /> : <Sun size={13} />}
          {t(`mode.options.${mode}`)}
        </span>
      </div>
      <div className={styles.previewCanvas}>
        <div className={styles.previewSurface}>
          <div>
            <strong>{t("currentEffect.sampleTitle")}</strong>
            <span>{t("currentEffect.sampleBody")}</span>
          </div>
          <span className={styles.previewAction}>
            {t("currentEffect.action")}
          </span>
          <span className={styles.previewLink}>{t("currentEffect.link")}</span>
          <span className={styles.focusSample}>{t("currentEffect.focus")}</span>
        </div>
      </div>
    </section>
  );
}

function AppearanceSection({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <section className={`${styles.section} pa-section`}>
      <header className={`${styles.sectionHeading} pa-section-heading`}>
        {eyebrow ? <span>{eyebrow}</span> : null}
        <h2>{title}</h2>
        <p>{description}</p>
      </header>
      <div className={`${styles.sectionBody} pa-section-body`}>{children}</div>
    </section>
  );
}

function SettingRow({
  testId,
  compact = false,
  title,
  description,
  source,
  hasOverride,
  busy,
  saveStatus,
  onClear,
  children,
}: {
  testId?: string;
  compact?: boolean;
  title: string;
  description: string;
  source: PersonalAppearanceSource;
  hasOverride: boolean;
  busy: boolean;
  saveStatus?: "saving" | "error";
  onClear: () => void;
  children: ReactNode;
}) {
  const t = useTranslations("settings.appearance");
  return (
    <div
      className={`${styles.settingRow} pa-setting-row`}
      data-testid={testId}
      data-compact={compact || undefined}
    >
      <div className={`${styles.settingHead} pa-setting-head`}>
        <div>
          <h3>{title}</h3>
          <p>{description}</p>
        </div>
        <div className={`${styles.inheritance} pa-inheritance`}>
          {saveStatus ? (
            <span
              className={styles.fieldStatus}
              data-state={saveStatus}
              aria-live="polite"
            >
              {t(
                saveStatus === "saving"
                  ? "saveState.fieldSaving"
                  : "saveState.fieldFailed",
              )}
            </span>
          ) : null}
          <span>
            {t("inheritance.current", { source: sourceLabel(t, source) })}
          </span>
          {hasOverride ? (
            <button type="button" disabled={busy} onClick={onClear}>
              {t("inheritance.clear")}
            </button>
          ) : null}
        </div>
      </div>
      {children}
    </div>
  );
}

/**
 * Personal-appearance radio segments (role="radiogroup"). Selection immediately saves (PATCH personal appearance and relayout the whole page), so arrows only move
 * focus; Space / Enter / click select, or navigating System → Dark would first persist Light and flash the whole page.
 * Use busy while saving: options remain focusable.
 */
function SegmentedControl<T extends string>({
  label,
  options,
  value,
  disabled,
  onChange,
}: {
  /** Group accessible name (this row's heading): radiogroup needs a name so screen readers identify which setting is selected. */
  label: string;
  options: Option<T>[];
  value: T;
  disabled: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <UiSegmentedControl<T>
      className={styles.segmented}
      aria-label={label}
      activation="manual"
      value={value}
      busy={disabled}
      onValueChange={(next) => {
        if (next !== value) onChange(next);
      }}
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <Segment key={option.value} value={option.value}>
            {option.icon}
            {option.label}
            {selected ? <Check size={12} aria-hidden /> : null}
          </Segment>
        );
      })}
    </UiSegmentedControl>
  );
}

function AccentChoices({
  resource,
  etag,
  mode,
  busy,
  compile,
  onChange,
}: {
  resource: PersonalAppearanceResource;
  etag: string;
  mode: "light" | "dark";
  busy: boolean;
  compile: ReturnType<typeof usePersonalAppearance>["compile"];
  onChange: (accent: PersonalAccent | null) => Promise<void>;
}) {
  const t = useTranslations("settings.appearance");
  const current = resource.overrides.accent;
  const customSeed =
    current?.kind === "custom"
      ? current.seed
      : resource.effective.colorScheme[mode].accent;
  const buildCandidate = useCallback(
    (hex: string): PersonalAppearanceOverrides => ({
      ...resource.overrides,
      accent: { kind: "custom", seed: hex },
    }),
    [resource.overrides],
  );
  const recommendations = useMemo(
    () =>
      resource.registries.accent.items.flatMap((item) => {
        const value = item.swatch[mode]?.primary;
        return item.availability === "available" && value ? [value] : [];
      }),
    [mode, resource.registries.accent.items],
  );

  return (
    <div className={styles.choiceStack}>
      <div className={styles.accentGrid}>
        {resource.registries.accent.items.map((item) => (
          <AccentChoice
            key={item.id}
            item={item}
            selected={current?.kind === "preset" && current.id === item.id}
            disabled={busy || item.availability !== "available"}
            onClick={() => void onChange(item.value)}
          />
        ))}
      </div>
      <PersonalColorPicker
        label={t("accentV3.custom")}
        value={customSeed}
        mode={mode}
        target="accent"
        etag={etag}
        recommendations={recommendations}
        disabled={busy}
        buildCandidate={buildCandidate}
        compile={compile}
        onApply={(hex) => onChange({ kind: "custom", seed: hex })}
      />
      {/* The picker above displays the entered seed when customized, the compiled result when inherited—
          one location with two meanings. Separate them into the three boxes of team-branding-v2-r2,
          sharing the team-brand editor's presentation. */}
      <div className={styles.seedTrio}>
        {(
          [
            ...(current?.kind === "custom"
              ? ([[t("accentV3.brandInput"), current.seed, true]] as Array<
                  [string, string, boolean]
                >)
              : []),
            [
              t("accentV3.lightActual"),
              resource.effective.colorScheme.light.accent,
              false,
            ],
            [
              t("accentV3.darkActual"),
              resource.effective.colorScheme.dark.accent,
              false,
            ],
          ] as Array<[string, string, boolean]>
        ).map(([label, hex, isSeed]) => (
          <div
            key={label}
            className={`${styles.seedCell}${isSeed ? ` ${styles.seedCellInput}` : ""}`}
          >
            <span>{label}</span>
            <span>
              <i style={{ background: hex }} aria-hidden />
              <code>{hex}</code>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AccentChoice({
  item,
  selected,
  disabled,
  onClick,
}: {
  item: PersonalAppearanceRegistryItem<PersonalAccent>;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("settings.appearance");
  const light = item.swatch.light;
  const dark = item.swatch.dark;
  return (
    <button
      type="button"
      className={styles.accentChoice}
      aria-pressed={selected}
      disabled={disabled}
      onClick={() => {
        if (!selected) onClick();
      }}
    >
      <span className={styles.accentDemo} aria-hidden>
        <i style={{ background: light?.primary }} />
        <b style={{ color: light?.primary }}>{t("accentV3.linkSample")}</b>
        <em style={{ boxShadow: `0 0 0 2px ${dark?.primary}` }} />
      </span>
      <span>{registryLabel(t, item.id, "accent")}</span>
      {selected ? (
        <small>
          <Check size={11} aria-hidden /> {t("selected")}
        </small>
      ) : null}
    </button>
  );
}

function BackgroundChoices({
  resource,
  etag,
  busy,
  compile,
  onChange,
}: {
  resource: PersonalAppearanceResource;
  etag: string;
  busy: boolean;
  compile: ReturnType<typeof usePersonalAppearance>["compile"];
  onChange: (background: PersonalBackground | null) => Promise<void>;
}) {
  const t = useTranslations("settings.appearance");
  const current = resource.overrides.background;
  return (
    <div className={styles.choiceStack}>
      <div className={styles.backgroundGrid}>
        {resource.registries.background.items.map((item) => (
          <BackgroundChoice
            key={item.id}
            item={item}
            selected={current?.kind === "preset" && current.id === item.id}
            disabled={busy || item.availability !== "available"}
            onClick={() => void onChange(item.value)}
          />
        ))}
      </div>
      <CustomBackgroundEditor
        key={etag}
        resource={resource}
        etag={etag}
        busy={busy}
        compile={compile}
        onChange={onChange}
      />
    </div>
  );
}

function BackgroundChoice({
  item,
  selected,
  disabled,
  onClick,
}: {
  item: PersonalAppearanceRegistryItem<PersonalBackground>;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("settings.appearance");
  return (
    <button
      type="button"
      className={styles.backgroundChoice}
      aria-pressed={selected}
      disabled={disabled}
      onClick={() => {
        if (!selected) onClick();
      }}
    >
      <span className={styles.backgroundDemo} aria-hidden>
        {(["light", "dark"] as const).map((mode) => (
          <i
            key={mode}
            style={{
              background: item.swatch[mode]?.primary,
              color: item.swatch[mode]?.onPrimary,
            }}
          >
            <b style={{ background: item.swatch[mode]?.secondary }} />
            Aa
          </i>
        ))}
      </span>
      <span>{registryLabel(t, item.id, "background")}</span>
      {selected ? (
        <small>
          <Check size={11} aria-hidden /> {t("selected")}
        </small>
      ) : null}
    </button>
  );
}

function CustomBackgroundEditor({
  resource,
  etag,
  busy,
  compile,
  onChange,
}: {
  resource: PersonalAppearanceResource;
  etag: string;
  busy: boolean;
  compile: ReturnType<typeof usePersonalAppearance>["compile"];
  onChange: (background: PersonalBackground) => Promise<void>;
}) {
  const t = useTranslations("settings.appearance");
  const current = resource.overrides.background;
  const [expanded, setExpanded] = useState(current?.kind === "custom");
  const [colors, setColors] = useState(() =>
    current?.kind === "custom"
      ? { light: current.light, dark: current.dark }
      : {
          light: resource.effective.colorScheme.light.background,
          dark: resource.effective.colorScheme.dark.background,
        },
  );
  const recommendation = useCallback(
    (mode: "light" | "dark") =>
      resource.registries.background.items.flatMap((item) => {
        const value = item.swatch[mode]?.primary;
        return item.availability === "available" && value ? [value] : [];
      }),
    [resource.registries.background.items],
  );
  const buildLightCandidate = useCallback(
    (hex: string): PersonalAppearanceOverrides => ({
      ...resource.overrides,
      background: { kind: "custom", light: hex, dark: colors.dark },
    }),
    [colors.dark, resource.overrides],
  );
  const buildDarkCandidate = useCallback(
    (hex: string): PersonalAppearanceOverrides => ({
      ...resource.overrides,
      background: { kind: "custom", light: colors.light, dark: hex },
    }),
    [colors.light, resource.overrides],
  );

  const apply = async (mode: "light" | "dark", hex: string) => {
    const next = { ...colors, [mode]: hex };
    await onChange({ kind: "custom", ...next });
    setColors(next);
  };

  return (
    <div className={styles.customBackground}>
      <button
        type="button"
        className={styles.customBackgroundToggle}
        aria-expanded={expanded}
        aria-controls="personal-custom-background-controls"
        onClick={() => setExpanded((currentExpanded) => !currentExpanded)}
      >
        <span>
          <strong>{t("background.custom")}</strong>
          <small>{t("background.customDesc")}</small>
        </span>
        <ChevronDown size={15} aria-hidden />
      </button>
      {expanded ? (
        <div
          id="personal-custom-background-controls"
          className={styles.customBackgroundPickers}
        >
          <PersonalColorPicker
            label={t("background.light")}
            value={colors.light}
            mode="light"
            target="background"
            etag={etag}
            recommendations={recommendation("light")}
            disabled={busy}
            buildCandidate={buildLightCandidate}
            compile={compile}
            onApply={(hex) => apply("light", hex)}
          />
          <PersonalColorPicker
            label={t("background.dark")}
            value={colors.dark}
            mode="dark"
            target="background"
            etag={etag}
            recommendations={recommendation("dark")}
            disabled={busy}
            buildCandidate={buildDarkCandidate}
            compile={compile}
            onApply={(hex) => apply("dark", hex)}
          />
        </div>
      ) : null}
    </div>
  );
}

function ResetConfirmation({
  busy,
  onCancel,
  onConfirm,
}: {
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const t = useTranslations("settings.appearance.resetAll");
  return (
    <DialogShell
      onClose={onCancel}
      busy={busy}
      closeOnBackdrop={false}
      labelledBy="appearance-reset-title"
      backdropClassName={styles.confirmBackdrop}
      panelClassName={styles.confirmDialog}
    >
      <span className={styles.confirmIcon} aria-hidden>
        <RefreshCw size={18} />
      </span>
      <h2 id="appearance-reset-title">{t("confirmTitle")}</h2>
      <p>{t("confirmDesc")}</p>
      <ul>
        <li>{t("themeItems")}</li>
        <li>{t("readingItems")}</li>
      </ul>
      <small>{t("excluded")}</small>
      <footer>
        <button
          data-dialog-initial-focus=""
          type="button"
          disabled={busy}
          onClick={onCancel}
        >
          {t("cancel")}
        </button>
        <button
          type="button"
          className={styles.confirmAction}
          disabled={busy}
          onClick={() => void onConfirm().catch(() => undefined)}
        >
          {busy ? t("clearing") : t("confirm")}
        </button>
      </footer>
    </DialogShell>
  );
}

function modeOptions(t: Translator): Option<ThemeMode>[] {
  return [
    { value: "system", label: t("mode.options.system") },
    { value: "light", label: t("mode.options.light"), icon: <Sun size={14} /> },
    { value: "dark", label: t("mode.options.dark"), icon: <Moon size={14} /> },
  ];
}

function typographyOptions(t: Translator): Option<TypographyProfileId>[] {
  return [
    { value: "bowhead", label: t("typography.options.bowhead") },
    { value: "system", label: t("typography.options.system") },
    { value: "readable", label: t("typography.options.readable") },
    { value: "serif-display", label: t("typography.options.serifDisplay") },
  ];
}

function typeScaleOptions(t: Translator): Option<TypeScale>[] {
  return ["compact", "standard", "large", "extra-large"].map((value) => ({
    value: value as TypeScale,
    label: t(
      `typeScale.options.${value === "extra-large" ? "extraLarge" : value}`,
    ),
  }));
}

function densityOptions(t: Translator): Option<ThemeDensity>[] {
  return ["compact", "comfortable", "spacious"].map((value) => ({
    value: value as ThemeDensity,
    label: t(`density.options.${value}`),
  }));
}

function radiusOptions(t: Translator): Option<ThemeRadius>[] {
  return ["sharp", "balanced", "soft"].map((value) => ({
    value: value as ThemeRadius,
    label: t(`radius.options.${value}`),
  }));
}

function motionOptions(t: Translator): Option<ThemeMotion>[] {
  return ["standard", "reduced"].map((value) => ({
    value: value as ThemeMotion,
    label: t(`motion.options.${value}`),
  }));
}

type Translator = ReturnType<typeof useTranslations<"settings.appearance">>;

function sourceLabel(t: Translator, source: PersonalAppearanceSource): string {
  return t(`inheritance.${source}`);
}

function registryLabel(
  t: Translator,
  id: string,
  kind: "accent" | "background",
): string {
  const keys: Record<string, string> =
    kind === "accent"
      ? {
          bowhead: "accent.options.default",
          ocean: "accent.options.ocean",
          forest: "accent.options.forest",
          plum: "accent.options.plum",
          graphite: "accent.options.graphite",
        }
      : {
          "warm-paper": "background.options.warmPaper",
          "pure-white": "background.options.pureWhite",
          "cool-gray": "background.options.coolGray",
        };
  const key = keys[id];
  return key ? t(key) : id;
}
