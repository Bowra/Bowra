"use client";

/**
 * Release and Evaluate › Test this change (agent-release-beginner-experience.md §4, mockups 03 and 03b;
 * Claude Design "Agent CI Spec" parts 1–2 for the rail, case table and Coach requests): one test card with
 * the view's single primary action, shared by the Release tab and the Evaluate tab. Release adds the version
 * rail, the per-case change table with the answers view, and the Coach evaluation requests beside it.
 *
 * Layout follows the workbench container width, not the viewport (`@container version-wb`): two
 * columns from 880, one column from 560, and the narrow list layout below 560 (Canvas pane, phone).
 */

import { forwardRef, useCallback, useEffect, useId, useMemo, useRef, useState, type ForwardedRef, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import {
  BadgeCheck,
  CircleAlert,
  CircleCheck,
  CircleX,
  History,
  ListChecks,
  Pencil,
  TriangleAlert,
  Clock,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { toast } from "@/lib/use-toast";
import {
  cancelAgentVersionComparison,
  completeAgentVersionRelease,
  createAgentVersionOverride,
  getAgentComparisonEstimate,
  getAgentEvalManager,
  getAgentEvalRun,
  getAgentVersionCaseAnswers,
  getAgentVersionCaseChanges,
  releasePrivateAgentDraft,
  testAgentChange,
  withdrawAgentVersionCandidate,
  type AgentEvalProgress,
  type AgentEvalSuite,
  type AgentEvalUsageEstimate,
  type AgentReleaseTestState,
  type AgentVersionCaseAnswerAttempt,
  type AgentVersionCaseAnswers,
  type AgentVersionCaseChange,
  type AgentVersionCaseChanges,
  type AgentVersionCasePassCount,
  type AgentVersionComparisonOverview,
  type AgentVersionUnfinishedCase,
  type AgentVersionWorkbench,
  type AgentWorkingCopyValue,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { randomUuid } from "@/lib/random-id";
import { useSpaceSelection } from "@/lib/use-space";
import { appPath } from "@/lib/base-path";
import { toolLabel } from "@/lib/tool-labels";
import { JUDGE_MODEL_SETTINGS_PATH, useCanConfigureModels } from "@/lib/use-can-configure-models";
import {
  ATTEMPT_FAILURE_REASONS,
  DATA_CHANGED_REASON,
  DATA_SOURCE_SETTINGS_REASONS,
  MODELS_SETTINGS_REASONS,
  TOOL_NOT_RUN_FAILURE_CODES,
  attemptReasonDetail,
  modelRoleLabel,
  retryCanHelp,
} from "@/lib/agent-eval-attempt-failure";
import { teamSettingsPath } from "@/lib/team-settings-paths";
import {
  CardStatus,
  useCiConfirm,
  CiCardHead,
  CiProgressBar,
  CiSpinner,
  CopyDiagnosticsButton,
  copyDiagnosticRecord,
  useCardStatus,
  useCiFormat,
  type CiFormat,
} from "./agent-ci-shared";
import { agentReleaseBlockedText, agentReleaseErrorKey, agentReleaseReasonKey } from "./agent-release-reason";
import { CoachCIPanel } from "./coach-ci-panel";
import { AgentThisChangeCard, testStateLabelKey } from "./agent-this-change-card";
import { AttemptPointVerdicts, PointCalibrationNotices, UnmetPointsList, quotedToolNames } from "./agent-eval-point-verdicts";
import { Markdown } from "@/lib/md";
import { pendingProposals } from "@/lib/agent-draft-changes";
import type { CiMerge } from "@/lib/agent-ci-merges";
import { ciRunTimeline, type CiTimelineStep } from "@/lib/agent-ci-timeline";

const ACTIVE_EXECUTION = new Set(["queued", "running", "finalizing"]);
/** Risk reasons that stand for unmet expected points; the acknowledgement lists those points (R28). */
const POINT_REASONS = new Set(["point_regression", "point_unmet"]);

/** What the release-check card shows; one card, one primary action (spec 3b/3d). */
export type ReleaseCardKind =
  | "preparing"
  | "prepare"
  | "rebase"
  | "compare"
  | "comparing"
  | "cancelled"
  | "failed"
  | "environment"
  | "risks"
  | "blocked"
  | "ready"
  | "review";

export function releaseCardKind(workbench: AgentVersionWorkbench, preparing: boolean): ReleaseCardKind {
  if (preparing) return "preparing";
  const execution = workbench.active_execution_summary;
  switch (workbench.primary_action.kind) {
    case "prepare_candidate":
      return "prepare";
    case "rebase_draft":
      return "rebase";
    case "wait_for_review":
      return "review";
    case "view_comparison":
      return "comparing";
    case "run_release_comparison":
      if (execution?.status === "cancelled" && workbench.readiness === "missing") return "cancelled";
      if (execution?.status === "failed" && workbench.readiness === "missing") return "failed";
      return "compare";
    case "retry_after_environment_fix":
      return "environment";
    case "resolve_risks":
      return "risks";
    case "fix_next_draft":
      return "blocked";
    case "activate_candidate":
    case "submit_for_review":
      return "ready";
    default:
      return execution && ACTIVE_EXECUTION.has(execution.status) ? "comparing" : "compare";
  }
}

/**
 * Reasons that put a comparison without a result down to attempts that stopped: the evaluation model, the
 * Agent's model, a Tool, a cancellation or a platform error. Each names its cases and fix in the reason list.
 */
const ENVIRONMENT_REASONS = ATTEMPT_FAILURE_REASONS;
/** A run stopped before its attempts, and the reasons that only restate that its results are missing. */
const RUN_STOP_REASONS = new Set(["run_model_unavailable", "run_model_config_changed"]);
const MISSING_RESULT_REASONS = new Set(["required_attempts_missing", "candidate_result_missing", "baseline_result_missing", "required_result_inconclusive"]);
/** Reasons that put it down to a time limit: the runs were stopped, nothing was broken. */
const TIME_LIMIT_REASONS = new Set(["attempt_time_limit_exceeded", "comparison_duration_budget_exceeded"]);

/**
 * Why a comparison has no result (readiness `unknown`), so the card names the actual cause: a
 * time limit is never shown as an environment failure.
 */
export function unknownResultCause(reasons: readonly string[]): "environment" | "time_limit" | "inconclusive" {
  if (reasons.some((code) => ENVIRONMENT_REASONS.has(code))) return "environment";
  if (reasons.some((code) => TIME_LIMIT_REASONS.has(code))) return "time_limit";
  return "inconclusive";
}

/**
 * What the Owner's test card shows, from server facts (agent-release-beginner-experience.md §4). The
 * candidate is an implementation detail of "Test this change": a prepared candidate that was never compared
 * reads "not tested", and a result that no longer covers the saved draft reads "stale".
 */
export type TestChangeState =
  | "nothing"
  | "not_tested"
  | "no_cases"
  | "testing"
  | "passed"
  | "risks"
  | "regressions"
  | "could_not_run"
  | "stale"
  | "review"
  | "rebase";

export function testChangeState(
  workbench: AgentVersionWorkbench,
  options: { kind: ReleaseCardKind; hasOwnerCases: boolean; startFailed: boolean },
): TestChangeState {
  const { kind } = options;
  if (kind === "review") return "review";
  if (kind === "rebase") return "rebase";
  if (kind === "preparing" || kind === "comparing") return "testing";
  if (options.startFailed) return "could_not_run";
  const candidate = workbench.candidate_identity;
  const stale = Boolean(candidate) && (
    Boolean(workbench.draft_ahead_of_candidate)
    || candidateIsOutdated(workbench)
    || workbench.display_state === "candidate_stale"
    || workbench.display_state === "evidence_stale"
    // Cases were saved after a finished test: its result no longer covers them.
    || (Boolean(workbench.evaluation_plan_behind) && Boolean(workbench.active_snapshot_identity))
  );
  if (stale) return "stale";
  if (kind === "failed" || kind === "environment" || kind === "cancelled") return "could_not_run";
  if (kind === "blocked") return "regressions";
  if (kind === "risks") return "risks";
  if (kind === "ready") return "passed";
  if (!candidate && !workbench.next_draft_identity) return "nothing";
  if (!options.hasOwnerCases) return "no_cases";
  return "not_tested";
}

const REGRESSION_REASONS = new Set(["critical_regression", "non_critical_regression"]);

/**
 * A blocked result is a regression only when a case got worse. A required case that fails on both the change
 * and the released version blocks too, but nothing got worse, so the page must not say so.
 */
export function blockedByRegression(reasons: readonly string[], changes: Pick<AgentVersionCaseChanges, "summary"> | null | undefined): boolean {
  return reasons.some((code) => REGRESSION_REASONS.has(code)) || (changes?.summary.worse ?? 0) > 0;
}

type SlotTone = "focus" | "bad" | "muted" | "plain";

export interface RailSlot {
  key: "live" | "change";
  tone: SlotTone;
  icon?: "check" | "alert" | "x" | "pencil" | "warn";
  value: string;
  valueMuted?: boolean;
  spinner?: boolean;
  badge?: { tone: "accent" | "warning" | "neutral"; text: string };
  progress?: { completed: number; total: number };
  sub: string;
}

/**
 * Release sequence the candidate would get if activated now. The server owns the numbering rule
 * (`candidate_identity.release_sequence_if_activated`); live + 1 is only the fallback for the
 * moment before a candidate exists and for servers that predate the field. Only the release action shows it.
 */
export function candidateReleaseSequence(workbench: AgentVersionWorkbench): number {
  return workbench.candidate_identity?.release_sequence_if_activated ?? workbench.live_identity.release_sequence + 1;
}

/** The candidate was prepared on a release that is no longer live, so its evidence no longer describes what would ship. */
export function candidateIsOutdated(workbench: AgentVersionWorkbench): boolean {
  const base = workbench.candidate_identity?.base_release_id;
  return Boolean(base) && base !== workbench.live_identity.release_id;
}

const STATE_TONE: Record<TestChangeState, { tone: SlotTone; icon?: RailSlot["icon"] }> = {
  nothing: { tone: "plain" },
  not_tested: { tone: "focus", icon: "pencil" },
  no_cases: { tone: "focus", icon: "pencil" },
  testing: { tone: "focus" },
  passed: { tone: "focus", icon: "check" },
  risks: { tone: "focus", icon: "warn" },
  regressions: { tone: "bad", icon: "x" },
  could_not_run: { tone: "bad", icon: "alert" },
  stale: { tone: "bad", icon: "alert" },
  review: { tone: "focus", icon: "warn" },
  rebase: { tone: "focus", icon: "warn" },
};

/**
 * Version rail (spec 3a, round 3): the released version and "this change". Only the released version has a
 * number; the change card names the saved draft and the test state. Pure so it is unit-tested without
 * rendering; copy comes from the caller's translator.
 */
export function versionRailModel(
  workbench: AgentVersionWorkbench,
  options: {
    state: TestChangeState;
    /** A blocked result has cases that got worse (see blockedByRegression); defaults to true. */
    regressed?: boolean;
    /** A "could not run" result that ran but gave no verdict (see unknownResultCause). */
    inconclusive?: boolean;
    hasDraft: boolean;
    draftSavedAt: string | null;
    liveActivatedAt: string | null;
    format: Pick<CiFormat, "date" | "time">;
    t: (key: string, values?: Record<string, string | number>) => string;
  },
): RailSlot[] {
  const { state, t, format } = options;
  const live = workbench.live_identity.release_sequence;
  const progress = workbench.active_execution_summary?.progress;
  const liveSlot: RailSlot = {
    key: "live",
    tone: state === "nothing" ? "focus" : "plain",
    icon: "check",
    value: `v${live}`,
    sub: options.liveActivatedAt ? t("version_rail_live_released", { date: format.date(options.liveActivatedAt) }) : t("version_rail_live_serving"),
  };
  const { tone, icon } = STATE_TONE[state];
  const changeSlot: RailSlot = {
    key: "change",
    tone,
    icon,
    value: state === "nothing"
      ? t("version_test_state_none")
      : options.draftSavedAt ? t("version_rail_change_saved", { time: format.time(options.draftSavedAt) }) : t("version_rail_change_saved_undated"),
    valueMuted: state === "nothing",
    spinner: state === "testing",
    progress: state === "testing" && progress ? { completed: progress.cases.completed, total: progress.cases.total } : undefined,
    sub: state === "rebase" ? t("version_rail_rebase", { version: `v${live}` }) : t(testStateLabelKey(state, options.regressed ?? true, options.inconclusive ?? false)),
  };
  return [liveSlot, changeSlot];
}

function SlotIcon({ icon }: { icon?: RailSlot["icon"] }) {
  if (icon === "check") return <CircleCheck size={14} aria-hidden className="am-ci-c-pos" />;
  if (icon === "alert") return <CircleAlert size={14} aria-hidden className="am-ci-c-neg" />;
  if (icon === "x") return <CircleX size={14} aria-hidden className="am-ci-c-neg" />;
  if (icon === "warn") return <TriangleAlert size={14} aria-hidden className="am-ci-c-warn" />;
  if (icon === "pencil") return <Pencil size={14} aria-hidden />;
  return null;
}

export function VersionRail({ slots }: { slots: RailSlot[] }) {
  const t = useTranslations("agents_marketplace");
  return (
    <ul className="am-ci-rail" aria-label={t("version_rail_label")}>
      {slots.map((slot) => (
        <li key={slot.key} className="am-ci-slot" data-tone={slot.tone} data-slot={slot.key}>
          <span className="am-ci-slot-k"><SlotIcon icon={slot.icon} />{t(`version_rail_${slot.key}` as never)}</span>
          <span className="am-ci-slot-v" data-muted={slot.valueMuted || undefined}>
            {slot.spinner ? <CiSpinner /> : null}
            {slot.value}
            {slot.badge ? <Badge size="sm" tone={slot.badge.tone}>{slot.badge.text}</Badge> : null}
          </span>
          {slot.progress ? (
            <CiProgressBar value={slot.progress.completed} max={slot.progress.total} label={t("version_progress_cases_label", slot.progress)} />
          ) : null}
          <span className="am-ci-slot-s">{slot.sub}</span>
        </li>
      ))}
    </ul>
  );
}

/** Tokens and model requests used; `sentence` joins them for running text (comma), `list` for a value cell. */
function usageClause(progress: AgentEvalProgress | undefined, format: CiFormat, t: ReturnType<typeof useTranslations>, style: "list" | "sentence" = "sentence"): string | null {
  if (!progress) return null;
  const parts: string[] = [];
  if (progress.usage.tokens !== null) parts.push(t("version_usage_tokens", { value: format.tokens(progress.usage.tokens) }));
  parts.push(t("version_usage_requests", { value: format.count(progress.model_calls.used) }));
  return parts.join(t(style === "list" ? "version_list_separator" : "version_list_comma"));
}

function suiteSummary(suites: AgentEvalSuite[], t: ReturnType<typeof useTranslations>): Array<{ key: string; name: string; count: number }> {
  return suites
    .filter((suite) => suite.revision && suite.state !== "archived" && (suite.suite_kind === "owner_quality" || suite.suite_kind === "platform_contract"))
    .map((suite) => ({
      key: suite.suite_id,
      name: suite.suite_kind === "platform_contract" ? t("eval_platform_suite_name") : suite.name,
      count: suite.revision!.case_count,
    }));
}

function sequenceLabel(value: number): string {
  return `v${value}`;
}

/**
 * The comparison behind the candidate's release check. The snapshot names it (`execution_id`), so the
 * failing cases stay available after the comparison finishes and `active_execution_summary` turns null
 * (tech-design §4.4). Older servers without the field fall back to a completed active execution.
 */
export function comparisonExecutionId(workbench: AgentVersionWorkbench): string | null {
  const snapshot = workbench.active_snapshot_identity;
  if (!workbench.candidate_identity || !snapshot) return null;
  if (snapshot.execution_id) return snapshot.execution_id;
  const execution = workbench.active_execution_summary;
  return execution?.status === "completed" ? execution.execution_id : null;
}

/** Per-case changes of the candidate's release-check comparison, read once per candidate and comparison. */
function useComparisonCaseChanges(agentId: string, workbench: AgentVersionWorkbench): { changes: AgentVersionCaseChanges | null; executionId: string | null } {
  const candidateId = workbench.candidate_identity?.candidate_id ?? null;
  const executionId = comparisonExecutionId(workbench);
  const [loaded, setLoaded] = useState<{ key: string; value: AgentVersionCaseChanges } | null>(null);
  const key = candidateId && executionId ? `${candidateId}:${executionId}` : null;
  useEffect(() => {
    if (!key || !candidateId || !executionId) return undefined;
    let cancelled = false;
    void getAgentVersionCaseChanges(agentId, candidateId, executionId).then((value) => {
      if (!cancelled) setLoaded({ key, value });
    }).catch(() => {
      if (!cancelled) setLoaded(null);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the exact candidate and comparison
  }, [agentId, key]);
  return { changes: key && loaded?.key === key ? loaded.value : null, executionId };
}

export type CaseBaselineLabel = "not_run" | "also_failing" | "new";

/**
 * D3: whether a failing case also fails on Live (tech-design §4.4). Only failing rows get a label;
 * `required_failure` is a required case that failed in both versions.
 */
export function caseBaselineLabel(item: Pick<AgentVersionCaseChange, "change" | "baseline">): CaseBaselineLabel | null {
  if (item.change !== "required_failure" && item.change !== "worse") return null;
  if (item.baseline === null) return "not_run";
  if (item.change === "required_failure") return "also_failing";
  return "new";
}

function TimelineIcon({ state }: { state: CiTimelineStep["state"] }) {
  if (state === "current") return <CiSpinner />;
  if (state === "done") return <CircleCheck size={14} className="am-ci-c-pos" />;
  if (state === "failed") return <CircleAlert size={14} className="am-ci-c-neg" />;
  return <Clock size={14} />;
}

/** S4 CI run timeline: each step with its state, time and, where known, who acted. */
export function CiRunTimeline({ steps, format }: { steps: CiTimelineStep[]; format: CiFormat }) {
  const t = useTranslations("agents_marketplace");
  const detail = (step: CiTimelineStep): string | null => {
    if (step.key === "prepared" && step.state === "current") return t("version_timeline_prepared_waiting");
    if (step.key === "evaluating" && step.state === "current") {
      return step.progress
        ? t("version_timeline_evaluating_progress", { completed: step.progress.completed, total: step.progress.total })
        : t(`version_execution_status_${step.status ?? "running"}` as never);
    }
    if (step.key === "evaluating" && step.state === "failed") return t(step.status === "cancelled" ? "version_timeline_evaluating_cancelled" : "version_timeline_evaluating_failed");
    if (step.key === "check" && step.outcome) return t(`version_timeline_check_${step.outcome}` as never);
    return null;
  };
  return (
    <ol className="am-ci-tl" aria-label={t("version_timeline_label")}>
      {steps.map((step) => {
        const text = detail(step);
        return (
          <li key={step.key} data-state={step.state}>
            <span className="am-ci-tl-icon" aria-hidden><TimelineIcon state={step.state} /></span>
            <span className="am-ci-tl-name">{t(`version_timeline_${step.key}` as never)}</span>
            <span className="sr-only">{t(`version_timeline_state_${step.state}` as never)}</span>
            {text ? <span className="am-ci-tl-detail">{text}</span> : null}
            {step.at || step.actor ? (
              <span className="am-ci-tl-meta">
                {[step.at ? format.dateTime(step.at) : null, step.actor ? t("version_timeline_by_owner") : null].filter(Boolean).join(t("version_list_separator"))}
              </span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

export function AgentVersionValidation({
  agentId,
  agentName,
  workbench,
  workingCopy,
  editorDirty = false,
  liveActivatedAt,
  reload,
  onChanged,
  onOpenConfig,
  onOpenEvaluate,
  onOpenRelease,
  onOpenHistory,
  onAskCoach,
  showChangeSummary = true,
  ciMerge = null,
  readOnly = false,
  surface = "release",
  suitesRefreshKey = 0,
  onAddCases,
  onTestState,
}: {
  agentId: string;
  agentName: string;
  workbench: AgentVersionWorkbench;
  workingCopy: AgentWorkingCopyValue | null;
  /** Configure has unsaved edits; the test uses the latest saved draft, so the card says they're left out. */
  editorDirty?: boolean;
  liveActivatedAt: string | null;
  reload: () => Promise<void>;
  onChanged: () => Promise<void>;
  onOpenConfig?: () => void;
  onOpenEvaluate?: () => void;
  /** Evaluate surface only: where advisory risks and the per-case details are handled. */
  onOpenRelease?: () => void;
  onOpenHistory?: () => void;
  /** Open Chat with Coach, given the failed checks in words (management page only; the canvas is already in Chat). */
  onAskCoach?: (failed: string[]) => void;
  /** The canvas lists Coach's changes above the configuration, so it leaves out the change summary. */
  showChangeSummary?: boolean;
  /** The newest CI proposal merged on this page, for the CI run timeline. */
  ciMerge?: CiMerge | null;
  /** A read-only view (such as a read-only Canvas): notices stay, actions that change the release check are left out. */
  readOnly?: boolean;
  /**
   * "release": the Release tab (rail, test card with its details, case changes, Coach requests).
   * "evaluate": the same test card above the evaluation suites; details stay on Release.
   */
  surface?: "release" | "evaluate";
  /** Bumped when the Owner saves the suite on this page; the card rereads the cases (the "no cases" state ends). */
  suitesRefreshKey?: number;
  /** Evaluate: start a case in the editor below (the "no cases" card's primary action). */
  onAddCases?: () => void;
  onTestState?: (state: TestChangeState) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const format = useCiFormat();
  const { activeId: workspaceId, loading: workspaceLoading } = useSpaceSelection();
  const { confirm, dialog } = useCiConfirm();
  const canConfigureModels = useCanConfigureModels() && !readOnly;
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useCardStatus();
  const [startFailure, setStartFailure] = useState<{ message: string; cause: unknown } | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [estimate, setEstimate] = useState<AgentEvalUsageEstimate | null>(null);
  const [suites, setSuites] = useState<AgentEvalSuite[] | null>(null);
  const [coachPending, setCoachPending] = useState(0);
  const [coachVisible, setCoachVisible] = useState(false);
  const [coachExpanded, setCoachExpanded] = useState<boolean | null>(null);
  const [answersFor, setAnswersFor] = useState<AgentVersionCaseChange | null>(null);
  const [releasing, setReleasing] = useState(false);
  const [releaseNote, setReleaseNote] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const coachRef = useRef<HTMLElement | null>(null);
  const changesRef = useRef<HTMLElement | null>(null);
  // The comparison details sit inside the test card, folded once the test passed (one result, not two cards).
  const [changesOpen, setChangesOpen] = useState<boolean | null>(null);
  const cardTitleId = useId();
  const evaluate = surface === "evaluate";

  const live = workbench.live_identity.release_sequence;
  const candidateSeq = candidateReleaseSequence(workbench);
  const candidate = workbench.candidate_identity;
  const execution = workbench.active_execution_summary;
  const plan = workbench.active_plan_identity;
  const snapshot = workbench.active_snapshot_identity;
  const kind = releaseCardKind(workbench, busy === "test");
  const reasons = workbench.reason_codes;
  const planBehind = Boolean(workbench.evaluation_plan_behind);

  // Suite names and case counts for the card, the "no cases" state and Coach requests.
  useEffect(() => {
    let cancelled = false;
    void getAgentEvalManager(agentId).then((value) => {
      if (!cancelled) setSuites(value.suites);
    }).catch(() => {
      if (!cancelled) setSuites([]);
    });
    return () => { cancelled = true; };
  }, [agentId, candidate?.candidate_id, suitesRefreshKey]);
  // Saving cases can move the evaluation policy, so the workbench is reread too (not on the first render).
  const savedKey = useRef(suitesRefreshKey);
  useEffect(() => {
    if (savedKey.current === suitesRefreshKey) return;
    savedKey.current = suitesRefreshKey;
    void reload().catch(() => undefined);
  }, [reload, suitesRefreshKey]);
  const suiteRows = suiteSummary(suites ?? [], t);
  // Unknown until the suites load, so the card never flashes "no cases" for an Agent that has them.
  const hasOwnerCases = suites === null || (suites ?? []).some((suite) => suite.suite_kind === "owner_quality" && suite.state !== "archived" && (suite.revision?.case_count ?? 0) > 0);
  const state = testChangeState(workbench, { kind, hasOwnerCases, startFailed: startFailure !== null });
  useEffect(() => { onTestState?.(state); }, [onTestState, state]);

  // D6 estimate: read once per exact plan while a test is offered or running (never on every poll).
  const wantsEstimate = Boolean(candidate && plan && ["not_tested", "testing", "could_not_run", "stale"].includes(state));
  const estimateKey = wantsEstimate ? `${candidate!.candidate_id}:${plan!.id}:${plan!.hash}` : null;
  useEffect(() => {
    if (!estimateKey || !candidate || !plan) {
      setEstimate(null);
      return;
    }
    let cancelled = false;
    void getAgentComparisonEstimate(agentId, candidate.candidate_id, plan).then((value) => {
      if (!cancelled) setEstimate(value);
    }).catch(() => {
      // 404 (run insights off), 409 or any failure: the card leaves the estimate out.
      if (!cancelled) setEstimate(null);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the exact Plan identity
  }, [agentId, estimateKey]);

  // D8 per-case changes of the comparison behind the candidate's snapshot; absent → reason sentences only.
  const { changes, executionId: changesExecutionId } = useComparisonCaseChanges(agentId, workbench);

  // Progress numbers are not announced; completion, failure and cancellation are, once.
  const previousStatus = useRef(execution?.status ?? null);
  useEffect(() => {
    const previous = previousStatus.current;
    const next = execution?.status ?? null;
    previousStatus.current = next;
    if (!previous || !ACTIVE_EXECUTION.has(previous) || !next || ACTIVE_EXECUTION.has(next)) return;
    setAnnouncement(t(next === "completed" ? "version_announce_completed" : next === "cancelled" ? "version_announce_cancelled" : "version_announce_failed"));
  }, [execution?.status, t]);

  const errorText = (key: string, cause: unknown): string => {
    if (cause instanceof ApiError && cause.code === "agent_eval_fixture_expired") return fixtureExpiredText(cause, suites ?? [], format, t);
    if (cause instanceof ApiError && cause.code === "agent_eval_model_unavailable") return modelUnavailableText(cause, canConfigureModels, t);
    if (key === "test" && cause instanceof ApiError) {
      const code = cause.code ?? "";
      const named = TEST_ERROR_COPY[code];
      if (named) return t(named as never, { live: sequenceLabel(live) } as never);
      if (code.includes("judge") || code.includes("model_config")) return t("version_test_error_models");
      if (code === "agent_eval_workspace_execute_denied") return t("version_plan_refresh_space_denied");
    }
    // Test cases were saved after this page loaded; the server refuses to release on the earlier plan.
    if (cause instanceof ApiError && cause.code === "agent_eval_plan_behind") return t("version_plan_behind_refused");
    if (cause instanceof ApiError && cause.code === "agent_eval_policy_version_conflict") return t("version_policy_changed");
    if (cause instanceof ApiError && cause.code === "agent_not_active") return t("version_agent_archived");
    // The candidate was checked against an earlier platform contract; nothing in the Agent needs fixing.
    if (cause instanceof ApiError && blockedReasonCodes(cause).includes("platform_contract_upgraded")) return t("version_platform_contract_upgraded");
    if ((key === "activate" || key === "submit") && cause instanceof ApiError) {
      // Releasing re-checks the exact tested evidence; each refusal says what changed and what to do.
      const code = cause.code ?? "";
      const gate = blockedReasonCodes(cause);
      if ((code === "agent_eval_release_blocked" || code === "agent_eval_release_run_incomplete") && gate.length) {
        return t("version_release_error_blocked", { reasons: [...new Set(gate.map((reason) => t(gateReasonKey(reason) as never)))].join(t("version_list_semicolon")) });
      }
      const named = RELEASE_ERROR_COPY[code];
      if (named) return t(named as never, { live: sequenceLabel(live) } as never);
      const blocked = agentReleaseBlockedText(cause, (item, values) => t(item as never, values as never));
      if (blocked) return blocked;
      const publication = agentReleaseErrorKey(code);
      if (publication) return t(publication as never);
    }
    if (key === "test" && cause instanceof ApiError) {
      // A blocked preparation answers with the failing conditions; name them instead of "try again".
      const codes = blockedReasonCodes(cause);
      if (codes.length) {
        const text = [...new Set(codes.map((code) => t(agentReleaseReasonKey(code) as never)))].join(t("version_list_semicolon"));
        return t("version_test_error_blocked", { reasons: text });
      }
      return t("version_test_error_generic");
    }
    return t(`version_failed_${key}` as never);
  };

  /**
   * Runs one card action. Retry repeats the same operation closure, so a request that carries an
   * idempotency key resends that key and replays instead of releasing twice.
   */
  const run = async (key: string, operation: () => Promise<unknown>, success?: string, onSuccess?: () => void): Promise<boolean> => {
    if (busy) return false;
    setBusy(key);
    setStatus(null);
    try {
      await operation();
      if (success) setStatus({ tone: "success", message: success });
      onSuccess?.();
      await onChanged();
      await reload();
      return true;
    } catch (cause) {
      if (cause instanceof ApiError && (cause.code === "agent_eval_policy_version_conflict" || cause.code === "agent_eval_plan_behind" || RELEASE_RELOAD_CODES.has(cause.code ?? ""))) {
        // The policy follows the Agent's latest saved test cases, so saving or adding cases elsewhere moves it.
        // Retrying would resend the old identities; refetch so the next attempt uses the current ones.
        await reload();
        setStatus({ tone: "error", message: errorText(key, cause), cause });
        return false;
      }
      setStatus({ tone: "error", message: errorText(key, cause), cause, onRetry: () => void run(key, operation, success, onSuccess) });
      return false;
    } finally {
      setBusy(null);
    }
  };

  /**
   * "Test this change": one server action reads the saved draft, reuses or prepares the candidate and starts
   * the comparison. A failure lands in the "could not run" state with the condition and the fix; the next
   * press finishes whatever is left (the server reuses a prepared candidate).
   */
  const test = async () => {
    if (!workspaceId || busy) return;
    setBusy("test");
    setStatus(null);
    try {
      await testAgentChange(agentId, workspaceId, randomUuid());
      setStartFailure(null);
      await onChanged();
      await reload();
    } catch (cause) {
      setStartFailure({ message: errorText("test", cause), cause });
      await reload().catch(() => undefined);
    } finally {
      setBusy(null);
    }
  };

  const progress = execution?.progress;
  const cancelComparison = async () => {
    const description = progress
      ? t("version_cancel_body_progress", {
        completed: progress.cases.completed,
        total: progress.cases.total,
        usage: usageClause(progress, format, t) ?? "",
      })
      : t("version_cancel_body");
    if (!await confirm({
      title: t("version_cancel_title"),
      description,
      cancelLabel: t("version_cancel_keep"),
      confirmLabel: t("version_cancel_confirm"),
      tone: "danger",
    })) return;
    void run("cancel", () => cancelAgentVersionComparison(agentId, workbench));
  };

  /** Shared Agents in review: withdrawing the submission returns the change to the Owner. */
  const withdraw = async () => {
    if (!await confirm({
      title: t("version_withdraw_title"),
      description: t("version_withdraw_body", { live: sequenceLabel(live) }),
      confirmLabel: t("version_withdraw_confirm"),
      tone: "danger",
    })) return;
    void run(
      "withdraw",
      () => withdrawAgentVersionCandidate(agentId, workbench),
      t("version_withdrawn_inline", { live: sequenceLabel(live) }),
    );
  };

  const submitAction = workbench.primary_action.kind === "submit_for_review";
  /**
   * Private Agents (§4): the test is information, never a gate. "Release as vN" releases the latest saved draft
   * whatever the test says; only a running test holds it. Shared Agents keep the evidence and review flow.
   */
  const privateRelease = workbench.private_release ?? null;
  const releaseSeq = privateRelease?.release_sequence ?? candidateSeq;
  const privateWarning = Boolean(privateRelease?.warning);
  const canReleasePrivately = Boolean(privateRelease?.available) && !readOnly && state !== "testing" && state !== "review";
  /** "Release as v2" / "Submit for review (as v2)": the dialog holds the optional release note. */
  const release = async () => {
    if (privateRelease) {
      if (!workspaceId) return;
      const released = sequenceLabel(releaseSeq);
      const note = releaseNote.trim();
      const idempotencyKey = randomUuid();
      const testState = privateRelease.test_state;
      const ok = await run(
        "activate",
        () => releasePrivateAgentDraft(agentId, workbench, { workspaceId, testState, releaseNote: note || null, idempotencyKey }),
        t("version_released_inline", { candidate: released }),
        () => toast({
          kind: "success",
          text: t("version_released_toast", { candidate: released, agent: agentName }),
          ...(onOpenHistory ? { action: { label: t("version_view_history"), onAction: onOpenHistory } } : {}),
        }),
      );
      if (ok) {
        setReleasing(false);
        setReleaseNote("");
      }
      return;
    }
    const activate = !submitAction;
    const released = sequenceLabel(candidateSeq);
    const note = releaseNote.trim();
    const idempotencyKey = randomUuid();
    const ok = await run(
      activate ? "activate" : "submit",
      () => completeAgentVersionRelease(agentId, workbench, activate ? "activate" : "submit", idempotencyKey, note || null),
      activate ? t("version_released_inline", { candidate: released }) : t("version_submitted_inline", { live: sequenceLabel(live) }),
      activate ? () => toast({
        kind: "success",
        text: t("version_released_toast", { candidate: released, agent: agentName }),
        ...(onOpenHistory ? { action: { label: t("version_view_history"), onAction: onOpenHistory } } : {}),
      }) : undefined,
    );
    if (ok) {
      setReleasing(false);
      setReleaseNote("");
    }
  };

  const recordRisks = () => void run(
    "override",
    () => createAgentVersionOverride(agentId, workbench, notes),
    t("version_override_saved"),
  );

  const diagnostics = useCallback(() => ({
    agent_id: agentId,
    live_release_id: workbench.live_identity.release_id,
    live_revision_id: workbench.live_identity.revision_id,
    candidate_id: candidate?.candidate_id,
    candidate_revision_id: candidate?.revision_id,
    plan_id: plan?.id,
    plan_policy_revision_id: plan?.policy_revision_id,
    current_eval_policy_revision_id: workbench.current_eval_policy_revision_id,
    evaluation_plan_behind: planBehind,
    snapshot_id: snapshot?.id,
    execution_id: execution?.execution_id,
    execution_status: execution?.status,
    failure_code: execution?.failure_code,
    display_state: workbench.display_state,
    primary_action: workbench.primary_action.kind,
    reason_codes: reasons,
    // Which cases stopped and why, with the attempt references the server logged the errors under.
    unfinished_cases: changes?.unfinished?.map((item) => ({
      case_key: item.case_key,
      reason_codes: item.reason_codes,
      failures: item.failures?.map((failure) => ({ reason_code: failure.reason_code, cause: failure.cause, tool: failure.tool, references: failure.references })),
    })),
    test_start_error: startFailure?.cause instanceof ApiError ? startFailure.cause.code : undefined,
  }), [agentId, candidate, changes, execution, plan, planBehind, reasons, snapshot, startFailure, workbench]);

  const timeline = ciRunTimeline(workbench, ciMerge);
  const issueCount = reasons.length || (changes ? changes.summary.worse + changes.summary.unchanged_required_failures : 0);
  const regressed = blockedByRegression(reasons, changes);
  const inconclusive = state === "could_not_run" && !startFailure && kind === "environment" && unknownResultCause(reasons) === "inconclusive";
  const slots = versionRailModel(workbench, {
    state,
    regressed,
    inconclusive,
    hasDraft: Boolean(workbench.next_draft_identity || workingCopy),
    draftSavedAt: workingCopy?.updated_at ?? null,
    liveActivatedAt,
    format,
    t: (key, values) => t(key as never, values as never),
  });

  const cardMenu = <RowActionsMenu label={t("version_card_more")} actions={[
    { id: "diagnostics", label: t("version_menu_copy_diagnostics"), onSelect: () => void copyDiagnosticRecord(diagnostics(), t("version_copy_diagnostics_prompt")) },
  ] satisfies RowAction[]} />;

  const coachAwaiting = coachPending > 0;
  // Spec 7: on a phone the view's single primary action docks to the bottom edge. While Coach awaits
  // consent its "Allow and start" is that primary, so the test card does not dock.
  const dock = coachAwaiting || evaluate ? undefined : "primary";
  const viewChanges = () => {
    setChangesOpen(true);
    requestAnimationFrame(() => changesRef.current?.focus());
  };
  const canAct = !readOnly;
  const testButton = (label: "version_test_action" | "version_test_again" | "version_test_again_after_fix", variant: "primary" | "secondary" = "primary") => canAct ? (
    <Button variant={variant} disabled={busy !== null || workspaceLoading || !workspaceId} loading={busy === "test"} onClick={() => void test()}>{t(label)}</Button>
  ) : null;
  /** Private Agents: "Release as vN" in every result state; "Release as vN anyway" after a real regression. */
  const releaseButton = (variant: "primary" | "secondary" | "ghost" = "secondary") => canReleasePrivately ? (
    <Button variant={variant} disabled={busy !== null || workspaceLoading || !workspaceId} loading={busy === "activate"} onClick={() => setReleasing(true)}>
      {t(privateWarning ? "version_release_anyway_action" : "version_release_action", { candidate: sequenceLabel(releaseSeq) })}
    </Button>
  ) : null;
  const notes_ = (
    <>
      {editorDirty && canAct ? <p className="am-ci-note" data-tone="warning">{t("version_test_unsaved_note")}</p> : null}
      {canAct && !workspaceLoading && !workspaceId ? <p className="am-ci-note">{t("version_workspace_required")}</p> : null}
    </>
  );

  const card = (() => {
    const head = (props: Parameters<typeof CiCardHead>[0]) => <CiCardHead titleId={cardTitleId} {...props} />;
    switch (state) {
      case "nothing":
        if (evaluate) return <p className="am-ci-note">{t("eval_no_config_changes", { live: sequenceLabel(live) })}</p>;
        return <>
          {head({ icon: <ListChecks size={16} />, title: t("version_test_nothing_title"), body: t("version_test_nothing_body", { live: sequenceLabel(live) }) })}
          {onOpenConfig ? <div className="am-ci-acts"><Button variant="secondary" onClick={onOpenConfig}>{t("version_change_go_config")}</Button></div> : null}
        </>;
      case "no_cases": {
        // §9 as amended: adding cases is the primary action on both surfaces; testing stays possible (secondary),
        // because an Agent without its own cases must still be releasable.
        const addCases = evaluate ? onAddCases : onOpenEvaluate;
        return <>
          {head({ icon: <ListChecks size={16} />, title: t("version_test_no_cases_title"), body: t("version_test_no_cases_body") })}
          <div className="am-ci-acts" data-dock={dock}>
            {addCases && canAct ? <Button variant="primary" onClick={addCases}>{t("version_test_add_cases")}</Button> : null}
            {testButton("version_test_action", addCases && canAct ? "secondary" : "primary")}
            {releaseButton(addCases && canAct ? "ghost" : "secondary")}
          </div>
          {notes_}
        </>;
      }
      case "not_tested":
        return <>
          {head({ icon: <CircleAlert size={16} />, tone: "warning", title: t("version_test_not_tested_title"), body: t("version_test_not_tested_body", { live: sequenceLabel(live) }) })}
          {coachAwaiting ? (
            <div className="am-ci-ist" data-tone="info">
              <span className="am-ci-ist-text">{t("version_card_prepare_coach_test")}</span>
            </div>
          ) : null}
          <div className="am-ci-acts" data-dock={dock}>
            {testButton("version_test_action", coachAwaiting ? "secondary" : "primary")}
            {releaseButton(coachAwaiting ? "ghost" : "secondary")}
          </div>
          {notes_}
        </>;
      case "testing": {
        const elapsed = progress?.elapsed_ms ?? (execution?.started_at ? Date.now() - new Date(execution.started_at).valueOf() : null);
        // The design's single figure: the upper end of the estimate. Its lower end can be 0 (a quarter of past
        // attempts made no model request), which says nothing about this test, so it isn't shown.
        const requests = estimate?.status === "available" && estimate.model_calls && estimate.model_calls.high > 0
          ? t("version_test_running_estimate", { value: format.count(estimate.model_calls.high) })
          : null;
        const coach = execution?.origin_kind === "internal_coach" ? t("version_test_running_coach") : null;
        const title = kind === "preparing" || !execution ? t("version_test_preparing_title")
          : progress ? t("version_test_running_title", { completed: progress.cases.completed, total: progress.cases.total })
            : t("version_test_running_title_unknown");
        const body = kind === "preparing" || !execution
          ? t("version_test_preparing_body")
          : [coach, requests, t("version_test_running_body")].filter(Boolean).join(t("version_list_separator"));
        return <>
          {head({ icon: <CiSpinner />, tone: "accent", title, body })}
          {progress ? (
            <CiProgressBar value={progress.cases.completed} max={progress.cases.total} label={t("version_progress_cases_label", { completed: progress.cases.completed, total: progress.cases.total })} />
          ) : elapsed !== null && execution ? (
            <p className="am-ci-meta"><span>{t("version_progress_elapsed", { value: format.elapsed(elapsed) })}</span></p>
          ) : null}
          {execution?.can_cancel && canAct ? (
            <div className="am-ci-acts">
              <Button variant="ghost" disabled={busy !== null} loading={busy === "cancel"} onClick={() => void cancelComparison()}>{t("version_test_stop")}</Button>
            </div>
          ) : null}
        </>;
      }
      case "stale": {
        const released = candidateIsOutdated(workbench);
        return <>
          {head({
            icon: <History size={16} />,
            tone: "warning",
            title: t(released ? "version_test_stale_released_title" : "version_test_stale_title"),
            body: released ? t("version_test_stale_released_body", { live: sequenceLabel(live) }) : t("version_test_stale_body"),
          })}
          <div className="am-ci-acts" data-dock={dock}>{testButton("version_test_again")}{releaseButton()}</div>
          {notes_}
        </>;
      }
      case "could_not_run": {
        if (startFailure) {
          // No model for the evaluation: testing again gives the same refusal until the Team's models change.
          const noModel = startFailure.cause instanceof ApiError && startFailure.cause.code === "agent_eval_model_unavailable";
          return <>
            {head({ icon: <CircleAlert size={16} />, tone: "warning", title: t(noModel ? "version_test_model_unavailable_title" : "version_test_start_failed_title"), body: startFailure.message })}
            <div className="am-ci-acts" data-dock={dock}>
              {noModel && canConfigureModels ? <ModelsSettingsLink /> : null}
              {testButton(noModel ? "version_test_again_after_fix" : "version_test_again", noModel && canConfigureModels ? "secondary" : "primary")}
              {releaseButton(noModel && canConfigureModels ? "ghost" : "secondary")}
              <CopyDiagnosticsButton record={diagnostics} />
            </div>
            {notes_}
          </>;
        }
        const cause = kind === "failed" ? "execution" : kind === "cancelled" ? "cancelled" : unknownResultCause(reasons);
        const title = cause === "cancelled" ? "version_test_cancelled_title" : cause === "time_limit" ? "version_test_time_limit_title" : cause === "inconclusive" ? "version_test_inconclusive_title" : "version_test_failed_title";
        // Attempts that stopped name their own causes in the list below; only a failed execution has no list to point to.
        const body = cause === "cancelled" ? "version_test_cancelled_body" : cause === "time_limit" ? "version_test_time_limit_body" : cause === "inconclusive" ? "version_test_inconclusive_body" : cause === "environment" ? "version_test_attempts_body" : "version_test_failed_body";
        // Rerunning can't change a required Tool that evaluation can't run; the fix is on the case's point.
        const toolCannotRun = reasons.includes("required_tool_not_run_in_eval");
        // An answer that depended on a Tool evaluation can't run yet (a data query, say): the case can't be tested
        // in evaluation at all, so there is nothing to retry or change on the case.
        const usedToolCannotRun = reasons.includes("tool_not_run_in_eval");
        const bodyText = cause !== "inconclusive"
          ? t(body)
          : toolCannotRun
            ? t("version_test_tool_not_run_body")
            : usedToolCannotRun ? t("version_test_used_tool_not_run_body") : t(body);
        // Retry only when it can give a verdict; a cause that stays (missing evaluation model, rejected credentials)
        // is fixed elsewhere first, so the retry waits for that fix and says so.
        const retryHelps = retryCanHelp(reasons);
        // A run stopped before its attempts already explains why they are missing; don't repeat it as its own reason.
        const shownReasons = reasons.some((code) => RUN_STOP_REASONS.has(code)) ? reasons.filter((code) => !MISSING_RESULT_REASONS.has(code)) : reasons;
        const modelsFix = canConfigureModels && reasons.some((code) => MODELS_SETTINGS_REASONS.has(code));
        // The Team's Datarc connection is fixed by a team administrator under Team settings › Data Sources.
        const dataSourceFix = canConfigureModels && reasons.some((code) => DATA_SOURCE_SETTINGS_REASONS.has(code));
        return <>
          {head({ icon: cause === "time_limit" ? <Clock size={16} /> : <CircleAlert size={16} />, tone: "warning", title: t(title), body: bodyText })}
          {/* Both surfaces list the reasons the body points to ("the reasons below"), naming the cases, Tools and fixes. */}
          {shownReasons.length ? <ReasonList codes={shownReasons} tone="neg" changes={changes} live={live} agentId={agentId} candidateRevisionId={workbench.candidate_identity?.revision_id} onOpenCase={setAnswersFor} /> : null}
          <div className="am-ci-acts" data-dock={dock}>
            {retryHelps ? testButton("version_test_again") : null}
            {/* Testing again can't run a Tool evaluation can't run: the fix is the case's point (on Evaluate, right below). */}
            {!retryHelps && toolCannotRun && onOpenEvaluate && !evaluate ? <Button variant="primary" onClick={onOpenEvaluate}>{t("version_test_open_points")}</Button> : null}
            {!retryHelps && !toolCannotRun && !usedToolCannotRun ? <>
              {modelsFix ? <ModelsSettingsLink /> : null}
              {dataSourceFix ? <DataSourceSettingsLink /> : null}
              {testButton("version_test_again_after_fix", modelsFix || dataSourceFix ? "secondary" : "primary")}
            </> : null}
            {releaseButton(modelsFix || dataSourceFix || (!retryHelps && toolCannotRun && !onOpenEvaluate) ? "ghost" : "secondary")}
            <CopyDiagnosticsButton record={diagnostics} />
          </div>
          {notes_}
        </>;
      }
      case "risks":
        return <>
          {head({ icon: <TriangleAlert size={16} />, tone: "warning", title: t("version_test_risks_title", { count: reasons.length }), body: t(privateRelease ? "version_test_risks_body_private" : "version_test_risks_body") })}
          {evaluate ? (
            onOpenRelease ? <div className="am-ci-acts"><Button variant="primary" onClick={onOpenRelease}>{t("version_test_risks_open_release")}</Button></div> : null
          ) : <>
            <ReasonList codes={reasons} tone="warn" changes={changes} live={live} onOpenCase={setAnswersFor} />
            {candidate && changesExecutionId && reasons.some((code) => POINT_REASONS.has(code)) ? (
              <UnmetPointsList agentId={agentId} candidateId={candidate.candidate_id} executionId={changesExecutionId} />
            ) : null}
            {/* Private Agents release without written reasons; the risks stay listed above and in the dialog. */}
            {privateRelease ? (
              canReleasePrivately ? <div className="am-ci-acts" data-dock={dock}>{releaseButton("primary")}</div> : null
            ) : canAct ? reasons.map((code) => (
              <div key={code} className="am-ci-fld">
                <label htmlFor={`${cardTitleId}-${code}`}>{t("version_override_reason", { reason: t(gateReasonKey(code) as never) })}</label>
                <textarea
                  id={`${cardTitleId}-${code}`}
                  className="settings-input"
                  rows={2}
                  maxLength={2000}
                  value={notes[code] ?? ""}
                  onChange={(event) => setNotes((current) => ({ ...current, [code]: event.target.value }))}
                />
              </div>
            )) : null}
            {canAct && !privateRelease ? (
              <div className="am-ci-acts" data-dock={dock}>
                <Button variant="primary" disabled={busy !== null || !workbench.primary_action.enabled || !reasons.length || !reasons.every((code) => notes[code]?.trim())} loading={busy === "override"} onClick={recordRisks}>{t("version_accept_risks")}</Button>
              </div>
            ) : null}
          </>}
        </>;
      case "regressions": {
        const worse = changes?.summary.worse ?? 0;
        // Nothing got worse when the blocking cases fail on the released version too: say that, not "got worse".
        const bothFail = changes?.summary.unchanged_required_failures ?? 0;
        const body = regressed
          ? t("version_test_regressions_body")
          : bothFail > 0
            ? t("version_test_blocked_both_fail_body", { count: bothFail, live: sequenceLabel(live) })
            : t("version_test_blocked_body");
        return <>
          {head({
            icon: <CircleX size={16} />,
            tone: "danger",
            title: worse > 0 ? t("version_test_regressions_title", { count: worse }) : t("version_test_blocked_title", { count: Math.max(1, reasons.length) }),
            body,
          })}
          {!evaluate ? <ReasonList codes={reasons} tone="neg" changes={changes} live={live} onOpenCase={setAnswersFor} onViewChanges={changes ? viewChanges : undefined} agentId={agentId} candidateRevisionId={workbench.candidate_identity?.revision_id} /> : null}
          <div className="am-ci-acts" data-dock={dock}>
            {onAskCoach && canAct ? (
              <Button variant="primary" onClick={() => onAskCoach(reasons.map((code) => t(gateReasonKey(code) as never)))}>{t("version_ask_coach_fix")}</Button>
            ) : null}
            {evaluate && onOpenRelease ? <Button variant="secondary" onClick={onOpenRelease}>{t(regressed ? "version_test_view_details" : "version_test_view_failures")}</Button> : null}
            {!evaluate && onOpenConfig ? <Button variant="secondary" onClick={onOpenConfig}>{t("version_fix_in_draft")}</Button> : null}
            {releaseButton("ghost")}
          </div>
        </>;
      }
      case "passed": {
        const summary = changes
          ? t("version_card_ready_summary", {
            better: changes.summary.better,
            same: Math.max(0, changes.summary.case_count - changes.summary.better - changes.summary.worse - changes.summary.unstable - changes.summary.not_comparable),
            worse: changes.summary.worse,
          })
          : null;
        const frozen = snapshot ? format.dateTime(snapshot.created_at) : null;
        return <>
          {head({ icon: <BadgeCheck size={16} />, tone: "positive", title: t("version_test_passed_title"), body: [summary, frozen].filter(Boolean).join(t("version_list_separator")) || undefined })}
          {workbench.display_state === "ready_with_risks" ? <p className="am-ci-note">{t("version_card_ready_risks_recorded")}</p> : null}
          {submitAction ? <p className="am-ci-note">{t("version_card_ready_submit_note", { live: sequenceLabel(live) })}</p> : null}
          {privateRelease ? (
            canReleasePrivately ? <div className="am-ci-acts" data-dock={dock}>{releaseButton("primary")}</div> : null
          ) : canAct ? (
            <div className="am-ci-acts" data-dock={dock}>
              <Button variant="primary" disabled={busy !== null || !workbench.primary_action.enabled} loading={busy === "activate" || busy === "submit"} onClick={() => setReleasing(true)}>
                {submitAction ? t("version_submit_review", { candidate: sequenceLabel(candidateSeq) }) : t("version_release_action", { candidate: sequenceLabel(candidateSeq) })}
              </Button>
            </div>
          ) : null}
        </>;
      }
      case "review":
        return <>
          {head({ icon: <Clock size={16} />, tone: "warning", title: t("version_test_in_review_title"), body: t("version_card_review_body", { live: sequenceLabel(live) }) })}
          {candidate && candidate.state !== "approved" && canAct ? (
            <div className="am-ci-acts" data-dock={dock}>
              <Button variant="secondary" className="settings-button--destructive-text" disabled={busy !== null} loading={busy === "withdraw"} onClick={() => void withdraw()}>{t("version_withdraw_candidate")}</Button>
            </div>
          ) : null}
        </>;
      case "rebase":
        return <>
          {head({ icon: <TriangleAlert size={16} />, tone: "warning", title: t("version_card_rebase_title", { live: sequenceLabel(live) }), body: t("version_card_rebase_body", { live: sequenceLabel(live) }) })}
          {onOpenConfig ? (
            <div className="am-ci-acts" data-dock={dock}>
              <Button variant="primary" onClick={onOpenConfig}>{t("version_fix_in_draft")}</Button>
            </div>
          ) : null}
        </>;
    }
  })();

  const testLine = privateRelease ? releaseTestLine(privateRelease.test_state, privateRelease.reason_codes, changes, (key, values) => t(key as never, values as never)) : null;
  const releaseDialog = releasing ? (
    <Dialog
      title={submitAction && !privateRelease ? t("version_submit_review", { candidate: sequenceLabel(candidateSeq) }) : t("version_release_title", { candidate: sequenceLabel(releaseSeq) })}
      onClose={() => { if (!busy) setReleasing(false); }}
      busy={busy === "activate" || busy === "submit"}
      footer={<>
        <Button variant="secondary" disabled={busy !== null} onClick={() => setReleasing(false)}>{t("cancel")}</Button>
        <Button variant="primary" loading={busy === "activate" || busy === "submit"} disabled={busy !== null} onClick={() => void release()}>
          {privateRelease
            ? t(privateWarning ? "version_release_anyway_action" : "version_release_confirm", { candidate: sequenceLabel(releaseSeq) })
            : submitAction ? t("version_submit_review", { candidate: sequenceLabel(candidateSeq) }) : t("version_release_confirm", { candidate: sequenceLabel(candidateSeq) })}
        </Button>
      </>}
    >
      {/* One plain line about the test when it didn't pass; a warning only for a real regression or a safety-contract failure. */}
      {testLine ? <p className="am-ci-note" data-tone={privateWarning ? "danger" : "warning"} data-release-test={privateRelease?.test_state}>{testLine}</p> : null}
      <p className="am-ci-note">{privateRelease
        ? t(privateRelease.test_state === "passed" ? "version_test_release_body_private" : "version_release_body_private", { candidate: sequenceLabel(releaseSeq), live: sequenceLabel(live) })
        : submitAction ? t("version_card_ready_submit_note", { live: sequenceLabel(live) }) : t(candidate?.audience_mode === "private" ? "version_test_release_body_private" : candidate?.audience_mode === "custom" ? "version_test_release_body_custom" : "version_test_release_body", { candidate: sequenceLabel(candidateSeq), live: sequenceLabel(live) })}</p>
      {(
        <div className="am-ci-fld">
          <label htmlFor={`${cardTitleId}-note`}>{t("version_release_note_label")}</label>
          <textarea
            id={`${cardTitleId}-note`}
            className="settings-input"
            name="agent-release-note"
            autoComplete="off"
            rows={3}
            maxLength={2000}
            value={releaseNote}
            onChange={(event) => setReleaseNote(event.target.value)}
            placeholder={t("version_release_note_placeholder")}
            aria-describedby={`${cardTitleId}-note-hint`}
          />
          <span id={`${cardTitleId}-note-hint`} className="am-ci-hint">{t(submitAction ? "version_test_release_note_hint_review" : "version_test_release_note_hint")}</span>
        </div>
      )}
      <CardStatus status={status?.tone === "error" ? status : null} />
    </Dialog>
  ) : null;

  const testCard = (
    <section className="am-ci-card am-ci-test" aria-labelledby={cardTitleId} data-state={state} data-surface={surface} aria-busy={busy !== null || undefined}>
      {card}
      {/* Live Datarc reads (design agent-eval-datarc-read-only §3.4): whose access the data cases used, said once. */}
      {changes?.datarc_owner_access ? (
        <p className="am-ci-note">{changes.datarc_owner_access.owner_name
          ? t("version_datarc_owner_access_note", { owner: changes.datarc_owner_access.owner_name })
          : t("version_datarc_owner_access_note_unnamed")}</p>
      ) : null}
      {!evaluate && changes && candidate ? (
        <CaseChangesDetails
          ref={changesRef}
          agentId={agentId}
          candidateId={candidate.candidate_id}
          changes={changes}
          live={live}
          frozenAt={snapshot?.created_at ?? null}
          overview={workbench.comparison_overview ?? null}
          progress={progress}
          onOpenCase={setAnswersFor}
          format={format}
          open={changesOpen ?? state !== "passed"}
          onOpenChange={setChangesOpen}
        />
      ) : null}
      {!evaluate ? <div className="am-ci-test-more">{cardMenu}</div> : null}
      <CardStatus status={releasing ? null : status} />
    </section>
  );

  const answersDialog = answersFor && candidate && changesExecutionId ? (
    <CaseAnswersDialog
      agentId={agentId}
      candidateId={candidate.candidate_id}
      executionId={changesExecutionId}
      item={answersFor}
      live={live}
      onClose={() => setAnswersFor(null)}
      format={format}
    />
  ) : null;

  if (evaluate) {
    return (
      <>
        {dialog}
        {releaseDialog}
        <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
        {testCard}
        {answersDialog}
      </>
    );
  }

  const coachPanel = (
    <CoachCIPanel
      ref={coachRef}
      agentId={agentId}
      liveSequence={live}
      candidateSequence={candidateSeq}
      proposals={workbench.change_proposals ?? []}
      suites={suiteRows}
      expanded={coachExpanded}
      onExpandedChange={setCoachExpanded}
      onPendingChange={setCoachPending}
      onPresenceChange={setCoachVisible}
      onViewResults={changes ? viewChanges : undefined}
      onChanged={async () => {
        await onChanged();
        await reload();
      }}
    />
  );

  return (
    <div className="am-ci-split" data-coach={coachVisible ? "true" : undefined}>
      {dialog}
      {releaseDialog}
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
      <div className="am-ci-col">
        {showChangeSummary && workbench.draft_changes ? (
          <AgentThisChangeCard
            changes={workbench.draft_changes}
            state={state}
            regressed={regressed}
            inconclusive={inconclusive}
            live={live}
            pendingCount={pendingProposals(workbench.change_proposals ?? []).filter((proposal) => proposal.status === "open").length}
            suites={suiteRows}
            onOpenConfig={onOpenConfig}
            onOpenEvaluate={onOpenEvaluate}
            onViewCheck={() => {
              // Bring the test card into view and move focus to its title, so the link visibly lands there.
              const title = document.getElementById(cardTitleId);
              title?.closest("section")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
              title?.focus({ preventScroll: true });
            }}
          />
        ) : null}
        {timeline ? <CiRunTimeline steps={timeline} format={format} /> : null}
        <VersionRail slots={slots} />
        {testCard}
      </div>
      <div className="am-ci-col am-ci-aside" hidden={!coachVisible}>{coachPanel}</div>
      {answersDialog}
    </div>
  );
}

/** Team settings › Models › Model assignments, styled as a button; a link so deployment prefixes and new tabs work. */
/** Team settings › Data Sources, where a team administrator adds or updates the Team's Datarc connection. */
function DataSourceSettingsLink() {
  const t = useTranslations("agents_marketplace");
  return (
    <a className="settings-button settings-button--primary settings-button--md" href={appPath(teamSettingsPath("datasource"))}>
      {t("version_open_datasource_settings")}
    </a>
  );
}

function ModelsSettingsLink() {
  const t = useTranslations("agents_marketplace");
  return (
    <a className="settings-button settings-button--primary settings-button--md" href={appPath(JUDGE_MODEL_SETTINGS_PATH)}>
      {t("version_open_models_settings")}
    </a>
  );
}

/**
 * "Evaluation can't use a model: <role>" with the fix: configurers are pointed to Team settings › Models
 * (the card also links there); everyone else asks a team administrator.
 */
function modelUnavailableText(cause: ApiError, canConfigure: boolean, t: ReturnType<typeof useTranslations>): string {
  const details = (cause.details ?? {}) as { model_role?: unknown };
  const role = t(details.model_role === "judge" ? "version_model_role_judge" : "version_model_role_main");
  return t(canConfigure ? "version_test_error_model_unavailable_admin" : "version_test_error_model_unavailable", { role });
}

/** Release refusals (activate / submit) that name their own fix. */
const RELEASE_ERROR_COPY: Record<string, string> = {
  agent_eval_override_required: "version_release_error_override_required",
  agent_eval_candidate_stale: "version_release_error_stale",
  agent_eval_evidence_stale: "version_release_error_stale",
  agent_eval_snapshot_hash_conflict: "version_release_error_stale",
  agent_eval_plan_hash_conflict: "version_release_error_stale",
  agent_eval_override_hash_conflict: "version_release_error_stale",
  agent_eval_control_version_conflict: "version_release_error_stale",
  agent_release_candidate_version_conflict: "version_release_error_stale",
  agent_release_candidate_state_conflict: "version_release_error_stale",
  agent_eval_release_run_incomplete: "version_release_error_incomplete",
  agent_release_audience_action_mismatch: "version_release_error_audience_changed",
  agent_release_private_review_not_required: "version_release_error_audience_changed",
  agent_release_test_state_changed: "version_release_error_test_changed",
  agent_release_draft_changed: "version_release_error_draft_changed",
  agent_release_test_running: "version_release_error_incomplete",
  agent_release_not_private: "version_release_error_audience_changed",
};

/** Release refusals after which the page shows stale state; the card rereads it so the next action is current. */
const RELEASE_RELOAD_CODES = new Set(Object.keys(RELEASE_ERROR_COPY));

/** API errors of "Test this change" that name their own fix. */
const TEST_ERROR_COPY: Record<string, string> = {
  agent_version_test_no_draft: "version_test_error_no_draft",
  agent_version_test_in_review: "version_test_error_in_review",
  agent_version_test_rebase_required: "version_card_rebase_body",
  agent_eval_unavailable: "version_test_error_eval_unavailable",
  team_model_unconfigured: "version_test_error_team_model",
  agent_eval_comparison_active: "version_test_error_running",
  agent_eval_workspace_required: "version_workspace_required",
};

const GATE_REASON_COPY: Record<string, string> = {
  agent_safety_blocked: "version_reason_agent_safety_blocked",
  attempt_infrastructure_incomplete: "version_reason_attempt_infrastructure_incomplete",
  attempt_model_unavailable: "version_reason_attempt_model_unavailable",
  run_model_unavailable: "version_reason_attempt_model_unavailable",
  run_model_config_changed: "version_reason_run_model_config_changed",
  attempt_judge_unavailable: "version_reason_attempt_judge_unavailable",
  attempt_judge_not_configured: "version_reason_attempt_judge_not_configured",
  attempt_agent_model_rate_limited: "version_reason_attempt_agent_model_rate_limited",
  attempt_agent_model_auth_failed: "version_reason_attempt_agent_model_auth_failed",
  attempt_agent_model_timeout: "version_reason_attempt_agent_model_timeout",
  attempt_agent_model_unavailable: "version_reason_attempt_agent_model_unavailable",
  attempt_agent_model_request_rejected: "version_reason_attempt_agent_model_request_rejected",
  attempt_agent_model_context_overflow: "version_reason_attempt_agent_model_context_overflow",
  attempt_tool_failed: "version_reason_attempt_tool_failed",
  attempt_cancelled: "version_reason_attempt_cancelled",
  attempt_time_limit_exceeded: "version_reason_attempt_time_limit_exceeded",
  baseline_result_missing: "version_reason_baseline_result_missing",
  candidate_result_missing: "version_reason_candidate_result_missing",
  comparison_cost_budget_exceeded: "version_reason_comparison_cost_budget_exceeded",
  comparison_duration_budget_exceeded: "version_reason_comparison_duration_budget_exceeded",
  compatibility_unknown: "version_reason_compatibility_unknown",
  critical_regression: "version_reason_critical_regression",
  non_critical_regression: "version_reason_non_critical_regression",
  flaky_limit_exceeded: "version_reason_flaky_limit_exceeded",
  minimum_delta_not_met: "version_reason_minimum_delta_not_met",
  owner_quality_skipped: "version_reason_owner_quality_skipped",
  point_regression: "version_reason_point_regression",
  point_unmet: "version_reason_point_unmet",
  provider_identity_attributed_only: "version_reason_provider_identity_attributed_only",
  provider_identity_incomparable: "version_reason_provider_identity_incomparable",
  provider_identity_unknown: "version_reason_provider_identity_unknown",
  required_attempts_missing: "version_reason_required_attempts_missing",
  required_quality_failed: "version_reason_required_quality_failed",
  required_tool_not_run_in_eval: "version_reason_required_tool_not_run_in_eval",
  tool_not_run_in_eval: "version_reason_tool_not_run_in_eval",
  data_changed_during_test: "version_reason_data_changed_during_test",
  ...Object.fromEntries(
    [
      "no_connection",
      "connection_unusable",
      "connection_rejected",
      "starter_not_owner",
      "owner_identity_unverified",
      "no_workspace",
      "owner_not_member",
      "datarc_unreachable",
      "datarc_busy",
      "datarc_timeout",
      "owner_permission_denied",
    ].map((cause) => [`attempt_datarc_unavailable_${cause}`, `version_reason_attempt_datarc_unavailable_${cause}`]),
  ),
  required_result_inconclusive: "version_reason_required_result_inconclusive",
  evaluation_plan_changed: "version_reason_evaluation_plan_changed",
  base_release_changed: "version_reason_base_release_changed",
  audience_changed: "version_reason_audience_changed",
  agent_owner_changed: "version_reason_agent_owner_changed",
};

/**
 * Release-check reason codes are machine identifiers; the Owner reads the reason. An unknown code
 * reads "Another issue needs attention"; the code itself is only in Copy diagnostics.
 */
export function gateReasonKey(code: string): string {
  return GATE_REASON_COPY[code] ?? "version_reason_other";
}

function caseName(item: Pick<AgentVersionCaseChange, "case_label">, t: ReturnType<typeof useTranslations>): string {
  return item.case_label || t("version_case_unnamed");
}

/**
 * The text a reader sees in a stored answer. Servers since 2026-10-21 send it already; an older server sends an
 * Agent's structured answer as JSON, whose Chat text is `exec_summary`.
 */
export function answerText(answer: string): string {
  const text = answer.trim();
  if (!text.startsWith("{")) return answer;
  try {
    const value = JSON.parse(text) as { exec_summary?: unknown };
    return typeof value.exec_summary === "string" && value.exec_summary.trim() ? value.exec_summary.trim() : answer;
  } catch {
    return answer;
  }
}

/** The cases a time limit or a Tool that can't run left without a result, by name: 「用例 A」、「用例 B」. */
/** Tools by the names the Owner knows them under, quoted and listed: 「查询数据」、「查看数据字段」. */
export function listedToolNames(tools: readonly string[], t: ReturnType<typeof useTranslations>): string {
  return tools.map((tool) => t("version_quoted", { name: toolLabel(tool).name })).join(t("version_list_comma"));
}

function unfinishedCaseNames(items: readonly AgentVersionUnfinishedCase[], t: ReturnType<typeof useTranslations>): string {
  return items.map((item) => t("version_quoted", { name: caseName(item, t) })).join(t("version_list_comma"));
}

/** A case whose attempts split (the "unstable" reason), from the changed list or, with other causes, the unfinished list. */
export interface UnstableCase {
  item: AgentVersionCaseChange;
  baseline: AgentVersionCasePassCount | null;
  candidate: AgentVersionCasePassCount | null;
}

/** Every case the `flaky_limit_exceeded` reason stands for, so the row can name each one (never only a count). */
export function unstableCases(changes: Pick<AgentVersionCaseChanges, "items" | "unfinished"> | null | undefined): UnstableCase[] {
  const flaky = (codes: readonly string[]) => codes.includes("flaky_limit_exceeded");
  const listed = (changes?.items ?? [])
    .filter((item) => item.change === "unstable" && flaky(item.reason_codes))
    .map((item) => ({ item, baseline: item.baseline, candidate: item.candidate }));
  const unfinished = (changes?.unfinished ?? [])
    .filter((item) => flaky(item.reason_codes))
    .map((item) => ({
      item: { ...item, change: "unstable" as const, classification: "inconclusive", key_case: false, baseline: item.baseline ?? null, candidate: item.candidate ?? { passed: 0, runs: 0 } },
      baseline: item.baseline ?? null,
      candidate: item.candidate ?? null,
    }));
  return [...listed, ...unfinished];
}

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * One side's attempts in plain words: "这次修改 3 次里 2 次通过、1 次没通过" / "v13 3 次都通过". Attempts without a
 * verdict are named separately; an older server without `failed` counts the rest as failed.
 */
export function attemptsSentence(side: string, count: AgentVersionCasePassCount, t: Translate): string {
  const failed = count.failed ?? count.runs - count.passed;
  const undecided = Math.max(0, count.runs - count.passed - failed);
  if (count.runs > 0 && count.passed === count.runs) return t("version_attempts_all_passed", { side, runs: count.runs });
  if (count.runs > 0 && failed === count.runs) return t("version_attempts_all_failed", { side, runs: count.runs });
  const split = t("version_attempts_split", { side, runs: count.runs, passed: count.passed, failed });
  return undecided ? `${split}${t("version_attempts_undecided", { count: undecided })}` : split;
}

/** Both sides of an unstable case: the change first, then the released version it was compared with. */
export function unstableCaseSentence(entry: UnstableCase, live: number | undefined, t: Translate): string {
  const sides = [
    entry.candidate ? attemptsSentence(t("version_rail_change"), entry.candidate, t) : null,
    entry.baseline && live !== undefined ? attemptsSentence(`v${live}`, entry.baseline, t) : null,
  ].filter((part): part is string => Boolean(part));
  return sides.join(t("version_list_semicolon"));
}

/** The one plain line the private release dialog shows about the test, or null when it passed. */
export function releaseTestLine(
  state: AgentReleaseTestState,
  reasons: readonly string[],
  changes: Pick<AgentVersionCaseChanges, "items" | "unfinished" | "summary"> | null | undefined,
  t: Translate,
): string | null {
  switch (state) {
    case "passed":
      return null;
    case "passed_with_risks":
      return t("version_release_test_risks", { count: Math.max(1, reasons.length) });
    case "regressed": {
      const worse = (changes?.items ?? []).filter((item) => item.change === "worse");
      return worse.length
        ? t("version_release_test_regressed_cases", { cases: worse.map((item) => t("version_quoted", { name: item.case_label || t("version_case_unnamed") })).join(t("version_list_comma")) })
        : t("version_release_test_regressed");
    }
    case "safety_failed":
      return t("version_release_test_safety_failed");
    case "failed":
      return t("version_release_test_failed", { count: Math.max(1, changes?.summary.unchanged_required_failures ?? 1) });
    case "no_conclusion": {
      const unstable = unstableCases(changes).length;
      return reasons.includes("flaky_limit_exceeded") && unstable
        ? t("version_release_test_no_conclusion_unstable", { count: unstable })
        : t("version_release_test_no_conclusion");
    }
    case "could_not_run":
      return t("version_release_test_could_not_run");
    case "outdated":
      return t("version_release_test_outdated");
    case "not_tested":
      return t("version_release_test_not_tested");
  }
}

function ratio(count: { passed: number; runs: number } | null): string {
  return count ? `${count.passed} / ${count.runs}` : "—";
}

function passed(count: { passed: number; runs: number } | null, t: ReturnType<typeof useTranslations>): string {
  return count ? t("version_case_passed", { passed: count.passed, runs: count.runs }) : t("version_case_not_run");
}

/**
 * An expired test case names itself: the case (by its task when this page has the suite loaded), its suite and the
 * expiry date, plus what to do. Editing a case keeps its expiry, so the fix is removing it (or adding it anew).
 */
export function fixtureExpiredText(
  cause: ApiError,
  suites: AgentEvalSuite[],
  format: Pick<CiFormat, "dateTime">,
  t: ReturnType<typeof useTranslations>,
): string {
  // Safe identifiers the gateway publishes next to the code (`detail.error.details`).
  const details = cause.details ?? {};
  const text = (value: unknown) => (typeof value === "string" && value ? value : null);
  const caseKey = text(details.case_key);
  const suite = suites.find((item) => item.revision?.suite_revision_id === text(details.suite_revision_id)) ?? null;
  const saved = suite?.revision?.cases.find((item) => item.case_key === caseKey);
  const task = (saved?.task ?? saved?.input?.task ?? "").trim();
  const name = task ? (task.length > 60 ? `${task.slice(0, 59)}…` : task) : caseKey;
  const until = text(details.valid_until);
  if (!name || !until) return t("version_fixture_expired_unnamed");
  const values = { case: name, date: format.dateTime(until) };
  if (suite?.platform_managed) return t("version_fixture_expired_platform", values);
  return suite ? t("version_fixture_expired", { ...values, suite: suite.name }) : t("version_fixture_expired_unknown_suite", values);
}

/** Reason codes of a blocked publication, safe to show by code. The gateway sends them as
 * `detail.error.details.reason_codes`; the flatter shapes are kept for older responses. */
function blockedReasonCodes(cause: ApiError): string[] {
  const body = cause.body as {
    detail?: { reason_codes?: unknown; error?: { details?: { reason_codes?: unknown } } };
    reason_codes?: unknown;
  } | null | undefined;
  const details = cause.details as { reason_codes?: unknown } | null | undefined;
  const raw = body?.detail?.error?.details?.reason_codes ?? body?.detail?.reason_codes ?? body?.reason_codes ?? details?.reason_codes;
  return Array.isArray(raw) ? raw.filter((code): code is string => typeof code === "string" && code.length > 0) : [];
}

const GATE_SCORER_LABELS: Record<string, string> = {
  terminal_contract: "version_gate_scorer_terminal_contract",
  output_schema: "version_gate_scorer_output_schema",
  tool_contract: "version_gate_scorer_tool_contract",
  permission_denial: "version_gate_scorer_permission_denial",
  effect_policy: "version_gate_scorer_effect_policy",
  citation: "version_gate_scorer_citation",
  no_fabrication: "version_gate_scorer_no_fabrication",
  budget_timeout: "version_gate_scorer_budget_timeout",
};

interface FailedGateCheck {
  caseKey: string;
  scorers: Array<{ name: string; detail: string }>;
}

/**
 * The failing checks of the newest failed run on this candidate, read from the run's own scorer rows.
 * The gate reason alone ("a required case failed") names no check; the owner needs to know which one.
 */
function useFailedGateCheck(agentId: string | undefined, candidateRevisionId: string | null | undefined, enabled: boolean): FailedGateCheck | null {
  const [failure, setFailure] = useState<FailedGateCheck | null>(null);
  useEffect(() => {
    if (!enabled || !agentId || !candidateRevisionId) {
      setFailure(null);
      return undefined;
    }
    let cancelled = false;
    void (async () => {
      try {
        const manager = await getAgentEvalManager(agentId);
        const run = (manager.runs ?? []).find((item) => item.target_kind === "candidate" && item.revision_id === candidateRevisionId && item.status === "failed");
        if (!run) return;
        const detail = await getAgentEvalRun(agentId, run.run_id);
        const result = (detail.results ?? []).find((item) => item.status === "failed");
        const rows = (result?.deterministic as { scorers?: Array<{ name?: string; passed?: boolean; detail?: string; applicable?: boolean; advisory?: boolean }> } | undefined)?.scorers ?? [];
        // A check with nothing to check (no declared expectation, no data read) or an advisory one never explains a failure.
        const failed = rows.filter((row) => row.passed === false && row.applicable !== false && row.advisory !== true && typeof row.name === "string");
        if (!cancelled && result && failed.length) {
          setFailure({ caseKey: result.case_key, scorers: failed.map((row) => ({ name: row.name as string, detail: row.detail ?? "" })) });
        }
      } catch {
        // The generic reason stays; detail is a best-effort addition.
      }
    })();
    return () => { cancelled = true; };
  }, [agentId, candidateRevisionId, enabled]);
  return failure;
}

/** Reason rows: 13/600 reason + 12 explanation + an entry to handle it (spec 3d). */
function ReasonList({ codes, tone, changes, live, onOpenCase, agentId, candidateRevisionId }: {
  codes: string[];
  tone: "neg" | "warn";
  agentId?: string;
  candidateRevisionId?: string | null;
  changes?: AgentVersionCaseChanges | null;
  live?: number;
  onOpenCase?: (item: AgentVersionCaseChange) => void;
  onViewChanges?: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const Icon = tone === "neg" ? CircleX : TriangleAlert;
  const worse = (changes?.items ?? []).filter((item) => item.change === "worse");
  const unstable = unstableCases(changes);
  const needsRunDetail = codes.includes("required_quality_failed") && !(changes?.items ?? []).some((item) => item.change === "required_failure");
  const failedCheck = useFailedGateCheck(agentId, candidateRevisionId, needsRunDetail);
  const unfinished = changes?.unfinished ?? [];
  const toolNotRun = (code: string) => unfinished.filter((item) => item.reason_codes.includes(code));
  const timedOut = unfinished.filter((item) => item.reason_codes.some((reason) => TIME_LIMIT_REASONS.has(reason)));
  return (
    <ul className="am-ci-rs">
      {codes.map((code) => {
        let detail: ReactNode = null;
        let action: ReactNode = null;
        let title = t(gateReasonKey(code) as never);
        const critical = worse.find((item) => item.key_case);
        const nonCritical = worse.find((item) => !item.key_case);
        const requiredFailure = changes?.items.find((item) => item.change === "required_failure");
        if (code === "required_quality_failed" && requiredFailure) {
          detail = t("version_reason_detail_required_failure", { name: caseName(requiredFailure, t), suite: requiredFailure.suite_name || t("version_case_unnamed"), result: ratio(requiredFailure.candidate), count: changes?.summary.unchanged_required_failures ?? 1 });
          if (onOpenCase) action = <Button size="sm" variant="ghost" onClick={() => onOpenCase(requiredFailure)}>{t("version_view_case")}</Button>;
        } else if (code === "required_quality_failed" && failedCheck) {
          detail = t("version_gate_failed_checks", {
            case: failedCheck.caseKey,
            items: failedCheck.scorers
              .map((row) => {
                const label = GATE_SCORER_LABELS[row.name];
                const name = label ? t(label as never) : row.name;
                return row.detail ? t("version_gate_failed_check_item", { name, detail: row.detail }) : name;
              })
              .join(t("version_list_semicolon")),
          });
        } else if (code === "critical_regression" && critical && live !== undefined) {
          detail = t("version_reason_detail_critical", { name: caseName(critical, t), live: `v${live}`, before: ratio(critical.baseline), after: ratio(critical.candidate) });
          if (onOpenCase) action = <Button size="sm" variant="ghost" onClick={() => onOpenCase(critical)}>{t("version_view_case")}</Button>;
        } else if (code === "non_critical_regression" && nonCritical) {
          detail = t("version_reason_detail_regression", { name: caseName(nonCritical, t), before: ratio(nonCritical.baseline), after: ratio(nonCritical.candidate) });
          if (onOpenCase) action = <Button size="sm" variant="ghost" onClick={() => onOpenCase(nonCritical)}>{t("version_view_case")}</Button>;
        } else if ((code === "required_tool_not_run_in_eval" || code === "tool_not_run_in_eval") && toolNotRun(code).length) {
          // Name the Tool and the cases, so the Owner knows which point to change (or which cases can't be tested yet).
          const affected = toolNotRun(code);
          const tools = [...new Set(affected.flatMap((item) => item.tools))];
          detail = tools.length
            ? t("version_reason_detail_tool_not_run", {
              tools: code === "tool_not_run_in_eval" ? listedToolNames(tools, t) : quotedToolNames(tools, t),
              cases: unfinishedCaseNames(affected, t),
            })
            : t("version_reason_detail_cases", { cases: unfinishedCaseNames(affected, t) });
          if (onOpenCase) {
            // The case's attempts, each saying which Tool couldn't run (a case without a result has no change row).
            const first = affected[0];
            action = <Button size="sm" variant="ghost" onClick={() => onOpenCase({ ...first, change: "unstable", classification: "inconclusive", key_case: false, baseline: null, candidate: { passed: 0, runs: 0 } })}>{t("version_view_case")}</Button>;
          }
        } else if (code === DATA_CHANGED_REASON && unfinished.some((item) => item.reason_codes.includes(code))) {
          // The same row query read different data on the two sides: not compared; testing again can read stable data.
          const affected = unfinished.filter((item) => item.reason_codes.includes(code));
          detail = t("version_reason_detail_data_changed_during_test", { cases: unfinishedCaseNames(affected, t) });
        } else if (TIME_LIMIT_REASONS.has(code) && timedOut.length) {
          detail = t("version_reason_detail_time_limit", { cases: unfinishedCaseNames(timedOut, t) });
        } else if (ATTEMPT_FAILURE_REASONS.has(code)) {
          // Name the cases, the Tool, the cancellation cause or the diagnostic reference, with the fix.
          const affected = unfinished.filter((item) => item.reason_codes.includes(code));
          const failures = affected.flatMap((item) => item.failures ?? []).filter((failure) => failure.reason_code === code);
          const tools = [...new Set(failures.flatMap((failure) => failure.tool ? [failure.tool] : []))];
          const roles = failures.flatMap((failure) => failure.role ? [failure.role] : []);
          detail = attemptReasonDetail(code, {
            owner: failures.find((failure) => failure.owner_name)?.owner_name ?? null,
            workspace: failures.find((failure) => failure.workspace)?.workspace ?? null,
            cases: unfinishedCaseNames(affected, t),
            tools,
            toolNames: tools.length ? quotedToolNames(tools, t) : undefined,
            role: modelRoleLabel(roles[0], (key, values) => t(key as never, values as never)),
            causes: failures.map((failure) => failure.cause),
            references: [...new Set(failures.flatMap((failure) => failure.references))].slice(0, 3),
          }, (key, values) => t(key as never, values as never));
          if (onOpenCase && affected[0]) {
            // The case's attempts with their causes and references (a case without a result has no change row).
            const first = affected[0];
            action = <Button size="sm" variant="ghost" onClick={() => onOpenCase({ ...first, change: "unstable", classification: "inconclusive", key_case: false, baseline: null, candidate: { passed: 0, runs: 0 } })}>{t("version_view_case")}</Button>;
          }
        } else if (code === "flaky_limit_exceeded" && unstable.length) {
          // Name every unstable case with how its attempts split on both sides, and open its answers.
          title = t("version_reason_flaky_count", { count: unstable.length });
          detail = (
            <ul className="am-ci-ri-cases">
              {unstable.map((entry) => (
                <li key={`${entry.item.suite_revision_id}:${entry.item.case_revision_id}`}>
                  <span>
                    <b>{t("version_quoted", { name: caseName(entry.item, t) })}</b>
                    {t("version_list_colon")}
                    {unstableCaseSentence(entry, live, (key, values) => t(key as never, values as never))}
                  </span>
                  {onOpenCase ? <Button size="sm" variant="ghost" onClick={() => onOpenCase(entry.item)}>{t("version_view_answers")}</Button> : null}
                </li>
              ))}
            </ul>
          );
        }
        return (
          <li key={code} className="am-ci-ri">
            <Icon size={14} aria-hidden className={tone === "neg" ? "am-ci-c-neg" : "am-ci-c-warn"} />
            <div>
              <strong>{title}</strong>
              {typeof detail === "string" ? <span>{detail}</span> : detail}
            </div>
            {action ?? <span />}
          </li>
        );
      })}
    </ul>
  );
}

const CHANGE_TONE = { required_failure: "danger", worse: "danger", unstable: "warning", better: "positive" } as const;
const CHANGE_RANK = { required_failure: 0, worse: 1, unstable: 2, better: 3 } as const;
const PREVIEW_ROWS = 5;

/** Locale-independent code-point order (the server's second key), so refreshes never reshuffle rows. */
function codePointOrder(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const delta = left[index].codePointAt(0)! - right[index].codePointAt(0)!;
    if (delta) return delta;
  }
  return left.length - right.length;
}

/** Worse → unstable → better, then case name; the server order already does this, kept stable here. */
export function orderCaseChanges(items: AgentVersionCaseChange[]): AgentVersionCaseChange[] {
  return [...items].sort((a, b) => CHANGE_RANK[a.change] - CHANGE_RANK[b.change]
    || codePointOrder(a.case_label ?? "", b.case_label ?? "")
    || codePointOrder(a.case_key ?? "", b.case_key ?? "")
    || codePointOrder(a.case_revision_id, b.case_revision_id));
}

/**
 * The per-case comparison inside the test card, behind "Comparison details". The card head already gives
 * the outcome and the better/same/worse counts, so this shows only what the head doesn't: run facts,
 * pass rates and the changed cases.
 */
function CaseChangesDetailsInner({ agentId, candidateId, changes, live, frozenAt, overview, progress, onOpenCase, format, open, onOpenChange }: {
  agentId: string;
  candidateId: string;
  changes: AgentVersionCaseChanges;
  live: number;
  frozenAt: string | null;
  overview: AgentVersionComparisonOverview | null;
  progress: AgentEvalProgress | undefined;
  onOpenCase: (item: AgentVersionCaseChange) => void;
  format: CiFormat;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}, ref: ForwardedRef<HTMLElement>) {
  const t = useTranslations("agents_marketplace");
  const [expanded, setExpanded] = useState(false);
  const [items, setItems] = useState(changes.items);
  const [nextOffset, setNextOffset] = useState(changes.next_offset);
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => {
    setItems(changes.items);
    setNextOffset(changes.next_offset);
    setExpanded(false);
  }, [changes]);
  const ordered = useMemo(() => orderCaseChanges(items), [items]);
  const changedTotal = changes.summary.unchanged_required_failures + changes.summary.worse + changes.summary.unstable + changes.summary.better;
  const visible = expanded ? ordered : ordered.slice(0, PREVIEW_ROWS);
  const same = Math.max(0, changes.summary.case_count - changedTotal - changes.summary.not_comparable);
  const meta = [
    frozenAt ? t("version_frozen_at", { date: format.dateTime(frozenAt) }) : null,
    progress?.elapsed_ms ? t("version_took", { value: format.elapsed(progress.elapsed_ms) }) : null,
    progress && progress.usage.tokens !== null ? t("version_usage_tokens", { value: format.tokens(progress.usage.tokens) }) : null,
    progress ? t("version_usage_requests", { value: format.count(progress.model_calls.used) }) : null,
  ].filter(Boolean).join(t("version_list_separator"));
  const showAll = async () => {
    setExpanded(true);
    if (nextOffset === null || loadingMore) return;
    setLoadingMore(true);
    try {
      let offset: number | null = nextOffset;
      let collected = items;
      while (offset !== null) {
        const page = await getAgentVersionCaseChanges(agentId, candidateId, changes.execution_id, { offset });
        collected = [...collected, ...page.items];
        offset = page.next_offset;
      }
      setItems(collected);
      setNextOffset(null);
    } catch {
      // Keep the rows already shown; the full list can be retried from the button.
      setExpanded(false);
    } finally {
      setLoadingMore(false);
    }
  };
  return (
    <details
      ref={ref as ForwardedRef<HTMLDetailsElement>}
      tabIndex={-1}
      className="am-ci-details am-ci-changes"
      open={open}
      onToggle={(event) => { if (event.currentTarget.open !== open) onOpenChange(event.currentTarget.open); }}
    >
      <summary>{t("version_changes_details")}</summary>
      {changes.summary.not_comparable > 0 ? <p className="am-ci-note" data-tone="warning">{t("version_changes_title_incomplete", { live: `v${live}` })}</p> : null}
      {meta ? <p className="am-ci-meta">{meta}</p> : null}
      {overview ? <ComparisonOverviewLine overview={overview} live={live} format={format} undecided={changes.summary.not_comparable} /> : null}
      {changes.summary.unchanged_required_failures || changes.summary.unstable || changes.summary.not_comparable || changedTotal ? (
        <div className="am-ci-sum">
          {changes.summary.unchanged_required_failures ? <Badge tone="danger">{t("version_change_required_failure_count", { count: changes.summary.unchanged_required_failures })}</Badge> : null}
          {changedTotal ? <Badge tone="neutral">{t("version_change_same_count", { count: same })}</Badge> : null}
          {changes.summary.unstable ? <Badge tone="warning">{t("version_change_unstable_count", { count: changes.summary.unstable })}</Badge> : null}
          {changes.summary.not_comparable ? <span className="am-ci-note">{t("version_change_not_comparable", { count: changes.summary.not_comparable })}</span> : null}
        </div>
      ) : null}
      {changedTotal ? (
        <table className="am-ci-dt">
          <caption className="sr-only">{t("version_changes_caption")}</caption>
          <thead>
            <tr>
              <th scope="col">{expanded ? t("version_changes_all_head", { count: changedTotal }) : t("version_changes_head", { shown: Math.min(PREVIEW_ROWS, changedTotal), count: changedTotal })}</th>
              <th scope="col">{`v${live}`}</th>
              <th scope="col">{t("version_rail_change")}</th>
              <th scope="col" className="am-ci-dt-x">{t("version_changes_change")}</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((item) => (
              <tr key={`${item.suite_revision_id}:${item.case_revision_id}`}>
                <td>
                  <button type="button" className="am-ci-dt-name" onClick={() => onOpenCase(item)}>{caseName(item, t)}</button>
                  {item.key_case ? <small>{t("version_case_critical")}</small> : null}
                  {caseBaselineLabel(item) ? <small data-baseline={caseBaselineLabel(item)}>{t(`version_case_baseline_${caseBaselineLabel(item)}` as never)}</small> : null}
                </td>
                <td className="am-ci-dt-r">{passed(item.baseline, t)}</td>
                <td className="am-ci-dt-r" data-change={item.change}>{passed(item.candidate, t)}</td>
                <td className="am-ci-dt-x"><Badge size="sm" tone={CHANGE_TONE[item.change]}>{t(`version_change_${item.change}` as never)}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="am-ci-note">{t("version_changes_none")}</p>}
      {changedTotal > PREVIEW_ROWS && !expanded ? (
        <div className="am-ci-acts"><Button variant="ghost" loading={loadingMore} onClick={() => void showAll()}>{t("version_changes_view_all", { count: changedTotal })}</Button></div>
      ) : null}
      {changes.split?.length ? (
        <div className="am-ci-majority">
          <p className="am-ci-note">{t("version_changes_split_title", { count: changes.split.length })}</p>
          <ul className="am-ci-ri-cases">
            {changes.split.map((entry) => {
              const item: AgentVersionCaseChange = { ...entry, change: "unstable", reason_codes: [], key_case: false };
              return (
                <li key={`${entry.suite_revision_id}:${entry.case_revision_id}`}>
                  <span>
                    <b>{t("version_quoted", { name: caseName(item, t) })}</b>
                    {t("version_list_colon")}
                    {unstableCaseSentence({ item, baseline: entry.baseline, candidate: entry.candidate }, live, (key, values) => t(key as never, values as never))}
                  </span>
                  <Button size="sm" variant="ghost" onClick={() => onOpenCase(item)}>{t("version_view_answers")}</Button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </details>
  );
}


/** Eval credibility R15: how many cases ran and how often each version passed, before the per-case rows. */
export function ComparisonOverviewLine({ overview, live, format, undecided = 0 }: {
  overview: AgentVersionComparisonOverview;
  live: number;
  format: CiFormat;
  /** Cases without a verdict (not comparable); pass rates only count the others, so say how many. */
  undecided?: number;
}) {
  const t = useTranslations("agents_marketplace");
  const rate = (version: string, value: number | null) => (
    value === null ? t("version_overview_rate_none", { version }) : t("version_overview_rate", { version, rate: format.percent(value) })
  );
  return (
    <p className="am-ci-meta">
      <span>{t("version_overview_cases", { count: overview.cases })}</span>
      {undecided > 0 ? <span>{t("version_overview_undecided", { count: undecided })}</span> : null}
      <span>{rate(t("version_rail_change"), overview.candidate_pass_rate)}</span>
      <span>{rate(`v${live}`, overview.live_pass_rate)}</span>
    </p>
  );
}

const CaseChangesDetails = forwardRef(CaseChangesDetailsInner);

/** The attempt couldn't be tested: a Tool the case requires, or one its answer depended on, can't run in evaluation. */
function notTestable(entry: Pick<AgentVersionCaseAnswerAttempt, "status" | "failure_code">): boolean {
  return entry.status === "infra_failed" && TOOL_NOT_RUN_FAILURE_CODES.has(entry.failure_code ?? "");
}

function attemptStatusKey(status: string): string {
  return ["passed", "failed", "judge_unknown", "infra_failed", "cancelled"].includes(status) ? `version_answer_status_${status}` : "version_answer_status_other";
}

function CaseAnswersDialog({ agentId, candidateId, executionId, item, live, onClose, format }: {
  agentId: string;
  candidateId: string;
  executionId: string;
  item: AgentVersionCaseChange;
  live: number;
  onClose: () => void;
  format: CiFormat;
}) {
  const t = useTranslations("agents_marketplace");
  const [answers, setAnswers] = useState<AgentVersionCaseAnswers | null>(null);
  const [failed, setFailed] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setFailed(null);
    void getAgentVersionCaseAnswers(agentId, candidateId, executionId, item.case_revision_id).then((value) => {
      if (!cancelled) setAnswers(value);
    }).catch((cause) => {
      if (!cancelled) setFailed(cause ?? new Error("answers"));
    });
    return () => { cancelled = true; };
  }, [agentId, candidateId, executionId, item.case_revision_id, attempt]);
  const side = (label: string, value: AgentVersionCaseAnswers["baseline"]) => (
    <section className="am-ci-answer-side" aria-label={label}>
      <h4>{label}</h4>
      {!value || value.attempts.length === 0 ? <p className="am-ci-note">{t("version_answer_none_side")}</p> : value.attempts.map((entry) => (
        <article key={entry.attempt} className="am-ci-answer">
          <p className="am-ci-meta">
            <span>{t("version_answer_attempt", { attempt: entry.attempt })}</span>
            {entry.time_limit ? (
              // Stopped at a time limit: say which limit and how long it ran, not "environment failure".
              <span>{t("version_answer_status_time_limit", { limit: format.elapsed(entry.time_limit.limit_ms), observed: format.elapsed(entry.time_limit.observed_ms) })}</span>
            ) : <>
              <span>{t((notTestable(entry) ? "version_answer_status_not_testable" : attemptStatusKey(entry.status)) as never)}</span>
              {/* No recorded time (an attempt that never ran, or one that kept none) shows no duration, never "0 s". */}
              {entry.duration_ms ? <span>{format.elapsed(entry.duration_ms)}</span> : null}
            </>}
          </p>
          {entry.datarc ? (
            // Owner-only: how much Datarc data the attempt read live (never the rows themselves).
            <p className="am-ci-note">{t("version_answer_datarc_counts", { calls: entry.datarc.calls, queries: entry.datarc.queries, rows: entry.datarc.rows })}</p>
          ) : null}
          {entry.time_limit && entry.datarc?.calls ? <p className="am-ci-note" data-tone="warning">{t("version_answer_time_limit_datarc")}</p> : null}
          {notTestable(entry) ? (
            // Not the Agent's failure: the answer depended on a Tool evaluation can't run yet. Testing again won't help.
            <p className="am-ci-note" data-tone="warning">
              {entry.tools_not_run?.length
                ? t("version_answer_tool_not_run", { tools: listedToolNames(entry.tools_not_run, t) })
                : t("version_answer_tool_not_run_unnamed")}
            </p>
          ) : null}
          {entry.failure ? (
            // Why this attempt has no verdict, and the reference the server logged the error under.
            <p className="am-ci-note" data-tone="warning">
              {[t(gateReasonKey(entry.failure.reason_code) as never), entry.failure.tool ? t("version_quoted", { name: entry.failure.tool }) : null, t("version_answer_failure_reference", { reference: entry.failure.reference })].filter(Boolean).join(t("version_list_separator"))}
            </p>
          ) : null}
          {entry.answer ? (
            // Rendered as Chat renders an answer: the structured answer's text, with its Markdown tables and headings.
            <div className="am-ci-answer-text" data-markdown="true"><Markdown markdown={answerText(entry.answer)} allowImages={false} /></div>
          ) : entry.answer_withheld === "stopped_without_answer" ? (
            // The stored text was an instruction for a model (the failure breaker's reason); the Owner reads what happened.
            <p className="am-ci-note">{t("version_answer_stopped_without_answer")}</p>
          ) : notTestable(entry) ? null : <p className="am-ci-note">{t("version_answer_missing")}</p>}
          {entry.answer_truncated ? <p className="am-ci-note">{t("version_answer_truncated")}</p> : null}
          {entry.failed_checks?.length ? (
            <p className="am-ci-note" data-tone="warning">{t("version_answer_failed_checks", {
              checks: entry.failed_checks.map((name) => GATE_SCORER_LABELS[name] ? t(GATE_SCORER_LABELS[name] as never) : name).join(t("version_list_semicolon")),
            })}</p>
          ) : null}
          <AttemptPointVerdicts
            agentId={agentId}
            definitions={answers?.points ?? []}
            points={entry.points ?? []}
            caseResult={entry.run_id && answers ? { run_id: entry.run_id, case_revision_id: answers.case_revision_id, attempt: entry.attempt } : null}
          />
          {entry.unsourced_figures?.length ? (
            // Advisory only: figures derived from the data (sums, shares, rounding) appear here too.
            <p className="am-ci-note">{t("version_answer_unsourced_figures", { figures: entry.unsourced_figures.join(t("version_list_comma")) })}</p>
          ) : null}
        </article>
      ))}
    </section>
  );
  return (
    <Dialog title={caseName(item, t)} onClose={onClose} width={760}>
      <div className="am-ci-answers">
        {failed ? (
          <CardStatus status={{ tone: "error", message: t("version_answer_failed"), cause: failed, onRetry: () => setAttempt((value) => value + 1) }} />
        ) : !answers ? <LoadingSkeleton rows={3} /> : <>
          {answers.task ? <p className="am-ci-answer-task">{answers.task}</p> : null}
          <PointCalibrationNotices definitions={answers.points ?? []} />
          <div className="am-ci-answer-grid">
            {side(`v${live}`, answers.baseline)}
            {side(t("version_rail_change"), answers.candidate)}
          </div>
        </>}
      </div>
    </Dialog>
  );
}

