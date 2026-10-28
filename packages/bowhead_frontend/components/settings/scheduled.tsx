"use client";

import { isNetworkFailure } from "@/lib/transport-error";

/**
 * Settings · Scheduled Tasks / Routines — named natural-language queries + schedules + notification settings,
 * automatically executed in the cloud on schedule to produce an analysis.
 *
 * Interaction and visual requirements:
 *   list (status dots/badges/readable schedule/next run+relative time/failure badges/inline toggle·run now·edit·delete·expand history)
 *   · empty / loading skeleton / error states
 *   · create/edit right drawer (name·query·space·schedule presets+advanced cron·timezone·execution mode·notification email chips)
 *   · run history (expand to fetch runs)
 *   · mobile (<640px cards + full-width sheet form)
 *
 * Existing backend APIs: lib/api.ts fetchScheduledJobs / createScheduledJob / patchScheduledJob /
 * deleteScheduledJob / fetchScheduledJobRuns / runScheduledJobNow.
 *
 * Fetch/state patterns align with members.tsx (useState+useEffect, loading/error/empty, optimistic updates).
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocale, useTranslations } from "next-intl";

import { ErrorNotice } from "@/components/error-notice";
import { entitlementErrorMessage } from "@/lib/entitlement-error";
import type { Locale } from "@/lib/locale";
import {
  createScheduledJob,
  deleteScheduledJob,
  fetchBindableWorkflows,
  fetchScheduledJobRuns,
  fetchScheduledJobs,
  patchScheduledJob,
  runScheduledJobNow,
  type BindableWorkflowDTO,
  type BindableWorkflowForm,
  type ScheduledJobDTO,
  type ScheduledJobRunDTO,
  type ScheduledRunStatus,
  type ScheduledSessionStrategy,
  type WorkspaceDTO,
} from "@/lib/api";
import { readSpaces } from "@/lib/use-space";
import { viewerTimeZone } from "@/lib/time-zone";
import { ApiError } from "@/lib/api-client";
import { appPath } from "@/lib/base-path";
import {
  normalizeErrorDiagnostics,
  parseErrorDiagnosticsWire,
  projectTransportFailure,
  toErrorPresentation,
  type ErrorPresentation,
} from "@/lib/error-presentation";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Switch } from "@/components/ui/switch";
import {
  AlertTriangle,
  Briefcase,
  Building,
  Calendar,
  CalendarClock,
  Check,
  ChevronDown,
  Clock,
  ExternalLink,
  Hand,
  Info,
  Layers,
  Mail,
  Plus,
  Lock,
  Repeat,
  Search,
  Sparkles,
  Sun,
  Terminal,
  Undo,
  X,
} from "@/components/icons";
import { AiFillPanel } from "@/components/config-copilot/ai-fill-panel";
import { type AiFillApplyReceipt } from "@/components/config-copilot/ai-fill-panel";
import { type ConfigDraftResult } from "@/lib/config-drafts-api";
import { useCopilotFields } from "@/lib/use-copilot-fields";

const MAX_JOBS = 10;
const MAX_EMAILS = 5;
const NAME_MAX = 60;
const RUNNING_POLL_MS = 6000;

type ScheduleT = (key: string, values?: Record<string, string | number>) => string;
type ScheduledOperation =
  | "load"
  | "create"
  | "update"
  | "toggle"
  | "delete"
  | "runs"
  | "run";

export const SCHEDULED_ERROR_MESSAGE_KEYS: Readonly<Record<string, string>> = {
  authz_unavailable: "errors.authz_unavailable",
  authz_lookup_failed: "errors.authz_lookup_failed",
  clock_tamper: "errors.clock_tamper",
  feature_unlicensed: "errors.feature_unlicensed",
  insufficient_role: "errors.insufficient_role",
  needs_license: "errors.needs_license",
  orchestrator_not_ready: "errors.orchestrator_not_ready",
  over_grace: "errors.over_grace",
  request_validation_failed: "errors.invalid_configuration",
  scheduled_anchor_session_deleted: "errors.anchor_session_deleted",
  scheduled_datarc_context_failed: "errors.datarc_context_failed",
  scheduled_invalid_cron: "errors.invalid_cron",
  scheduled_job_create_failed: "errors.create_failed",
  scheduled_job_delete_failed: "errors.delete_failed",
  scheduled_job_limit_reached: "errors.job_limit_reached",
  scheduled_job_load_failed: "errors.load_failed",
  scheduled_job_not_found: "errors.job_not_found",
  scheduled_job_run_active: "errors.job_run_active",
  scheduled_job_update_failed: "errors.update_failed",
  scheduled_owner_lost_datarc_access: "errors.owner_lost_datarc_access",
  scheduled_public_entry_mismatch: "errors.public_entry_mismatch",
  scheduled_run_conflict: "errors.run_conflict",
  scheduled_run_dispatch_failed: "errors.run_dispatch_failed",
  scheduled_run_interrupted: "errors.run_interrupted",
  scheduled_runs_load_failed: "errors.runs_failed",
  scheduled_store_unavailable: "errors.store_unavailable",
  scheduled_turn_cancelled: "errors.turn_cancelled",
  scheduled_turn_failed: "errors.turn_failed",
  scheduled_turn_requires_approval: "errors.turn_requires_approval",
  scheduled_turn_requires_input: "errors.turn_requires_input",
  scheduled_input_timeout: "errors.input_timeout",
  scheduled_input_superseded: "errors.input_superseded",
  scheduled_recipe_lost: "errors.recipe_lost",
  scheduled_recipe_changed: "errors.recipe_changed",
  scheduled_recipe_rejected: "errors.recipe_rejected",
  scheduled_team_model_unconfigured: "errors.team_model_unconfigured",
  seat_exceeded: "errors.seat_exceeded",
  suspended: "errors.suspended",
  tamper: "errors.tamper",
  team_pool_exhausted: "errors.team_pool_exhausted",
  user_credit_exhausted: "errors.user_credit_exhausted",
  user_not_active: "errors.user_not_active",
  workspace_archived: "errors.workspace_archived",
  workspace_required: "errors.workspace_required",
  workspace_role_unknown: "errors.workspace_role_unknown",
  workspace_unavailable: "errors.workspace_unavailable",
  ws_access_denied: "errors.ws_access_denied",
};

// ─── Bind solidified workflows (scheduled-recipe-runs S1 / PRD FR-3) ────────────────────────
/** Persisted as JSON string; FE only tracks which recipe is bound, while bound_sha belongs to backend. */
export interface BoundWorkflowRef {
  form: BindableWorkflowForm;
  id_or_name: string;
}

const BINDABLE_FORMS: readonly BindableWorkflowForm[] = [
  "personal_skill",
  "team_skill",
  "team_subagent",
];

/** Three system pause codes for invalid bindings; only these expose "重新绑定". */
const RECIPE_PAUSE_CODES: ReadonlySet<string> = new Set([
  "scheduled_recipe_lost",
  "scheduled_recipe_changed",
  "scheduled_recipe_rejected",
]);

export function parseBoundWorkflowRef(raw: unknown): BoundWorkflowRef | null {
  let obj: unknown = raw;
  if (typeof raw === "string") {
    const s = raw.trim();
    if (!s) return null;
    try {
      obj = JSON.parse(s);
    } catch {
      return null;
    }
  }
  if (!obj || typeof obj !== "object") return null;
  const form = (obj as { form?: unknown }).form;
  const id = (obj as { id_or_name?: unknown }).id_or_name;
  if (typeof form !== "string" || typeof id !== "string" || !id) return null;
  if (!BINDABLE_FORMS.includes(form as BindableWorkflowForm)) return null;
  return { form: form as BindableWorkflowForm, id_or_name: id };
}

/** `<form>:<id_or_name>` — common encoding for solidification-card deep links and select option values. */
export function parseBindParam(raw: string): BoundWorkflowRef | null {
  const sep = raw.indexOf(":");
  if (sep <= 0) return null;
  return parseBoundWorkflowRef({
    form: raw.slice(0, sep),
    id_or_name: raw.slice(sep + 1),
  });
}

export function boundWorkflowKey(ref: BoundWorkflowRef): string {
  return `${ref.form}:${ref.id_or_name}`;
}

/**
 * Pause reason → readable explanation. Never fall back to raw code: users seeing `scheduled_recipe_changed`
 * would search the web; '作者改了内容，确认后可继续' explains their next action.
 *
 * Backend may persist either spelling (`scheduled_x` family / old bare `x`); recognize both.
 */
export function pauseReasonMessageKey(code: string): string | null {
  const c = code.trim();
  if (!c) return null;
  return (
    SCHEDULED_ERROR_MESSAGE_KEYS[c] ??
    SCHEDULED_ERROR_MESSAGE_KEYS[`scheduled_${c}`] ??
    null
  );
}

