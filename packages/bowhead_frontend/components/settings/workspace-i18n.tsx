"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { getWorkspaceSettings, setWorkspaceSetting, type WorkspaceSettings } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { Switch } from "@/components/ui/switch";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { CanvasSection, FieldRow, SectionHeading } from "@/components/ui/section-heading";
import { getActiveTeamId } from "@/lib/team-storage";
import { WORKSPACE_SETTINGS_SAVED_EVENT, type WorkspaceSettingsSavedTarget } from "@/lib/workspace-settings-notifications";
import type { LiteActionReceipt } from "@/lib/config-copilot-lite";

export interface WorkspaceI18nLifecycle {
  workspaceId: string;
  savedValues: Readonly<WorkspaceSettings> | null;
  localValues: Readonly<WorkspaceSettings>;
  unconfirmedFields: readonly string[];
  editGeneration: number;
  pendingWrites: number;
  error: string | null;
}

export interface WorkspaceI18nAgentInterface {
  requestReadback: (receipt: LiteActionReceipt) => Promise<boolean>;
}

interface WorkspaceI18nPageProps {
  workspaceId: string;
  workspaceName?: string;
  onLifecycleChange?: (state: WorkspaceI18nLifecycle) => void;
  onAgentInterfaceChange?: (api: WorkspaceI18nAgentInterface | null) => void;
  onAccessDenied?: () => void;
  /**
   * Rendered inside a Space settings Canvas object (M6). `section` adds the “Preferences” section heading
   * (inside 常规); the standalone preferences page already carries that title in its object header.
   */
  hosted?: boolean | "section";
}

export function SettingsWorkspaceI18n(props: WorkspaceI18nPageProps) {
  return <WorkspaceI18nPage key={props.workspaceId} {...props} />;
}

/**
 * i18n v1 · Workspace admin settings — data_language / week_start_day / i18n_v1 flag.
 */