function useScheduledErrorPresentation() {
  const t = useTranslations("settings.scheduled") as ScheduleT;
  // Pass interface language unchanged; authorization explanations use its catalog (German no longer falls back to Chinese).
  const locale = useLocale() as Locale;

  return useCallback(
    (
      error: unknown,
      operation: ScheduledOperation,
    ): ErrorPresentation => {
      const genericMessage = t(`errors.${operation}_failed`);
      let presentation: ErrorPresentation;
      if (error instanceof ApiError) {
        presentation = toErrorPresentation(error, {
          locale,
          surface: "task",
          allowedDisclosures: ["authenticated", "admin"],
          genericMessage,
          messageForCode: (code) => {
            const entitlementMessage = entitlementErrorMessage(error, locale, {
              featureFallback: "scheduling",
            });
            if (entitlementMessage) return entitlementMessage;
            const key = SCHEDULED_ERROR_MESSAGE_KEYS[code];
            return key ? t(key) : null;
          },
        });
      } else if (isNetworkFailure(error)) {
        presentation =
          projectTransportFailure(
            {
              phase:
                operation === "load" || operation === "runs"
                  ? "receive"
                  : "send",
              timeoutTriggered: false,
              userAborted: false,
              onlineHint:
                typeof navigator === "undefined"
                  ? undefined
                  : navigator.onLine,
            },
            {
              requestTimedOut: t("errors.request_timed_out"),
              serviceUnreachable: t("errors.service_unreachable"),
              retry: t("retry"),
            },
          ) ?? { message: genericMessage };
      } else {
        presentation = { message: genericMessage };
      }
      if (operation !== "load" && operation !== "runs") {
        return presentation;
      }
      return {
        ...presentation,
        action: {
          kind: "retry",
          label: t("retry"),
        },
      };
    },
    [locale, t],
  );
}

function persistedRunFailure(
  run: ScheduledJobRunDTO,
  t: ScheduleT,
): ErrorPresentation | null {
  if (!run.error) return null;
  const code = SCHEDULED_ERROR_MESSAGE_KEYS[run.error]
    ? run.error
    : "scheduled_turn_failed";
  const parsed = parseErrorDiagnosticsWire(run.error_diagnostics);
  const diagnostics = normalizeErrorDiagnostics(
    parsed &&
      (parsed.disclosure === "authenticated" ||
        parsed.disclosure === "admin")
      ? {
          ...parsed,
          code: parsed.code ?? code,
          operationId: run.id,
        }
      : {
          schemaVersion: 1,
          disclosure: "authenticated",
          code,
          operationId: run.id,
        },
  );
  return {
    message: t(
      SCHEDULED_ERROR_MESSAGE_KEYS[code] ?? "errors.turn_failed",
    ),
    ...(diagnostics ? { diagnostics } : {}),
  };
}

// ─── preset ↔ cron ────────────────────────────────────────────────────────────
// Backend concurrently adds weekdays 1-5 and manual @manual support; frontend maps accordingly, assuming backend acceptance.
type Preset = "hourly" | "daily" | "weekday" | "weekly" | "manual" | "custom";
const MANUAL_CRON = "@manual";

// cron Sunday=0, Monday=1 … Saturday=6.
const WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
type WeekdayKey = (typeof WEEKDAY_KEYS)[number];
const WEEKDAY_TO_CRON: Record<WeekdayKey, number> = {
  mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6, sun: 0,
};
const CRON_TO_WEEKDAY: Record<number, WeekdayKey> = {
  0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat",
};

const TIME_OPTIONS = ["09:00", "08:00", "10:00", "12:00", "18:00", "21:00"];

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** preset + time/weekday → cron string. */
function presetToCron(preset: Preset, time: string, weekday: WeekdayKey): string {
  const [hh, mm] = parseTime(time);
  switch (preset) {
    case "hourly":
      return "0 * * * *";
    case "daily":
      return `${mm} ${hh} * * *`;
    case "weekday":
      return `${mm} ${hh} * * 1-5`;
    case "weekly":
      return `${mm} ${hh} * * ${WEEKDAY_TO_CRON[weekday]}`;
    case "manual":
      return MANUAL_CRON;
    default:
      return `${mm} ${hh} * * *`;
  }
}

function parseTime(time: string): [number, number] {
  const m = /^(\d{1,2}):(\d{2})$/.exec(time.trim());
  if (!m) return [9, 0];
  return [Math.min(23, Number(m[1])), Math.min(59, Number(m[2]))];
}

/** Basic five-field cron validation; detailed/sub-hourly validation belongs to backend 400. */
function isFiveFieldCron(cron: string): boolean {
  const c = cron.trim();
  if (c === MANUAL_CRON) return true;
  return c.split(/\s+/).filter(Boolean).length === 5;
}

interface CronShape {
  preset: Preset;
  time: string;
  weekday: WeekdayKey;
}

/** cron → infer preset state (prefill edit drawer); unrecognized values use custom. */
function cronToShape(cron: string): CronShape {
  const c = (cron || "").trim();
  if (c === MANUAL_CRON || c === "") {
    return { preset: c === MANUAL_CRON ? "manual" : "daily", time: "09:00", weekday: "mon" };
  }
  const f = c.split(/\s+/).filter(Boolean);
  if (f.length !== 5) return { preset: "custom", time: "09:00", weekday: "mon" };
  const [min, hour, dom, mon, dow] = f;
  if (min === "0" && hour === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { preset: "hourly", time: "09:00", weekday: "mon" };
  }
  const minN = Number(min);
  const hourN = Number(hour);
  const simpleTime = Number.isInteger(minN) && Number.isInteger(hourN)
    && minN >= 0 && minN <= 59 && hourN >= 0 && hourN <= 23;
  if (simpleTime && dom === "*" && mon === "*") {
    const time = `${pad2(hourN)}:${pad2(minN)}`;
    if (dow === "*") return { preset: "daily", time, weekday: "mon" };
    if (dow === "1-5") return { preset: "weekday", time, weekday: "mon" };
    if (/^[0-6]$/.test(dow)) return { preset: "weekly", time, weekday: CRON_TO_WEEKDAY[Number(dow)] };
  }
  return { preset: "custom", time: "09:00", weekday: "mon" };
}

/** cron → readable Chinese/English (list row + readout). @manual → manual. */
function cronToHuman(cron: string, t: ScheduleT): string {
  const c = (cron || "").trim();
  if (c === MANUAL_CRON) return t("human_manual");
  const f = c.split(/\s+/).filter(Boolean);
  if (f.length !== 5) return t("human_cron", { cron: c || "—" });
  const [min, hour, dom, mon, dow] = f;
  if (min === "0" && hour === "*" && dom === "*" && mon === "*" && dow === "*") {
    return t("human_hourly");
  }
  const minN = Number(min);
  const hourN = Number(hour);
  const simpleTime = Number.isInteger(minN) && Number.isInteger(hourN)
    && minN >= 0 && minN <= 59 && hourN >= 0 && hourN <= 23;
  if (!simpleTime || dom !== "*" || mon !== "*") return t("human_cron", { cron: c });
  const time = `${pad2(hourN)}:${pad2(minN)}`;
  if (dow === "*") return t("human_daily", { time });
  if (dow === "1-5") return t("human_weekday", { time });
  if (/^[0-6]$/.test(dow)) {
    return t("human_weekly", { weekday: t(`weekday_${CRON_TO_WEEKDAY[Number(dow)]}`), time });
  }
  // Multi-day lists (e.g. 1,3,5) fall back to generic display.
  return t("human_cron", { cron: c });
}

function presetIcon(cron: string): ReactNode {
  const c = (cron || "").trim();
  if (c === MANUAL_CRON) return <Hand size={14} />;
  const { preset } = cronToShape(c);
  switch (preset) {
    case "hourly": return <Repeat size={14} />;
    case "weekday": return <Briefcase size={14} />;
    case "weekly": return <Calendar size={14} />;
    case "custom": return <Terminal size={14} />;
    default: return <Sun size={14} />;
  }
}

// ─── time formatting ──────────────────────────────────────────────────────────
function formatAbsolute(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(d);
}

function formatRelative(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diffMs = d.getTime() - Date.now();
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const abs = Math.abs(diffMs);
  const min = Math.round(diffMs / 60000);
  const hr = Math.round(diffMs / 3_600_000);
  const day = Math.round(diffMs / 86_400_000);
  if (abs < 3_600_000) return rtf.format(min, "minute");
  if (abs < 86_400_000) return rtf.format(hr, "hour");
  return rtf.format(day, "day");
}

function runDurationLabel(run: ScheduledJobRunDTO, t: ScheduleT): string {
  if (run.status === "running") return t("run_duration_running");
  if (run.status === "skipped") return t("run_duration_skipped");
  if (run.finished_at && run.started_at) {
    const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
    if (Number.isFinite(ms) && ms >= 0) return t("run_duration", { seconds: Math.max(1, Math.round(ms / 1000)) });
  }
  return "";
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ════════════════════════════════════════════════════════════════════════════
// page
// ════════════════════════════════════════════════════════════════════════════
export function SettingsScheduled() {
  const t = useTranslations("settings.scheduled") as ScheduleT;
  const presentError = useScheduledErrorPresentation();
  const [jobs, setJobs] = useState<ScheduledJobDTO[]>([]);
  const [workspaces, setWorkspaces] = useState<WorkspaceDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] =
    useState<ErrorPresentation | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<ErrorPresentation | null>(null);

  const [drawer, setDrawer] = useState<
    | { mode: "new"; bind?: BoundWorkflowRef | null }
    | { mode: "edit"; job: ScheduledJobDTO; focusWorkflow?: boolean }
    | null
  >(null);
  const [confirmDelete, setConfirmDelete] = useState<ScheduledJobDTO | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const flash = (message: string) => {
    setErr(null);
    setMsg(message);
    window.setTimeout(() => setMsg(null), 3200);
  };

  const reportError = useCallback(
    (error: unknown, operation: ScheduledOperation) => {
      setMsg(null);
      setErr(presentError(error, operation));
    },
    [presentError],
  );

  const clearError = () => {
    setErr(null);
  };

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const d = await fetchScheduledJobs();
      setJobs(d.items);
    } catch (e) {
      setLoadError(presentError(e, "load"));
    } finally {
      setLoading(false);
    }
  }, [presentError]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    void readSpaces().then((w) => setWorkspaces(w.items)).catch(() => { /* Drawer falls back to an empty dropdown. */ });
  }, []);

  // Solidification success-card "设为定时任务" deep link: ?bind=<form>:<id_or_name> opens create drawer with preselection.
  // Client reads location (avoid useSearchParams Suspense boundary, following tools-manager's ?oauth= precedent),
  // then removes the parameter; otherwise every refresh opens another create drawer.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const raw = new URLSearchParams(window.location.search).get("bind");
    if (!raw) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("bind");
    window.history.replaceState({}, "", url.toString());
    const ref = parseBindParam(raw);
    if (ref) setDrawer({ mode: "new", bind: ref });
  }, []);

  const activeCount = jobs.filter((j) => j.status === "active").length;
  // Backend caps ACTIVE jobs only (count_active, status='active'); gate + display on that, not total
  // (else paused jobs spuriously trip "limit reached" / show a wrong "N / 10").
  const atLimit = activeCount >= MAX_JOBS;

  // ── Inline: enable/pause toggle (optimistic) ──
  const toggleStatus = async (job: ScheduledJobDTO) => {
    const next = job.status === "active" ? "paused" : "active";
    setBusyId(job.id);
    setJobs((prev) => prev.map((j) => (j.id === job.id ? { ...j, status: next } : j)));
    try {
      const updated = await patchScheduledJob(job.id, { status: next });
      setJobs((prev) => prev.map((j) => (j.id === job.id ? updated : j)));
    } catch (e) {
      setJobs((prev) => prev.map((j) => (j.id === job.id ? job : j))); // rollback
      reportError(e, "toggle");
    } finally {
      setBusyId(null);
    }
  };

  // ── Delete ──
  const performDelete = async () => {
    if (!confirmDelete || deleting) return;
    setDeleting(true);
    try {
      await deleteScheduledJob(confirmDelete.id);
      setJobs((prev) => prev.filter((j) => j.id !== confirmDelete.id));
      flash(t("deleted"));
    } catch (e) {
      reportError(e, "delete");
    } finally {
      setDeleting(false);
      setConfirmDelete(null);
    }
  };

  const onSaved = (saved: ScheduledJobDTO, mode: "new" | "edit") => {
    setJobs((prev) => mode === "new"
      ? [saved, ...prev]
      : prev.map((j) => (j.id === saved.id ? saved : j)));
    flash(mode === "new" ? t("created") : t("updated"));
    setDrawer(null);
  };

  return (
    <div className="routines-root legacy-ai-sched-root ai-sched">
      <RoutinesScopedStyles />

      {/* Header: breadcrumb 12 ink4 regular, neutral count badge beside title, unified primary action. */}
      <div className="legacy-ai-sched-head ai-page-head">
        <div className="legacy-ai-sched-head-text ai-page-head-text">
          <div className="legacy-ai-sched-eyebrow ai-sched-eyebrow"><CalendarClock size={13} /> {t("eyebrow")}</div>
          <div className="legacy-ai-sched-title-row ai-sched-title-row">
            <h1 className="ui-page-title legacy-scheduled-title">{t("title")}</h1>
            {!loading && !loadError ? (
              <span className="legacy-ai-sched-count ai-sched-count">
                <b className="legacy-ai-sched-count-n">{activeCount}</b> / {MAX_JOBS}
              </span>
            ) : null}
          </div>
          <p className="ui-page-desc legacy-scheduled-sub">{t("subtitle")}</p>
        </div>
        <div className="ai-page-head-actions">
          <Button
            variant="primary"
            disabled={atLimit}
            title={atLimit ? t("limit_reached", { max: MAX_JOBS }) : undefined}
            onClick={() => setDrawer({ mode: "new" })}
          >
            <Plus size={14} /> {t("new")}
          </Button>
        </div>
      </div>

      {msg ? <Notice tone="success">{msg}</Notice> : null}
      {err ? (
        <ErrorNotice
          message={err.message}
          action={err.action}
          diagnostics={err.diagnostics}
          diagnosticsEnabled
          surface="task"
          onAction={() => {
            clearError();
            void load();
          }}
        />
      ) : null}

      {/* body */}
      {loading ? (
        <LoadingSkeleton />
      ) : loadError ? (
        <ErrorState
          error={loadError}
          onRetry={() => void load()}
        />
      ) : jobs.length === 0 ? (
        <EmptyState t={t} onNew={() => setDrawer({ mode: "new" })} />
      ) : (
        <div className="legacy-ai-sched-list ai-card-list">
          {jobs.map((job) => (
            <RoutineRow
              key={job.id}
              job={job}
              t={t}
              busy={busyId === job.id}
              onToggle={() => void toggleStatus(job)}
              onEdit={() => setDrawer({ mode: "edit", job })}
              onRebind={() => setDrawer({ mode: "edit", job, focusWorkflow: true })}
              onDelete={() => setConfirmDelete(job)}
              reportError={reportError}
            />
          ))}
        </div>
      )}

      {drawer ? (
        <RoutineDrawer
          key={drawer.mode === "edit" ? drawer.job.id : "new"}
          mode={drawer.mode}
          job={drawer.mode === "edit" ? drawer.job : null}
          workspaces={workspaces}
          initialRecipeRef={drawer.mode === "new" ? drawer.bind ?? null : null}
          focusWorkflow={drawer.mode === "edit" ? drawer.focusWorkflow : false}
          t={t}
          onClose={() => setDrawer(null)}
          onSaved={(saved) => onSaved(saved, drawer.mode)}
        />
      ) : null}

      {confirmDelete ? (
        <ConfirmDialog
          title={t("confirm_delete_title")}
          description={t("confirm_delete_desc", { name: confirmDelete.name })}
          confirmLabel={t("delete")}
          cancelLabel={t("cancel")}
          busy={deleting}
          onCancel={() => setConfirmDelete(null)}
          onConfirm={() => void performDelete()}
        />
      ) : null}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// routine row (desktop + mobile via CSS)
// ════════════════════════════════════════════════════════════════════════════
function RoutineRow({
  job, t, busy, onToggle, onEdit, onRebind, onDelete, reportError,
}: {
  job: ScheduledJobDTO;
  t: ScheduleT;
  busy: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onRebind: () => void;
  onDelete: () => void;
  reportError: (
    error: unknown,
    operation: ScheduledOperation,
  ) => void;
}) {
  const presentError = useScheduledErrorPresentation();
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState<ScheduledJobRunDTO[] | null>(null);
  const [runsError, setRunsError] =
    useState<ErrorPresentation | null>(null);
  const [running, setRunning] = useState(false);

  const paused = job.status === "paused";
  const expired = job.status === "expired";
  const inactive = paused || expired;  // visually "off" — expired is not a healthy active job
  const human = cronToHuman(job.cron, t);

  const loadRuns = useCallback(async () => {
    setRunsError(null);
    try {
      const d = await fetchScheduledJobRuns(job.id, 10);
      setRuns(d.items);
    } catch (error) {
      setRunsError(presentError(error, "runs"));
      setRuns([]);
    }
  }, [job.id, presentError]);

  const toggleOpen = () => {
    const next = !open;
    setOpen(next);
    if (next && runs === null) void loadRuns();
  };

  // Run now: optimistically insert "运行中", then poll runs until no running state remains.
  // Poll awaiting_input too: users may answer in another browser tab; this history panel must refresh terminal state itself.
  const hasRunningRun = useMemo(
    () => (runs ?? []).some((r) => r.status === "running" || r.status === "awaiting_input"),
    [runs],
  );
  useEffect(() => {
    if (!open || !hasRunningRun) return;
    const h = window.setInterval(() => { void loadRuns(); }, RUNNING_POLL_MS);
    return () => window.clearInterval(h);
  }, [open, hasRunningRun, loadRuns]);

  const runNow = async () => {
    if (running) return;
    setRunning(true);
    if (!open) setOpen(true);
    try {
      const res = await runScheduledJobNow(job.id);
      // Optimistically show a "运行中" row immediately (backend run-now returns run_id/status).
      const optimistic: ScheduledJobRunDTO = {
        id: res.run_id,
        job_id: job.id,
        started_at: new Date().toISOString(),
        status: res.status ?? "running",
      };
      setRuns((prev) => [optimistic, ...(prev ?? []).filter((r) => r.id !== res.run_id)]);
      // Polling takes over actual state after tens of seconds; first fetch once to synchronize last_run_at, etc.
      void loadRuns();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        reportError(e, "run");
        void loadRuns();
      } else {
        reportError(e, "run");
      }
    } finally {
      setRunning(false);
    }
  };

  const failBadge = job.failure_count > 0 ? (
    <span className={`legacy-badge-sched-fail${job.failure_count >= 2 ? " is-crit" : ""}`}>
      <AlertTriangle size={12} />
      {job.failure_count >= 2
        ? t("fail_streak_warn", { count: job.failure_count })
        : t("fail_streak", { count: job.failure_count })}
    </span>
  ) : null;

  // Enabled `@manual` jobs run nothing automatically; labeling them '运行中' claims an event that did not happen (116 dogfood
  // P2-2). Enabled means retained until the user clicks Run Now.
  const activeLabel = job.cron === "@manual" ? t("status_active_manual") : t("status_active");
  const statusBadge = (
    <span className={`legacy-badge-sched ${inactive ? "is-paused" : "is-active"}`}>
      <span className="legacy-badge-sched-pip" aria-hidden />
      {expired ? t("status_expired") : paused ? t("status_paused") : activeLabel}
    </span>
  );

  // Bound solidified workflow (P2-1): without it, list users cannot identify bound tasks or their authors
  // without opening each edit drawer. Show other authors too (US-6 borrowed-authority visibility: recipients must distinguish
  // owner analysis from owner analysis using someone else's workflow). Omit unresolved names; pause strip explains invalidity.
  const boundChip = job.bound_display_name ? (
    <span className="legacy-sched-bound-chip" title={job.bound_author ? t("bound_by", { author: job.bound_author }) : undefined}>
      <Layers size={12} />
      <span className="legacy-sched-bound-name">{job.bound_display_name}</span>
      {job.bound_author ? <span className="legacy-sched-bound-author">· {job.bound_author}</span> : null}
    </span>
  ) : null;

  // "已暂停" gives state without reason; system pauses (invalid binding / unconfigured team models / archived space)
  // need a readable explanation; the three invalid-binding codes also expose "重新绑定" directly for repair.
  // Place below the entire row, not in the badge column: sentence + action would wrap into four lines in a narrow 1.1fr
  // column; one full-width strip covers desktop/mobile, avoiding duplicated rendering.
  const pauseCode = paused ? (job.pause_reason ?? "").trim() : "";
  const pauseMessageKey = pauseCode ? pauseReasonMessageKey(pauseCode) : null;
  const pauseText = pauseCode
    ? (pauseMessageKey ? t(pauseMessageKey) : t("pause_reason_unknown"))
    : null;
  const pauseStrip = pauseText ? (
    <div className="legacy-sched-pause-strip">
      <span className="legacy-sched-pause-icon">
        <AlertTriangle size={14} />
      </span>
      <span className="legacy-sched-pause-text">{pauseText}</span>
      {RECIPE_PAUSE_CODES.has(pauseCode) ? (
        <button type="button" className="legacy-sched-pause-act" onClick={onRebind}>
          <Layers size={13} /> {t("rebind")}
        </button>
      ) : null}
    </div>
  ) : null;

  return (
    <div
      className={`routine legacy-ai-sched-routine ai-sched-routine${paused ? " is-paused" : ""}${open ? " is-open" : ""}`}
    >
      {/* desktop grid row */}
      <div className="r-row-desktop legacy-ai-sched-rrow ai-sched-rrow">
        <div className="legacy-sched-row-name-cell">
          <span className={`legacy-sched-dot${paused ? " is-paused" : ""}`} />
          <span className="legacy-sched-row-name-text">
            <span className={`legacy-sched-r-name${paused ? " is-dim" : ""}`}>{job.name}</span>
            <span className="legacy-sched-r-query">{job.query}</span>
            {boundChip}
          </span>
        </div>

        <div className="legacy-sched-r-meta">
          <div className="legacy-sched-meta-ln">{statusBadge}</div>
          <div className={`legacy-sched-meta-ln${paused ? " is-dim" : ""}`}>
            <span className="legacy-sched-meta-ic">{presetIcon(job.cron)}</span>
            <span>{human}</span>
          </div>
        </div>

        <div className="legacy-sched-r-meta">
          <div className={`legacy-sched-meta-ln${paused ? " is-dim" : ""}`}>
            <span className="legacy-sched-meta-ic"><Clock size={14} /></span>
            <span>{t("next_run")} <span className="legacy-sched-mono">{paused ? t("no_next_run") : formatAbsolute(job.next_run_at)}</span></span>
          </div>
          <div className="legacy-sched-meta-ln-meta-sub">
            {paused ? t("next_run_paused_hint") : formatRelative(job.next_run_at)}
          </div>
          {failBadge ? <div className="legacy-sched-meta-ln">{failBadge}</div> : null}
        </div>

        <div className="legacy-sched-r-actions">
          <Switch
            checked={!inactive}
            title={t("toggle_title")}
            busy={busy}
            onCheckedChange={() => onToggle()}
            className={`legacy-switch-sched${paused ? "" : " is-on-pos"}`}
          >
            <span className={`legacy-switch-sched-knob${paused ? "" : " is-on"}`} />
          </Switch>
          <span className="legacy-ai-sched-sep ai-sched-sep" />
          <RowActionsMenu
            label={t("more_actions_for", { name: job.name })}
            actions={[
              { id: "run", label: t("run_now"), disabled: running, disabledReason: running ? t("run_now_running_reason") : undefined, onSelect: () => void runNow() },
              { id: "edit", label: t("edit"), onSelect: onEdit },
              { id: "delete", label: t("delete"), danger: true, onSelect: onDelete },
            ]}
          />
          <button
            type="button"
            aria-expanded={open}
            title={t("history_title")}
            onClick={toggleOpen}
            className="legacy-ai-sched-expand ai-sched-expand"
          >
            <span className={`legacy-ai-sched-chev${open ? " is-open" : ""}`}>
              <ChevronDown size={16} />
            </span>
          </button>
        </div>
      </div>

      {/* mobile stacked card */}
      <div className="r-row-mobile legacy-ai-sched-mcard">
        <div className="legacy-sched-mrow-head">
          <span className={`legacy-sched-dot legacy-sched-dot--m${paused ? " is-paused" : ""}`} />
          <span className={`legacy-sched-r-name legacy-sched-r-name--m${paused ? " is-dim" : ""}`}>{job.name}</span>
          {statusBadge}
        </div>
        <div className="legacy-sched-mrow-meta">
          <div className={`legacy-sched-meta-ln${paused ? " is-dim" : ""}`}>
            <span className="legacy-sched-meta-ic">{presetIcon(job.cron)}</span><span>{human}</span>
          </div>
          {boundChip ? <div className="legacy-sched-meta-ln">{boundChip}</div> : null}
          <div className={`legacy-sched-meta-ln${paused ? " is-dim" : ""}`}>
            <span className="legacy-sched-meta-ic"><Clock size={14} /></span>
            <span>{t("next_run")} <span className="legacy-sched-mono">{paused ? t("no_next_run") : formatAbsolute(job.next_run_at)}</span>
              {!paused && job.next_run_at ? ` · ${formatRelative(job.next_run_at)}` : ""}</span>
          </div>
          {failBadge ? <div className="legacy-sched-meta-ln">{failBadge}</div> : null}
        </div>
        <div className="legacy-sched-mrow-foot">
          <Switch
            checked={!inactive} busy={busy}
            onCheckedChange={() => onToggle()} title={t("toggle_title")}
            className={`legacy-switch-sched${paused ? "" : " is-on-pos"}`}
          >
            <span className={`legacy-switch-sched-knob${paused ? "" : " is-on"}`} />
          </Switch>
          <span className="legacy-sched-grow" />
          <RowActionsMenu
            label={t("more_actions_for", { name: job.name })}
            actions={[
              { id: "run", label: t("run_now"), disabled: running, disabledReason: running ? t("run_now_running_reason") : undefined, onSelect: () => void runNow() },
              { id: "edit", label: t("edit"), onSelect: onEdit },
            ]}
          />
        </div>
      </div>

      {/* System pause reason + repair entry (shared desktop/mobile strip). */}
      {pauseStrip}

      {/* run history (expanded) */}
      {open ? (
        <div className="legacy-ai-sched-history ai-sched-history">
          <div className="legacy-ai-sched-history-head ai-sched-history-head">
            <span>{t("history_recent")}</span>
          </div>
          {runs === null ? (
            <div className="legacy-sched-run-empty">…</div>
          ) : runsError ? (
            <ErrorNotice
              message={runsError.message}
              action={runsError.action}
              diagnostics={runsError.diagnostics}
              diagnosticsEnabled
              surface="task"
              onAction={() => void loadRuns()}
            />
          ) : runs.length === 0 ? (
            <div className="legacy-sched-run-empty">{t("history_empty")}</div>
          ) : (
            runs.map((run) => <RunRow key={run.id} run={run} t={t} />)
          )}
        </div>
      ) : null}
    </div>
  );
}