function WorkspaceI18nPage({ workspaceId, workspaceName, onLifecycleChange, onAgentInterfaceChange, onAccessDenied, hosted = false }: WorkspaceI18nPageProps) {
  const t = useTranslations("settings.workspace_i18n");
  const [settings, setSettings] = useState<WorkspaceSettings>({});
  const [savedSettings, setSavedSettings] = useState<WorkspaceSettings>({});
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [savedFlag, setSavedFlag] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);
  const accessLost = useRef(false);
  const [pendingWrites, setPendingWrites] = useState(0);
  const [editGeneration, setEditGeneration] = useState(0);
  const mounted = useRef(false);
  const generation = useRef(0);
  const pending = useRef(0);
  const readTicket = useRef(0);
  const refreshNeeded = useRef(false);
  const localEdits = useRef(new Map<string, { generation: number; value: string }>());
  const writes = useRef<Promise<void>>(Promise.resolve());
  const denyAccess = useCallback(() => {
    accessLost.current = true;
    readTicket.current += 1;
    setAccessDenied(true);
    setHydrated(false);
    setSettings({});
    setSavedSettings({});
    localEdits.current.clear();
    onAccessDenied?.();
  }, [onAccessDenied]);

  const refreshSaved = useCallback(async (): Promise<void> => {
    if (accessLost.current) return;
    if (pending.current > 0) {
      refreshNeeded.current = true;
      return;
    }
    refreshNeeded.current = false;
    const ticket = ++readTicket.current;
    const originalGeneration = generation.current;
    try {
      const values = await getWorkspaceSettings(workspaceId);
      if (!mounted.current || ticket !== readTicket.current) return;
      if (pending.current > 0 || generation.current !== originalGeneration) {
        refreshNeeded.current = true;
        if (pending.current === 0) void refreshSaved();
        return;
      }
      setSavedSettings(values);
      setSettings({ ...values, ...Object.fromEntries([...localEdits.current].map(([key, edit]) => [key, edit.value])) });
      setHydrated(true);
    } catch (error) {
      if (mounted.current && ticket === readTicket.current) {
        setErr((error as Error).message);
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) denyAccess();
      }
    } finally {
      if (mounted.current && ticket === readTicket.current) setLoading(false);
    }
  }, [workspaceId, denyAccess]);

  useEffect(() => {
    mounted.current = true;
    accessLost.current = false;
    setSettings({});
    setLoading(true);
    setErr(null);
    void refreshSaved();
    const onSaved = (event: Event) => {
      const target = (event as CustomEvent<WorkspaceSettingsSavedTarget>).detail;
      if (!target?.teamId || target.workspaceId !== workspaceId) return;
      const currentTeam = getActiveTeamId();
      if (currentTeam && currentTeam !== target.teamId) return;
      // The original API rechecks authorization; this pointer carries no values.
      void refreshSaved();
    };
    window.addEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, onSaved);
    return () => {
      mounted.current = false;
      readTicket.current += 1;
      window.removeEventListener(WORKSPACE_SETTINGS_SAVED_EVENT, onSaved);
    };
  }, [workspaceId, refreshSaved]);

  useEffect(() => {
    onAgentInterfaceChange?.({
      requestReadback: async (receipt) => {
        if (
          !mounted.current ||
          receipt.form_id !== "workspace_i18n" ||
          receipt.workspace_id !== workspaceId ||
          !receipt.team_id ||
          !receipt.owner_user_id ||
          !["applied", "reverted"].includes(receipt.status)
        )
          return false;
        const currentTeam = getActiveTeamId();
        if (currentTeam && currentTeam !== receipt.team_id) return false;
        await refreshSaved();
        return true;
      },
    });
    return () => onAgentInterfaceChange?.(null);
  }, [workspaceId, refreshSaved, onAgentInterfaceChange]);

  useEffect(() => {
    onLifecycleChange?.({
      workspaceId,
      savedValues: hydrated ? savedSettings : null,
      localValues: settings,
      unconfirmedFields: [...localEdits.current.keys()],
      editGeneration,
      pendingWrites,
      error: err,
    });
  }, [workspaceId, onLifecycleChange, hydrated, savedSettings, settings, editGeneration, pendingWrites, err]);

  const update = async (key: string, value: string) => {
    generation.current += 1;
    const edit = generation.current;
    localEdits.current.set(key, { generation: edit, value });
    setSettings((s) => ({ ...s, [key]: value }));
    pending.current += 1;
    setEditGeneration(generation.current);
    setPendingWrites(pending.current);
    setErr(null);
    setSavedFlag(null);
    try {
      const save = writes.current.then(() => {
        if (!mounted.current || accessLost.current) throw new Error("The settings page is unavailable for this save.");
        return setWorkspaceSetting(workspaceId, key, value);
      });
      writes.current = save.catch(() => undefined);
      await save;
      if (!mounted.current) return;
      setSavedSettings((s) => ({ ...s, [key]: value }));
      if (localEdits.current.get(key)?.generation === edit) {
        localEdits.current.delete(key);
        setSettings((s) => ({ ...s, [key]: value }));
      }
      if (localEdits.current.size === 0) setErr(null);
      if (pending.current === 1 && localEdits.current.size === 0) {
        setSavedFlag(t("saved"));
        setTimeout(() => setSavedFlag(null), 2000);
      }
    } catch (e) {
      if (mounted.current) {
        setErr((e as Error).message);
        if (e instanceof ApiError && [401, 403, 404].includes(e.status)) denyAccess();
      }
    } finally {
      pending.current -= 1;
      if (mounted.current) {
        setPendingWrites(pending.current);
        if (pending.current === 0 && refreshNeeded.current) void refreshSaved();
      }
    }
  };

  if (accessDenied) return <Notice tone="error">{err}</Notice>;

  // Hosted in Canvas (M6): one “Preferences” section with the two user-facing preferences as segmented
  // rows; the rollout switch and the interface-language default stay on the full settings page.
  if (hosted) {
    return (
      <CanvasSection labelledBy={hosted === "section" ? `ws-i18n-${workspaceId}-heading` : undefined} label={hosted === "section" ? undefined : t("hosted_section_title")}>
        {hosted === "section" ? <SectionHeading id={`ws-i18n-${workspaceId}-heading`} title={t("hosted_section_title")} /> : null}
        {err ? <Notice tone="error">{err}</Notice> : null}
        {loading ? (
          <LoadingSkeletonFallback label={t("loading")} />
        ) : (
          <div>
            <WorkspaceI18nFields settings={settings} variant="segmented" fields={HOSTED_FIELDS} onChange={(key, value) => void update(key, value)} />
            {savedFlag ? <div role="status" aria-live="polite" className="ui-ws-i18n-saved">{savedFlag}</div> : null}
          </div>
        )}
      </CanvasSection>
    );
  }

  // Page-by-page acceptance A: headings and descriptions sit outside the cards, one item per card row (label left, control right),
  // and the feature toggle is an immediately effective Switch.
  return (
    <div className="legacy-ws-i18n ui-group ui-ws-i18n-group">
      <h3 className="legacy-ws-i18n-title ui-group-title">{t("title")}</h3>
      <p className="legacy-ws-i18n-subtitle ui-group-desc">{t("subtitle")}</p>

      <div className="legacy-ws-i18n-current ui-ws-i18n-current">
        {t("current_workspace")}
        {/* dogfood-0708 F8: fifth site missed by #16—previously showed bare ws_xxx to users (request about settings-page
            internal terminology). Show the name instead and move the raw ID to hover title (same treatment as #16 list rows/dropdown). */}
        <code title={workspaceId} className="legacy-ws-i18n-current-name ui-ws-i18n-current-name">
          {workspaceName || workspaceId}
        </code>
      </div>

      {err ? <Notice tone="error">{err}</Notice> : null}
      {loading ? (
        <LoadingSkeletonFallback label={t("loading")} />
      ) : (
        <div className="legacy-ws-i18n-card ui-group-card ui-ws-i18n-card">
          <WorkspaceI18nFields settings={settings} onChange={(key, value) => void update(key, value)} />

          {savedFlag ? (
            <div role="status" aria-live="polite" className="legacy-ws-i18n-saved ui-ws-i18n-saved">
              {savedFlag}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

export type WorkspaceI18nField = "i18n_v1" | "default_data_language" | "default_ui_language" | "week_start_day";
const ALL_FIELDS: readonly WorkspaceI18nField[] = ["i18n_v1", "default_data_language", "default_ui_language", "week_start_day"];
const HOSTED_FIELDS: readonly WorkspaceI18nField[] = ["week_start_day", "default_data_language"];

/** Controlled domain fields: hydration never calls a save callback. */
export function WorkspaceI18nFields({
  settings,
  onChange,
  fields = ALL_FIELDS,
  readOnly = false,
  variant = "select",
}: {
  settings: WorkspaceSettings;
  onChange: (key: WorkspaceI18nField, value: string) => void;
  fields?: readonly WorkspaceI18nField[];
  readOnly?: boolean;
  /** `segmented`: Canvas rows (C6) with a segmented control, in the order given by `fields`. */
  variant?: "select" | "segmented";
}) {
  const t = useTranslations("settings.workspace_i18n");
  const i18nEnabled = settings.i18n_v1 === "true";
  if (variant === "segmented") {
    return (
      <>
        {fields.map((field) => field === "week_start_day" ? (
          <FieldRow key={field} title={t("hosted_week_label")} control={(
            <SegmentedControl aria-label={t("hosted_week_label")} busy={readOnly} value={settings.week_start_day ?? "monday"}
              onValueChange={(value) => onChange("week_start_day", value)}>
              <Segment value="monday">{t("week_monday_short")}</Segment>
              <Segment value="sunday">{t("week_sunday_short")}</Segment>
            </SegmentedControl>
          )} />
        ) : field === "default_data_language" ? (
          <FieldRow key={field} title={t("hosted_data_lang_label")} control={(
            <SegmentedControl aria-label={t("hosted_data_lang_label")} busy={readOnly} value={settings.default_data_language ?? "zh-CN"}
              onValueChange={(value) => onChange("default_data_language", value)}>
              <Segment value="zh-CN">{t("lang_zh")}</Segment>
              <Segment value="en-US">{t("lang_en")}</Segment>
            </SegmentedControl>
          )} />
        ) : null)}
      </>
    );
  }
  return (
    <>
      {fields.includes("i18n_v1") ? (
        <Field label={t("field_flag_label")} hint={t("field_flag_hint")}>
          <span className="ui-ws-i18n-switch">
            <Switch
              checked={i18nEnabled}
              disabled={readOnly}
              aria-label={t("field_flag_label")}
              onCheckedChange={(checked) => onChange("i18n_v1", checked ? "true" : "false")}
            />
            <span className="ui-ws-i18n-switch-state">{i18nEnabled ? t("flag_on") : t("flag_off")}</span>
          </span>
        </Field>
      ) : null}

      {fields.includes("default_data_language") ? (
        <Field label={t("field_data_lang_label")} hint={t("field_data_lang_hint")}>
          <select
            aria-label={t("field_data_lang_label")}
            disabled={readOnly}
            value={settings.default_data_language ?? "zh-CN"}
            onChange={(e) => onChange("default_data_language", e.target.value)}
            className="ui-input legacy-input-workspace-i18n"
          >
            <option value="zh-CN">{t("lang_zh")}</option>
            <option value="en-US">{t("lang_en")}</option>
          </select>
        </Field>
      ) : null}

      {fields.includes("default_ui_language") ? (
        <Field label={t("field_ui_lang_label")} hint={t("field_ui_lang_hint")}>
          <select
            aria-label={t("field_ui_lang_label")}
            disabled={readOnly}
            value={settings.default_ui_language ?? "en-US"}
            onChange={(e) => onChange("default_ui_language", e.target.value)}
            className="ui-input legacy-input-workspace-i18n"
          >
            <option value="zh-CN">{t("lang_zh")}</option>
            <option value="en-US">{t("lang_en")}</option>
            <option value="de-DE">{t("lang_de")}</option>
            <option value="fr-FR">{t("lang_fr")}</option>
          </select>
        </Field>
      ) : null}

      {fields.includes("week_start_day") ? (
        <Field label={t("field_week_label")} hint={t("field_week_hint")}>
          <select
            aria-label={t("field_week_label")}
            disabled={readOnly}
            value={settings.week_start_day ?? "monday"}
            onChange={(e) => onChange("week_start_day", e.target.value)}
            className="ui-input legacy-input-workspace-i18n"
          >
            <option value="monday">{t("week_monday")}</option>
            <option value="sunday">{t("week_sunday")}</option>
          </select>
        </Field>
      ) : null}
    </>
  );
}

function LoadingSkeletonFallback({ label }: { label: string }) {
  return <LoadingSkeleton variant="form" rows={4} label={label} />;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="legacy-ws-i18n-field ui-group-row ui-ws-i18n-row">
      <label className="legacy-ws-i18n-label ui-group-label">{label}</label>
      {children}
      {hint ? <div className="legacy-ws-i18n-hint ui-group-hint">{hint}</div> : null}
    </div>
  );
}