function RunRow({ run, t }: { run: ScheduledJobRunDTO; t: ScheduleT }) {
  // State colors: styles/ui-controls-legacy.css .legacy-badge-sched-run.is-* (original inline state colors moved verbatim).
  // Awaiting your answer is unfinished like running, but without pulse: waiting for a person, not executing.
  const tone: Record<ScheduledRunStatus, string> = {
    running: "is-running",
    success: "is-success",
    failed: "is-failed",
    skipped: "is-muted",
    expired: "is-muted",
    awaiting_input: "is-awaiting",
  };
  const label: Record<ScheduledRunStatus, string> = {
    running: t("run_status_running"),
    success: t("run_status_success"),
    failed: t("run_status_failed"),
    skipped: t("run_status_skipped"),
    expired: t("run_status_expired"),
    awaiting_input: t("run_status_awaiting_input"),
  };
  // Newer backends may send states outside this union; direct color indexing yields undefined → TypeError,
  // crashing run history. Prefer a neutral badge with raw status code to a blank panel.
  const toneClass = tone[run.status] ?? "is-muted";
  const statusLabel = label[run.status] ?? run.status;
  const dur = runDurationLabel(run, t);
  const runError = persistedRunFailure(run, t);

  return (
    <div className="legacy-sched-run">
      <span className={`legacy-badge-sched-run ${toneClass}`}>
        <span className="legacy-badge-sched-run-pip" />
        {statusLabel}
      </span>
      <div className="legacy-sched-run-main">
        <div className="legacy-sched-run-line">
          {formatAbsolute(run.started_at)} {dur ? <span className="legacy-sched-run-time">· {dur}</span> : null}
        </div>
        {runError ? (
          <ErrorNotice
            message={runError.message}
            diagnostics={runError.diagnostics}
            diagnosticsEnabled
            surface="task"
          />
        ) : null}
      </div>
      {run.status === "running" ? (
        <span className="legacy-sched-run-act-run-act-muted">{t("run_refresh_hint")}</span>
      ) : (run.status === "success" || run.status === "awaiting_input") && run.session_id ? (
        <a
          href={appPath(`/?session=${encodeURIComponent(run.session_id)}`)} className="legacy-sched-run-act"
          target="_blank"
          rel="noreferrer"
        >
          <ExternalLink size={12} />{" "}
          {run.status === "awaiting_input" ? t("go_answer") : t("view_result")}
        </a>
      ) : null}
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// drawer (create / edit)
// ════════════════════════════════════════════════════════════════════════════
export function RoutineDrawer({
  mode, job, workspaces, t, onClose, onSaved,
  variant = "standalone", initialDraft, initialWorkspaceId, draftSubscribe,
  footerLabels, onDiscard, initialRecipeRef, focusWorkflow,
}: {
  mode: "new" | "edit";
  job: ScheduledJobDTO | null;
  workspaces: WorkspaceDTO[];
  t: ScheduleT;
  onClose: () => void;
  onSaved: (saved: ScheduledJobDTO) => void;
  // ── scheduled-recipe-runs S1: bind solidified workflows ──
  /** Create preselection (solidification-card ?bind= deep link); editing decodes job.recipe_ref instead. */
  initialRecipeRef?: BoundWorkflowRef | null;
  /** Entry via invalid-row "重新绑定": scroll selector into view and focus after mount. */
  focusWorkflow?: boolean;
  // ── Batch 1-b (tech-design-batch1b §4.4): embedded variant for chat ConfigDrawer.
  //    All optional, defaulting to existing behavior: settings-page DOM unchanged (adapter preserves the hot-path body).
  variant?: "standalone" | "embedded";   // embedded: host owns shell/title/scrim/Esc handling.
  initialDraft?: ConfigDraftResult | null;   // Prefill after mount via applyAiDraft (full admit/kept mechanism).
  initialWorkspaceId?: string | null;        // Draft-fixed target space (P1-1: no longer use workspaces[0]).
  draftSubscribe?: (apply: (draft: ConfigDraftResult) => AiFillApplyReceipt) => () => void;
  footerLabels?: { save?: string; discard?: string };
  onDiscard?: () => void;
}) {
  const embedded = variant === "embedded";
  const presentError = useScheduledErrorPresentation();
  // Batch 0.5 P2-13: copilot UI copy moves to settings.config_copilot namespace; FieldAi/AiFillPanel
  // no longer depend on settings.scheduled, so other forms directly reuse the same copy.
  const tCopilot = useTranslations("settings.config_copilot") as ScheduleT;
  const initialShape = job ? cronToShape(job.cron) : { preset: "daily" as Preset, time: "09:00", weekday: "mon" as WeekdayKey };

  const [name, setName] = useState(job?.name ?? "");
  const [query, setQuery] = useState(job?.query ?? "");
  const [workspaceId, setWorkspaceId] = useState(job?.workspace_id ?? "");
  const [preset, setPreset] = useState<Preset>(initialShape.preset);
  const [time, setTime] = useState(initialShape.time);
  const [weekday, setWeekday] = useState<WeekdayKey>(initialShape.weekday);
  // New jobs default to the viewer's browser zone (UTC when the browser reports none); edits keep the job's zone.
  const [tz, setTz] = useState(() => job?.tz ?? viewerTimeZone());
  const [strategy, setStrategy] = useState<ScheduledSessionStrategy>(job?.session_strategy ?? "fresh");
  const [emails, setEmails] = useState<string[]>(job?.notify_channels?.email ?? []);
  const [emailDraft, setEmailDraft] = useState("");
  const [emailErr, setEmailErr] = useState<string | null>(null);
  const [sendEmail, setSendEmail] = useState(
    (job?.notify_channels?.email?.length ?? 0) > 0 || (job?.notify_channels?.deliver_on_success ?? false),
  );

  // ── Bind solidified workflows (PRD FR-3.2) ──
  // Editing decodes current binding from job.recipe_ref (JSON string); creation uses deep-link preselection.
  const savedRef = useMemo(() => parseBoundWorkflowRef(job?.recipe_ref ?? null), [job?.recipe_ref]);
  const [boundRef, setBoundRef] = useState<BoundWorkflowRef | null>(
    savedRef ?? initialRecipeRef ?? null,
  );
  // null = unavailable endpoint (404 / not deployed / request failed); omit selector, keep the rest of the form unchanged.
  const [bindable, setBindable] = useState<BindableWorkflowDTO[] | null>(null);
  const workflowFieldRef = useRef<HTMLDivElement | null>(null);
  const workflowFocusedRef = useRef(false);

  const [advOpen, setAdvOpen] = useState(initialShape.preset === "custom");
  const [customCron, setCustomCron] = useState(initialShape.preset === "custom" ? (job?.cron ?? "") : "");
  const [cronErr, setCronErr] = useState<string | null>(null);
  const [submissionError, setSubmissionError] =
    useState<ErrorPresentation | null>(null);
  const [saving, setSaving] = useState(false);

  // ── config-copilot (FEATURE_CONFIG_COPILOT; flag off = all following undefined, DOM unchanged) ──
  // 2026-08-18: FEATURE_CONFIG_COPILOT gate removed (Allen's decision, always on for all customers).
  // Keep constant instead of inline true: 5 downstream references, a name reads better than scattered true values.
  // Also fix flicker: original hook started false, turning true only after /health returned.
  const copilotEnabled = true;
  type CopilotField = "name" | "query" | "schedule" | "tz" | "notify";
  const COPILOT_FIELDS: CopilotField[] = ["name", "query", "schedule", "tz", "notify"];
  const [aiBusy, setAiBusy] = useState(false);
  type FieldSnapshot =
    | { kind: "name"; name: string }
    | { kind: "query"; query: string }
    | { kind: "schedule"; preset: Preset; time: string; weekday: WeekdayKey; customCron: string; advOpen: boolean }
    | { kind: "tz"; tz: string }
    | { kind: "notify"; emails: string[]; sendEmail: boolean };

  const snapshotOf = (field: CopilotField): FieldSnapshot => {
    if (field === "name") return { kind: "name", name };
    if (field === "query") return { kind: "query", query };
    if (field === "schedule") return { kind: "schedule", preset, time, weekday, customCron, advOpen };
    if (field === "tz") return { kind: "tz", tz };
    return { kind: "notify", emails: [...emails], sendEmail };
  };

  const restoreSnapshot = (snapshot: FieldSnapshot) => {
    if (snapshot.kind === "name") setName(snapshot.name);
    if (snapshot.kind === "query") setQuery(snapshot.query);
    if (snapshot.kind === "schedule") {
      setPreset(snapshot.preset); setTime(snapshot.time); setWeekday(snapshot.weekday);
      setCustomCron(snapshot.customCron); setAdvOpen(snapshot.advOpen); setCronErr(null);
    }
    if (snapshot.kind === "tz") setTz(snapshot.tz);
    if (snapshot.kind === "notify") { setEmails(snapshot.emails); setSendEmail(snapshot.sendEmail); }
  };

  // Batch 1-d WP-2: five-state mechanism extracted into shared hook (three invariants' comments / implementation retained verbatim there).
  const copilot = useCopilotFields<CopilotField, FieldSnapshot>({
    enabled: copilotEnabled,
    fields: COPILOT_FIELDS,
    snapshotOf,
    restoreSnapshot,
  });
  const aiMeta = copilot.aiMeta;
  const touchAiField = copilot.touchField;
  const revertAiField = copilot.revertField;
  const acceptAiField = copilot.acceptField;
  const acceptAllAi = copilot.acceptAll;
  const clearAiSuggestions = copilot.clearSuggestions;

  const applyAiDraft = (result: ConfigDraftResult): AiFillApplyReceipt => {
    const fields = result.fields as Partial<{ name: string; query: string; cron: string; tz: string; notify_channels: { email?: string[] } }>;
    const session = copilot.beginApply(result);
    const admit = session.admit;
    if (typeof fields.name === "string" && admit("name", "name")) setName(fields.name.slice(0, NAME_MAX));
    if (typeof fields.query === "string" && admit("query", "query")) setQuery(fields.query);
    if (typeof fields.cron === "string" && admit("schedule", "cron")) {
      const shape = cronToShape(fields.cron);
      setPreset(shape.preset); setTime(shape.time); setWeekday(shape.weekday);
      if (shape.preset === "custom") { setCustomCron(fields.cron); setAdvOpen(true); } else { setCustomCron(""); setAdvOpen(false); }
      setCronErr(null);
    }
    if (typeof fields.tz === "string" && admit("tz", "tz")) setTz(fields.tz);
    const notify = fields.notify_channels;
    if (notify && Array.isArray(notify.email) && admit("notify", "notify_channels")) {
      const list = notify.email.filter((e) => EMAIL_RE.test(e)).slice(0, MAX_EMAILS);
      setEmails(list);
      setSendEmail(list.length > 0);
    }
    // R4 review P2-6/P2-17: summary receipt = actual admit count (exclude kept/skipped) + readable issues for unmapped fields.
    return session.commit(["name", "query", "cron", "tz", "notify_channels"]);
  };

  // Batch 1-b: draft prefill + chat deltas (same admit/kept channel naturally preserves manual edits).
  const applyDraftRef = useRef(applyAiDraft);
  applyDraftRef.current = applyAiDraft;
  const draftAppliedRef = useRef(false);
  useEffect(() => {
    if (!initialDraft || draftAppliedRef.current) return;
    draftAppliedRef.current = true;
    applyDraftRef.current(initialDraft);
  }, [initialDraft]);
  useEffect(() => {
    if (!draftSubscribe) return;
    return draftSubscribe((draft) => applyDraftRef.current(draft));
  }, [draftSubscribe]);

  /** Field ai prop: always undefined with flag off; empty fields show skeleton while busy. */
  const fieldAi = (field: CopilotField): FieldAiProps | undefined => {
    if (!copilotEnabled) return undefined;
    if (aiBusy && !aiMeta[field] && ((field === "name" && !name) || (field === "query" && !query))) {
      return { state: "pending", t: tCopilot };
    }
    const entry = aiMeta[field];
    if (!entry) return undefined;
    return { ...entry, t: tCopilot, onAccept: () => acceptAiField(field), onRevert: () => revertAiField(field) };
  };

  // Default space: job's when editing; first listed when creating.
  useEffect(() => {
    if (workspaceId) return;
    // Batch 1-b P1-1: draft target space precedes first listed space, preventing wrong-space jobs in multi-space teams.
    const preferred = initialWorkspaceId && workspaces.some((w) => w.id === initialWorkspaceId) ? initialWorkspaceId : null;
    const fallback = preferred ?? workspaces[0]?.id ?? "";
    if (fallback) setWorkspaceId(fallback);
  }, [workspaces, workspaceId, initialWorkspaceId]);

  // Bindable workflow list: any failure/404 (older backend without endpoint / deployment without bindable workflows) means no binding surface,
  // silently omit it; optional binding must not turn a usable form into a red error page.
  useEffect(() => {
    let alive = true;
    void fetchBindableWorkflows()
      .then((d) => { if (alive) setBindable(d?.items ?? []); })
      .catch(() => { if (alive) setBindable(null); });
    return () => { alive = false; };
  }, []);

  const boundItem = useMemo(
    () => (boundRef
      ? (bindable ?? []).find((w) => w.form === boundRef.form && w.id_or_name === boundRef.id_or_name) ?? null
      : null),
    [bindable, boundRef],
  );
  // Empty list and no existing binding (including flag-off empty lists) means no choices; occupy no space.
  const showWorkflowField = bindable !== null && (bindable.length > 0 || boundRef !== null);

  // "重新绑定" entry: scroll selector into view / focus so users need not find it in a long form.
  useEffect(() => {
    if (!focusWorkflow || !showWorkflowField || workflowFocusedRef.current) return;
    workflowFocusedRef.current = true;
    const el = workflowFieldRef.current?.querySelector("select");
    if (!el) return;
    if (typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "center" });
    }
    el.focus();
  }, [focusWorkflow, showWorkflowField]);

  // Esc closes.
  useEffect(() => {
    if (embedded) return;  // embedded: host shell handles Esc centrally (avoid duplicate handlers, batch1b review P3-3).
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !saving) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, saving, embedded]);

  const usingCustom = preset === "custom";
  const effectiveCron = usingCustom
    ? customCron.trim()
    : presetToCron(preset, time, weekday);

  const humanReadout = cronToHuman(effectiveCron, t);
  const isManual = effectiveCron === MANUAL_CRON;

  const addEmail = (raw: string) => {
    const e = raw.trim().toLowerCase();
    if (!e) return;
    if (!EMAIL_RE.test(e)) { setEmailErr(t("notify_email_invalid")); return; }
    if (emails.includes(e)) { setEmailErr(t("notify_email_dup")); return; }
    if (emails.length >= MAX_EMAILS) { setEmailErr(t("notify_email_max", { max: MAX_EMAILS })); return; }
    touchAiField("notify");
    setEmails((prev) => [...prev, e]);
    setEmailDraft("");
    setEmailErr(null);
  };

  const onCustomCronChange = (v: string) => {
    setCustomCron(v);
    setCronErr(null);
    if (v.trim() && !isFiveFieldCron(v) && v.trim() !== MANUAL_CRON) {
      setCronErr(t("cron_invalid_format"));
    }
  };

  const canSave = name.trim().length > 0
    && name.trim().length <= NAME_MAX
    && query.trim().length > 0
    && workspaceId.length > 0
    && effectiveCron.length > 0
    && !cronErr
    && !saving;

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setCronErr(null);
    setSubmissionError(null);
    // Backend expects a dict, not an array: {email:[...], deliver_on_success:bool}. Send delivery-off
    // explicitly (not {}) so an edit-save unambiguously clears it on PATCH rather than relying on shape.
    const notify = sendEmail
      ? { email: emails, deliver_on_success: true }
      : { email: [], deliver_on_success: false };
    // When selector is absent (unavailable endpoint), omit the entire field from body; PATCH None means leave unchanged,
    // never unbind existing recipes merely because frontend omitted the selector.
    const recipeRefPatch: Record<string, unknown> | null = !showWorkflowField
      ? null
      : boundRef
        ? { form: boundRef.form, id_or_name: boundRef.id_or_name }
        : (savedRef ? { unbind: true } : null);
    const recipeRefBody = recipeRefPatch ? { recipe_ref: recipeRefPatch } : {};
    // "重新绑定" entry with a genuinely selected workflow repairs invalidity; also resume execution, otherwise job retains
    // paused + old pause_reason and UI still says '已暂停·绑定已失效', effectively leaving it unrepaired.
    const resume = focusWorkflow === true && boundRef !== null && job?.status === "paused";
    try {
      let saved: ScheduledJobDTO;
      if (mode === "new") {
        saved = await createScheduledJob({
          name: name.trim(),
          cron: effectiveCron,
          query: query.trim(),
          workspace_id: workspaceId,
          tz,
          session_strategy: strategy,
          notify_channels: notify,
          ...recipeRefBody,
        });
      } else if (job) {
        // Space is a structural binding, immutable (disabled in form); every other field is editable.
        saved = await patchScheduledJob(job.id, {
          name: name.trim(),
          cron: effectiveCron,
          query: query.trim(),
          tz,
          session_strategy: strategy,
          notify_channels: notify,
          ...recipeRefBody,
          ...(resume ? { status: "active" as const } : {}),
        });
      } else {
        return;
      }
      onSaved(saved);
    } catch (e) {
      // Backend 400 (invalid/sub-hourly) → inline red error below cron.
      if (
        e instanceof ApiError &&
        (e.code === "scheduled_invalid_cron" ||
          e.code === "request_validation_failed")
      ) {
        setAdvOpen(true);
        setCronErr(
          e.code === "scheduled_invalid_cron"
            ? t("errors.invalid_cron")
            : t("errors.invalid_configuration"),
        );
      } else {
        setSubmissionError(
          presentError(e, mode === "new" ? "create" : "update"),
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const presets: { key: Preset; label: string; icon: ReactNode }[] = [
    { key: "hourly", label: t("preset_hourly"), icon: <Repeat size={15} /> },
    { key: "daily", label: t("preset_daily"), icon: <Sun size={15} /> },
    { key: "weekday", label: t("preset_weekday"), icon: <Briefcase size={15} /> },
    { key: "weekly", label: t("preset_weekly"), icon: <Calendar size={15} /> },
    { key: "manual", label: t("preset_manual"), icon: <Hand size={15} /> },
  ];

  // embedded (batch 1-b): host ConfigDrawer supplies scrim/position/title bar/close; render only body+footer here.
  const shell = (inner: ReactNode) =>
    embedded ? (
      <>{inner}</>
    ) : (
      <div className="routine-drawer-scrim legacy-sched-scrim" onClick={(e) => { if (e.target === e.currentTarget && !saving) onClose(); }}>
        <div className="routine-drawer legacy-sched-drawer" role="dialog" aria-modal="true">
          {inner}
        </div>
      </div>
    );

  return shell(
    <>
        {/* Header (host renders when embedded). */}
        {embedded ? null : (
        <div className="legacy-sched-dr-head">
          <div className="legacy-sched-dr-headtext">
            <div className="legacy-sched-dr-eyebrow">{mode === "new" ? t("drawer_new_eyebrow") : t("drawer_edit_eyebrow")}</div>
            <div className="legacy-sched-dr-title">{mode === "new" ? t("drawer_new_title") : t("drawer_edit_title")}</div>
          </div>
          <IconBtn title={t("cancel")} onClick={onClose}><X size={18} /></IconBtn>
        </div>
        )}

        {/* body */}
        <div className={`legacy-sched-dr-body${copilotEnabled ? " ccp" : ""}`}>
          {/* embedded (chat drawer) omits AiFillPanel: chat itself generates suggestions; another "生成" action
              would overwrite them, and design drawer body contains only form (R2 review P2-6). */}
          {copilotEnabled && !embedded ? (
            <AiFillPanel
              formId="scheduled_job"
              getCurrentValues={() => ({ workspace_id: workspaceId })}
              onApply={applyAiDraft}
              onClear={clearAiSuggestions}
              onAcceptAll={acceptAllAi}
              onBusyChange={setAiBusy}
            />
          ) : null}
          {/* Name */}
          <Field label={t("field_name")} counter={`${name.length} / ${NAME_MAX}`} ctl={copilotEnabled} ai={fieldAi("name")}>
            <input className="legacy-sched-inp"
              maxLength={NAME_MAX}
              value={name}
              placeholder={t("field_name_placeholder")}
              onChange={(e) => { touchAiField("name"); setName(e.target.value); }}
            />
          </Field>

          {/* Query */}
          <Field label={t("field_query")} help={t("field_query_help")} ctl={copilotEnabled} ai={fieldAi("query")}>
            <textarea className="legacy-sched-inp-textarea"
              value={query}
              placeholder={t("field_query_placeholder")}
              onChange={(e) => { touchAiField("query"); setQuery(e.target.value); }}
            />
          </Field>

          {/* Bind solidified workflow (unavailable endpoint = omit region; remaining form behavior unchanged). */}
          {showWorkflowField ? (
            <div ref={workflowFieldRef}>
              <Field label={t("field_workflow")} help={t("field_workflow_help")}>
                <SelectBox
                  value={boundRef ? boundWorkflowKey(boundRef) : ""}
                  onChange={(v) => setBoundRef(v ? parseBindParam(v) : null)}
                  testId="bound-workflow"
                >
                  <option value="">{t("workflow_none")}</option>
                  {BINDABLE_FORMS.map((form) => {
                    const group = (bindable ?? []).filter((w) => w.form === form);
                    if (group.length === 0) return null;
                    return (
                      <optgroup key={form} label={t(`workflow_group_${form}`)}>
                        {group.map((w) => (
                          <option key={boundWorkflowKey(w)} value={boundWorkflowKey(w)}>
                            {w.display_name}
                          </option>
                        ))}
                      </optgroup>
                    );
                  })}
                  {/* Existing binding no longer bindable (deleted / lost access / flag off); still display and allow clearing. */}
                  {boundRef && !boundItem ? (
                    <option value={boundWorkflowKey(boundRef)}>{boundRef.id_or_name}</option>
                  ) : null}
                </SelectBox>
                {boundRef ? (
                  <div className="legacy-sched-bound-note">
                    <span className="legacy-sched-note-icon">
                      <Layers size={14} />
                    </span>
                    <span className="legacy-sched-bound-text">
                      <b className="legacy-sched-strong">
                        {boundItem?.display_name ?? boundRef.id_or_name}
                      </b>
                      {boundItem && !boundItem.is_own && (boundItem.author_name || boundItem.author) ? (
                        <span className="legacy-sched-bound-by">
                          {t("workflow_author", { author: boundItem.author_name || boundItem.author || "" })}
                        </span>
                      ) : null}
                      {!boundItem ? (
                        <span className="legacy-sched-bound-missing">
                          {t("workflow_unavailable")}
                        </span>
                      ) : null}
                    </span>
                    <button
                      type="button" className="legacy-sched-bound-clear"
                      onClick={() => setBoundRef(null)}
                    >
                      {t("workflow_clear")}
                    </button>
                  </div>
                ) : null}
              </Field>
            </div>
          ) : null}

          {/* Space */}
          <Field label={t("field_workspace")}>
            <SelectBox value={workspaceId} onChange={setWorkspaceId} disabled={mode === "edit"}>
              {workspaces.length === 0 ? <option value="">—</option> : null}
              {workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
            </SelectBox>
          </Field>

          <Divider />

          {/* Schedule */}
          <Field label={t("field_schedule")} option={t("schedule_min_interval")} ctl={copilotEnabled} ai={fieldAi("schedule")}>
            <div className="routines-preset-grid legacy-sched-preset-grid">
              {presets.map((p) => (
                <button
                  key={p.key}
                  type="button"
                  className={`legacy-sched-preset-btn${preset === p.key && !usingCustom ? " is-on" : ""}`}
                  onClick={() => {
                    touchAiField("schedule");
                    setPreset(p.key);
                    setAdvOpen(false);
                    setCustomCron("");
                    setCronErr(null);
                  }}
                >
                  <span className={`legacy-sched-preset-icon${preset === p.key && !usingCustom ? " is-on" : ""}`}>{p.icon}</span>
                  {p.label}
                </button>
              ))}
            </div>

            {/* sub picker */}
            {!usingCustom && (preset === "daily" || preset === "weekday") ? (
              <div className="legacy-sched-subpick">
                <div className="legacy-sched-grow">
                  <div className="legacy-sched-seg-lab">{preset === "daily" ? t("subpick_time_daily") : t("subpick_time_weekday")}</div>
                  <SelectBox value={time} onChange={(v) => { touchAiField("schedule"); setTime(v); }}>
                    {TIME_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
                  </SelectBox>
                </div>
              </div>
            ) : null}
            {!usingCustom && preset === "weekly" ? (
              <div className="legacy-sched-subpick-stack">
                <div>
                  <div className="legacy-sched-seg-lab">{t("subpick_weekday")}</div>
                  <div className="legacy-sched-weekdays">
                    {WEEKDAY_KEYS.map((wk) => (
                      <button
                        key={wk}
                        type="button"
                        className={`legacy-sched-weekday-btn${weekday === wk ? " is-on" : ""}`}
                        onClick={() => { touchAiField("schedule"); setWeekday(wk); }}
                      >
                        {t(`weekday_${wk}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <div className="legacy-sched-seg-lab">{t("subpick_time")}</div>
                  <SelectBox value={time} onChange={(v) => { touchAiField("schedule"); setTime(v); }}>
                    {TIME_OPTIONS.map((o) => <option key={o} value={o}>{o}</option>)}
                  </SelectBox>
                </div>
              </div>
            ) : null}

            {/* readout / manual note */}
            {isManual ? (
              <div className="legacy-sched-manual-note">
                <span className="legacy-sched-note-icon"><Info size={14} /></span>
                <span>{t("manual_note")}</span>
              </div>
            ) : !usingCustom ? (
              <div className="legacy-sched-readout">
                <span className="legacy-sched-readout-icon"><Check size={15} /></span>
                <span>{t("readout_will_run")}<b className="legacy-sched-strong">{humanReadout}</b>{t("readout_tz_suffix", { tz })}</span>
              </div>
            ) : null}

            {/* advanced cron */}
            <div className="legacy-sched-adv">
              <button type="button" className="legacy-sched-adv-head" onClick={() => setAdvOpen((v) => !v)}>
                <Terminal size={14} /> {t("adv_cron")}
                <span className={`legacy-sched-adv-chev${advOpen ? " is-open" : ""}`}>
                  <ChevronDown size={15} />
                </span>
              </button>
              {advOpen ? (
                <div className="legacy-sched-adv-body">
                  <input
                    className={`legacy-sched-inp-mono${cronErr ? " is-err" : ""}`}
                    value={usingCustom ? customCron : effectiveCron}
                    onChange={(e) => { touchAiField("schedule"); setPreset("custom"); onCustomCronChange(e.target.value); }}
                    placeholder="0 9 * * *"
                  />
                  {cronErr ? (
                    <div className="legacy-sched-cron-err"><span className="legacy-sched-flex-none"><AlertTriangle size={14} /></span> {cronErr}</div>
                  ) : (
                    <div className="legacy-sched-help">{t("adv_cron_help")}</div>
                  )}
                </div>
              ) : null}
            </div>
          </Field>

          {/* Timezone */}
          <Field label={t("field_tz")} ctl={copilotEnabled} ai={fieldAi("tz")}>
            <SelectBox value={tz} onChange={(v) => { touchAiField("tz"); setTz(v); }}>
              <option value="Asia/Shanghai">{t("tz_shanghai")}</option>
              <option value="Asia/Hong_Kong">{t("tz_hongkong")}</option>
              <option value="UTC">{t("tz_utc")}</option>
              <option value="America/Los_Angeles">{t("tz_la")}</option>
              {!["Asia/Shanghai", "Asia/Hong_Kong", "UTC", "America/Los_Angeles"].includes(tz) ? (
                <option value={tz}>{tz}</option>
              ) : null}
            </SelectBox>
          </Field>

          <Divider />

          {/* Execution mode */}
          <Field label={t("field_exec")}>
            <div className="legacy-sched-rcards">
              <RadioCard
                on={strategy === "fresh"}
                name={t("exec_fresh_name")}
                pill="fresh"
                desc={t("exec_fresh_desc")}
                onClick={() => setStrategy("fresh")}
              />
              <RadioCard
                on={strategy === "anchor"}
                name={t("exec_anchor_name")}
                pill="anchor"
                desc={t("exec_anchor_desc")}
                onClick={() => setStrategy("anchor")}
              />
            </div>
          </Field>

          <Divider />

          {/* Notifications */}
          <Field label={t("field_notify")} counter={t("notify_count", { used: emails.length, max: MAX_EMAILS })} ctl={copilotEnabled} ai={fieldAi("notify")}>
            <div className={`legacy-sched-chips${copilotEnabled ? " chips" : ""}`}>
              {emails.map((e) => (
                <span key={e} className="legacy-sched-chip">
                  {e}
                  <span
                    role="button"
                    tabIndex={0} className="legacy-sched-chip-x"
                    onClick={() => { touchAiField("notify"); setEmails((prev) => prev.filter((x) => x !== e)); }}
                  >
                    <X size={11} />
                  </span>
                </span>
              ))}
              <input className="legacy-sched-chip-inp"
                value={emailDraft}
                placeholder={emails.length < MAX_EMAILS ? t("notify_email_placeholder") : ""}
                disabled={emails.length >= MAX_EMAILS}
                onChange={(e) => { setEmailDraft(e.target.value); setEmailErr(null); }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addEmail(emailDraft); }
                  if (e.key === "Backspace" && !emailDraft && emails.length) {
                    touchAiField("notify");
                    setEmails((prev) => prev.slice(0, -1));
                  }
                }}
                onBlur={() => { if (emailDraft.trim()) addEmail(emailDraft); }}
              />
            </div>
            {emailErr ? <div className="legacy-sched-help-err">{emailErr}</div> : null}
            <div className="legacy-sched-notify-row">
              <div className="legacy-sched-grow">
                <div className="legacy-sched-notify-name">{t("notify_send_name")}</div>
                <div className="legacy-sched-notify-desc">{t("notify_send_desc")}</div>
              </div>
              <Switch
                checked={sendEmail}
                aria-label={t("notify_send_name")}
                onCheckedChange={() => { touchAiField("notify"); setSendEmail((v) => !v); }}
                className={`legacy-switch-sched${sendEmail ? " is-on-accent" : ""}`}
              >
                <span className={`legacy-switch-sched-knob${sendEmail ? " is-on" : ""}`} />
              </Switch>
            </div>
          </Field>
          {submissionError ? (
            <ErrorNotice
              message={submissionError.message}
              diagnostics={submissionError.diagnostics}
              diagnosticsEnabled
              surface="task"
            />
          ) : null}
        </div>

        {/* foot */}
        <div className="legacy-sched-dr-foot">
          {embedded && onDiscard ? (
            <button type="button" className="legacy-sched-btn-ghost" onClick={onDiscard} disabled={saving}>
              {footerLabels?.discard ?? t("cancel")}
            </button>
          ) : (
            <span className="legacy-sched-foot-hint"><Info size={13} /> {t("save_hint")}</span>
          )}
          <span className="legacy-sched-grow" />
          {embedded ? null : (
            <button type="button" className="legacy-sched-btn-ghost" onClick={onClose} disabled={saving}>{t("cancel")}</button>
          )}
          <button type="button" className={`legacy-sched-btn-pri${canSave ? "" : " is-disabled"}`} disabled={!canSave} onClick={() => void save()}>
            {saving ? t("saving") : (footerLabels?.save ?? t("save"))}
          </button>
        </div>
    </>
  );
}

// ════════════════════════════════════════════════════════════════════════════
// sub-components
// ════════════════════════════════════════════════════════════════════════════
function IconBtn({
  children, title, variant, disabled, onClick,
}: {
  children: ReactNode;
  title: string;
  variant?: "danger" | "run";
  disabled?: boolean;
  onClick: () => void;
}) {
  const [hover, setHover] = useState(false);
  // JS still switches hover state at the original inline-style timing; values: styles/ui-controls-legacy.css .legacy-sched-icon-btn.
  const hoverClass = !hover ? "" : variant === "danger" ? " is-hover-danger" : variant === "run" ? " is-hover-run" : " is-hover";
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      disabled={disabled}
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={`legacy-sched-icon-btn${hoverClass}${disabled ? " is-disabled" : ""}`}
    >
      {children}
    </button>
  );
}

/** config-copilot five field states (design screen 14): ai/warn/kept/done/pending + inline Accept/Restore.
 * Without `ai`, rendering is byte-identical to before copilot integration (flag off = unchanged DOM, PRD A4). */
export interface FieldAiProps {
  state: "ai" | "warn" | "kept" | "done" | "pending";
  note?: string | null;
  why?: string | null;
  t: ScheduleT;
  onAccept?: () => void;
  onRevert?: () => void;
}

function FieldAiMark({ ai }: { ai: FieldAiProps }) {
  if (ai.state === "pending") return null;
  if (ai.state === "ai") return <span className="mk ai"><Sparkles size={9} />{ai.t("mk_ai")}</span>;
  if (ai.state === "warn") return <span className="mk warn"><AlertTriangle size={9} />{ai.t("mk_warn")}</span>;
  if (ai.state === "kept") return <span className="mk kept"><Lock size={9} />{ai.t("mk_kept")}</span>;
  return <span className="mk done"><Check size={9} />{ai.t("mk_done")}</span>;
}

function Field({
  label, option, counter, help, children, ai, ctl,
}: {
  label: string;
  option?: string;
  counter?: string;
  help?: string;
  children: ReactNode;
  ai?: FieldAiProps;
  /** R4 review P2-15: flag on always mounts .ctl wrapper (bare .ctl unstyled; selectors require .fld state classes),
   *  so ai appearance/disappearance no longer remounts inputs; flag-off omission preserves existing DOM (A4). */
  ctl?: boolean;
}) {
  return (
    <div className={ai ? `legacy-sched-field is-ai fld ${ai.state}` : "legacy-sched-field"}>
      <div className="legacy-sched-field-head">
        <label className="legacy-sched-field-label">{label}</label>
        {ai ? <FieldAiMark ai={ai} /> : null}
        {option ? <span className="legacy-sched-field-option">{option}</span> : null}
        {counter ? <span className="legacy-sched-field-counter">{counter}</span> : null}
      </div>
      {ai || ctl ? <div className={ai ? "ctl fill-fade" : "ctl"}>{children}</div> : children}
      {ai?.note && (ai.state === "ai" || ai.state === "warn") ? (
        <div className="fld-note"><span className="ic"><AlertTriangle size={13} /></span><span>{ai.note}</span></div>
      ) : null}
      {ai && (ai.state === "ai" || ai.state === "warn") ? (
        <div className="fld-acts">
          <button type="button" className="mini acc" onClick={ai.onAccept}><Check size={11} />{ai.t("accept")}</button>
          <button type="button" className="mini" onClick={ai.onRevert}><Undo size={11} />{ai.t("revert")}</button>
          {ai.why ? (
            <span className="why-wrap"><span className="why" tabIndex={0}><Info size={11} />{ai.t("why")}</span><span className="tip">{ai.why}</span></span>
          ) : null}
        </div>
      ) : null}
      {ai && ai.state === "kept" ? (
        <div className="fld-acts"><span className="why"><Lock size={11} />{ai.t("kept_tip")}</span></div>
      ) : null}
      {help ? <div className="legacy-sched-help">{help}</div> : null}
    </div>
  );
}

function SelectBox({
  value, onChange, disabled, children, testId,
}: {
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <div className="legacy-sched-select-wrap">
      <select
        value={value}
        disabled={disabled}
        data-testid={testId}
        onChange={(e) => onChange(e.target.value)}
        className={`legacy-sched-select${disabled ? " is-disabled" : ""}`}
      >
        {children}
      </select>
      <span className="legacy-sched-select-chev">
        <ChevronDown size={14} />
      </span>
    </div>
  );
}

function RadioCard({
  on, name, pill, desc, onClick,
}: {
  on: boolean;
  name: string;
  pill: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button type="button" onClick={onClick} className={`legacy-sched-rcard${on ? " is-on" : ""}`}>
      <span className={`legacy-sched-radio${on ? " is-on" : ""}`}>
        {on ? <span className="legacy-sched-radio-dot" /> : null}
      </span>
      <span className="legacy-sched-rcard-text">
        <span className="legacy-sched-rcard-name">
          {name}
          <span className={`legacy-sched-pill${on ? " is-on" : ""}`}>{pill}</span>
        </span>
        <span className="legacy-sched-rcard-desc">{desc}</span>
      </span>
    </button>
  );
}

function Divider() {
  return <div className="legacy-sched-divider" />;
}

function EmptyState({ t, onNew }: { t: ScheduleT; onNew: () => void }) {
  return (
    <div className="legacy-ai-sched-empty ai-sched-empty">
      <div className="legacy-ai-sched-empty-ico ai-sched-empty-ico"><CalendarClock size={26} /></div>
      <div className="legacy-ai-sched-empty-title ai-sched-empty-title">{t("empty_title")}</div>
      <div className="legacy-ai-sched-empty-body ai-sched-empty-body">{t("empty_body")}</div>
      <button type="button" className="legacy-ai-sched-btnpri ai-sched-btnpri" onClick={onNew}><Plus size={15} /> {t("new")}</button>
    </div>
  );
}

function ErrorState({
  error,
  onRetry,
}: {
  error: ErrorPresentation;
  onRetry: () => void;
}) {
  return (
    <div className="legacy-sched-err-state">
      <ErrorNotice
        message={error.message}
        action={error.action}
        diagnostics={error.diagnostics}
        diagnosticsEnabled
        surface="task"
        onAction={onRetry}
      />
    </div>
  );
}

/** scoped keyframes + responsive desktop/mobile row swap (640px breakpoint). */
function RoutinesScopedStyles() {
  return (
    <style>{`
      @keyframes routinePulse { 0%,100%{opacity:1} 50%{opacity:.3} }
      @keyframes routineShimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
      .routine .r-row-mobile { display: none; }
      .routine .r-row-desktop { display: grid; }
      @media (max-width: 640px) {
        .routine .r-row-desktop { display: none; }
        .routine .r-row-mobile { display: flex; }
        .routine-drawer { width: 100% !important; border-left: none !important; }
        .routines-preset-grid { grid-template-columns: repeat(3, 1fr) !important; }
      }
    `}</style>
  );
}
