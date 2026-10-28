"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  cancelAgentEvalRun,
  getAgentEvalManager,
  getAgentEvalRun,
  runAgentEval,
  saveAgentEvalSuite,
  suggestAgentEvalCases,
  updateAgentEvalTriggerPolicy,
  type AgentEvalCase,
  type AgentEvalManager,
  type AgentEvalRun,
  type AgentEvalSuite,
} from "@/lib/agents-api";
import { caseWithCurrentDefaults, withCurrentScorerDefaults } from "@/lib/agent-eval-defaults";
import { reviewAgentEvalSuite, type SuiteReview } from "@/lib/agent-eval-alignment-api";
import { isHeldOut, isOnlyHeldOut, setHeldOut } from "@/lib/agent-eval-held-out";
import { useAgentEvalAlignmentCapability } from "@/lib/use-agent-eval-alignment-capability";
import { randomUuid } from "@/lib/random-id";
import { AnswerQuality, AgentEvalRunResults } from "./agent-eval-run-results";
import { ApiError } from "@/lib/api-client";
import { useSpaceSelection } from "@/lib/use-space";
import { CircleAlert, Clock, ShieldCheck } from "lucide-react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ChatCard, ChatCardButton } from "@/components/ui/card";
import { CheckRow } from "@/components/ui/check-row";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { Bot } from "lucide-react";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { releaseEvidence } from "@/lib/agent-release-evidence";
import { CardStatus, CiCardHead, useCiFormat } from "./agent-ci-shared";
import { casePoints, EvalCaseCard, EvalCasePoints, type EvalPointTool } from "./agent-eval-points";
import { quotedToolNames } from "./agent-eval-point-verdicts";
import { gateReasonKey, listedToolNames } from "./agent-version-validation";
import { TOOL_NOT_RUN_CODE, TOOL_NOT_RUN_FAILURE_CODES, attemptFailureReason, attemptReasonDetail, modelRoleLabel, storedAttemptFailure } from "@/lib/agent-eval-attempt-failure";
import { AgentEvalConversationPicker } from "./agent-eval-conversation-picker";

// The server accepts up to 200 cases per suite revision.
const MAX_SUITE_CASES = 200;
// Failure codes of attempts without a verdict that are not an evaluation environment fault.
const TIME_LIMIT_FAILURE_CODES = new Set(["agent_eval_attempt_timeout", "agent_eval_duration_budget_exceeded"]);

interface EditableCase extends AgentEvalCase {
  task: string;
}

function editableCase(index: number, task = ""): EditableCase {
  return {
    case_key: `quality.${index}`,
    task,
    fixture_ref: {},
    expected_contract: { allowed_terminal_statuses: ["completed"] },
    // Same defaults as server-side suggestions: no Tool-call cap and no critical fabrication check,
    // so a data Agent isn't failed for querying data to answer.
    scorer_config: {
      max_cost_microusd: 5_000_000,
      max_duration_ms: 120_000,
    },
    effect_policy: { mode: "no_effects" },
    valid_until: null,
  };
}

function caseTask(value: AgentEvalCase): string {
  return value.task ?? value.input?.task ?? "";
}

/**
 * The fields the save endpoint accepts; server-only fields such as `input` or `case_revision_id` stay out.
 * Cases are numbered `quality.N` as before (drafted `conversation.N` keys included, so drafting twice can't
 * clash); only a case the alignment loop added (`align.<point>`) keeps its key, which the loop refers to.
 */
export function savedCaseBody(item: EditableCase, index: number): AgentEvalCase {
  return {
    case_key: isAlignmentCase(item) ? item.case_key : `quality.${index + 1}`,
    task: item.task.trim(),
    fixture_ref: item.fixture_ref ?? {},
    expected_contract: item.expected_contract,
    scorer_config: item.scorer_config,
    effect_policy: item.effect_policy,
    valid_until: item.valid_until ?? null,
  };
}

/**
 * How many cases differ from the saved suite (added, removed or edited), for the save bar. A blank new case
 * that was never saved is not a change.
 */
export function unsavedCaseChanges(cases: readonly EditableCase[], saved: readonly AgentEvalCase[]): number {
  // What the Owner edits here: the question, its points and the held-out flag. Fields the server fills in on
  // save (defaults, revision ids) are not edits.
  const comparable = (item: AgentEvalCase) => {
    const contract = (item.expected_contract ?? {}) as { points?: Array<Record<string, unknown>>; held_out?: unknown };
    const points = (contract.points ?? []).map((point) => ({ id: point.id, kind: point.kind, text: point.text, state: point.state, action: point.action ?? null }));
    return JSON.stringify({ task: caseTask(item).trim(), points, held_out: contract.held_out === true });
  };
  const edited = cases.filter((item, index) => item.task.trim() || index < saved.length).map(comparable);
  const before = saved.map(comparable);
  let count = 0;
  for (let index = 0; index < Math.max(edited.length, before.length); index += 1) {
    if (edited[index] !== before[index]) count += 1;
  }
  return count;
}

/** A case the alignment loop added for a confirmed point. */
function isAlignmentCase(item: { case_key: string }): boolean {
  return /^align\.[A-Za-z0-9_-]+$/.test(item.case_key);
}

function evidenceTone(state: string): string {
  if (state === "valid" || state === "passed") return "positive";
  if (state === "running" || state === "queued") return "progress";
  if (state === "missing" || state === "stale") return "warning";
  return "danger";
}

export function AgentEvalPanel(props: Parameters<typeof AgentEvalPanelContent>[0]) {
  // An Agent selection owns its editor buffer, in-flight requests and result cache.
  return <AgentEvalPanelContent key={props.agentId} {...props} />;
}

function AgentEvalPanelContent({
  agentId,
  agentName,
  archived,
  refreshToken,
  onAskCoach,
  onSuiteSaved,
  addCaseRequest = 0,
  hideSafetyOnly = false,
  tools,
  privateAgent = false,
  liveVersion,
}: {
  agentId: string;
  agentName?: string;
  archived: boolean;
  refreshToken: string;
  /** Opens a new Chat with @Coach and this request staged; absent when Coach can't edit this Agent. */
  onAskCoach?: (text: string) => void;
  /** The Owner saved the suite; the test card above rereads its cases. */
  onSuiteSaved?: () => void;
  /** Bumped by the test card's "Add cases": bring the editor into view and start a case. */
  addCaseRequest?: number;
  /** The test card above already says "Only the basic safety check"; say it once. */
  hideSafetyOnly?: boolean;
  /** The Agent's own Tools by display name, for required actions. */
  tools?: readonly EvalPointTool[];
  /** Only the Owner uses it: no administrator approves its releases, so the copy never mentions approval. */
  privateAgent?: boolean;
  /** The released version's number, so the page says "released v1" instead of "the released version". */
  liveVersion?: number;
}) {
  const t = useTranslations("agents_marketplace");
  const format = useCiFormat();
  const { activeId: workspaceId } = useSpaceSelection();
  const [manager, setManager] = useState<AgentEvalManager | null>(null);
  const [available, setAvailable] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const submitting = useRef(false);
  const startIntent = useRef<{ signature: string; key: string } | null>(null);
  const [error, setError] = useState<{ message: string; cause?: unknown; retry?: () => void } | null>(null);
  const [suiteName, setSuiteName] = useState("");
  const [goal, setGoal] = useState("");
  const [cases, setCases] = useState<EditableCase[]>([editableCase(1)]);
  const [savedThisVisit, setSavedThisVisit] = useState(false);
  const [builderLoadedFor, setBuilderLoadedFor] = useState<string | null>(null);
  const [inspectedRuns, setInspectedRuns] = useState<Record<string, AgentEvalRun>>({});
  const [inspectingRunId, setInspectingRunId] = useState<string | null>(null);
  const [pickingConversations, setPickingConversations] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  // FEATURE_AGENT_EVAL_ALIGNMENT: suite review and held-out controls show only while it is on. Saving
  // keeps every case's stored held_out value either way.
  const alignment = useAgentEvalAlignmentCapability();
  const [suiteReview, setSuiteReview] = useState<{ revisionId: string; value: SuiteReview } | null>(null);
  const [suiteReviewOpen, setSuiteReviewOpen] = useState(false);
  const [dismissedAdvice, setDismissedAdvice] = useState<Set<string>>(() => new Set());
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  // Unsaved cases open with their question in edit mode, saved ones as text; a toggle flips that per case.
  const [taskToggled, setTaskToggled] = useState<Set<number>>(() => new Set());

  const load = useCallback(async () => {
    try {
      const value = await getAgentEvalManager(agentId);
      setManager(value);
      setAvailable(true);
      setError(null);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        setAvailable(false);
        return;
      }
      setError({ message: t("eval_load_failed"), cause, retry: () => void load() });
    }
  }, [agentId, t]);

  useEffect(() => { void load(); }, [load, refreshToken]);

  // Coach's suite review (R19): advice only, cached per suite revision on the server, silent when unavailable.
  const reviewedSuite = manager?.suites.find((suite) => suite.suite_kind === "owner_quality" && suite.revision);
  const reviewedRevisionId = reviewedSuite?.revision?.suite_revision_id ?? null;
  useEffect(() => {
    if (!alignment || !reviewedSuite || !reviewedRevisionId || archived) return;
    const controller = new AbortController();
    void reviewAgentEvalSuite(agentId, reviewedSuite.suite_id, reviewedRevisionId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setSuiteReview({ revisionId: reviewedRevisionId, value }); })
      .catch(() => undefined);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, alignment, archived, reviewedRevisionId]);

  useEffect(() => {
    setBuilderLoadedFor(null);
    setSuiteName("");
    setGoal("");
    setCases([editableCase(1)]);
  }, [agentId]);

  useEffect(() => {
    if (!manager || builderLoadedFor === agentId) return;
    const ownerSuite = manager.suites.find((suite) => suite.suite_kind === "owner_quality");
    if (ownerSuite) {
      setSuiteName(ownerSuite.name);
      const savedCases = ownerSuite.revision?.cases.map((item, index) => ({
        ...editableCase(index + 1, caseTask(item)),
        ...item,
        task: caseTask(item),
      })) ?? [];
      setCases(savedCases.length ? savedCases : [editableCase(1)]);
    }
    setBuilderLoadedFor(agentId);
  }, [agentId, builderLoadedFor, manager]);

  const activeRun = useMemo(
    () => manager?.runs.find((run) => run.status === "queued" || run.status === "running") ?? null,
    [manager?.runs],
  );

  useEffect(() => {
    if (!activeRun) return;
    const timer = window.setTimeout(() => {
      void getAgentEvalRun(agentId, activeRun.run_id).then((run) => {
        setManager((current) => current ? {
          ...current,
          runs: [run, ...current.runs.filter((item) => item.run_id !== run.run_id)],
        } : current);
        if (run.status !== "queued" && run.status !== "running") void load();
      }).catch((cause) => setError({ message: t("eval_refresh_failed"), cause }));
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [activeRun, agentId, load, t]);

  // The test card's "Add cases" (Evaluate, no cases yet) lands in the editor, as the evidence card's does.
  const focusCaseEditorRef = useRef<(() => void) | null>(null);
  const lastAddRequest = useRef(addCaseRequest);
  useEffect(() => {
    if (lastAddRequest.current === addCaseRequest || !manager) return;
    lastAddRequest.current = addCaseRequest;
    focusCaseEditorRef.current?.();
  }, [addCaseRequest, manager]);

  if (!available) return null;

  const runErrorMessage = (cause: unknown, fallback: "eval_run_failed" | "eval_saved_run_failed" | "eval_save_failed") =>
    t(cause instanceof ApiError && cause.code === "agent_eval_subject_changed" ? "eval_target_changed"
      : cause instanceof ApiError && cause.code === "agent_eval_model_unavailable" ? "eval_model_unavailable" : fallback);

  const startSuite = async (suite: AgentEvalSuite, target: AgentEvalManager["target"]) => {
    if (!workspaceId || !suite.revision) return;
    const body = {
      suite_revision_id: suite.revision.suite_revision_id,
      workspace_id: workspaceId,
      target: target.kind,
      ...(target.revision_id ? { expected_revision_id: target.revision_id } : {}),
      attempts: 1,
    };
    // A lost start response must reuse the exact intent instead of creating another paid run.
    const signature = JSON.stringify([agentId, body]);
    if (startIntent.current?.signature !== signature) startIntent.current = { signature, key: randomUuid() };
    const run = await runAgentEval(agentId, body, startIntent.current.key);
    startIntent.current = null;
    setManager((current) => current ? { ...current, runs: [run, ...current.runs.filter((item) => item.run_id !== run.run_id)] } : current);
  };

  const runSuite = async (suite: AgentEvalSuite) => {
    if (!manager || !workspaceId || !suite.revision || busy || submitting.current) return;
    submitting.current = true;
    setBusy(`run:${suite.suite_id}`);
    try {
      await startSuite(suite, manager.target);
      setError(null);
    } catch (cause) {
      setError({ message: runErrorMessage(cause, "eval_run_failed"), cause });
      if (cause instanceof ApiError && cause.code === "agent_eval_subject_changed") void load();
    } finally {
      submitting.current = false;
      setBusy(null);
    }
  };

  const inspectRun = async (runId: string) => {
    if (inspectingRunId) return;
    setInspectingRunId(runId);
    try {
      const run = await getAgentEvalRun(agentId, runId);
      setInspectedRuns((current) => ({ ...current, [runId]: run }));
    } catch (cause) {
      setError({ message: t("eval_inspection_failed"), cause });
    } finally {
      setInspectingRunId(null);
    }
  };

  const suggest = async () => {
    if (busy) return;
    setBusy("suggest");
    setNotice(null);
    try {
      const value = await suggestAgentEvalCases(agentId, goal);
      setCases((current) => {
        // Suggestions replace the hand-written cases, but never a case the alignment loop added or a held-out one.
        const kept = current.filter((item) => isAlignmentCase(item) || isHeldOut(item));
        return [...kept, ...value.cases.map((item, index) => ({
          ...editableCase(kept.length + index + 1, caseTask(item)),
          ...item,
          task: caseTask(item),
        }))].slice(0, MAX_SUITE_CASES);
      });
      setTaskToggled(new Set());
      setError(null);
    } catch (cause) {
      setError({ message: t("eval_suggest_failed"), cause });
    } finally {
      setBusy(null);
    }
  };

  const addDraftedCases = (drafted: AgentEvalCase[]) => {
    setCases((current) => {
      // A single blank starter case is replaced rather than kept above the drafts.
      const kept = current.length === 1 && !current[0].task.trim() ? [] : current;
      const added = drafted.map((item, index) => ({
        ...editableCase(kept.length + index + 1, caseTask(item)),
        ...item,
        task: caseTask(item),
      }));
      return [...kept, ...added].slice(0, MAX_SUITE_CASES);
    });
    setPickingConversations(false);
    setNotice(t("eval_conversations_drafted", { count: drafted.length }));
  };

  const updateOldDefaults = async (suite: AgentEvalSuite) => {
    if (!suite.revision || busy) return;
    setBusy(`defaults:${suite.suite_id}`);
    try {
      // Saved from the stored revision, so unsaved edits in the builder are not published by this action.
      const value = await saveAgentEvalSuite(agentId, {
        suite_id: suite.suite_id,
        name: suite.name,
        visibility: suite.visibility,
        cases: suite.revision.cases.map(caseWithCurrentDefaults),
      });
      setManager(value);
      // Keep the builder from saving the old settings back later.
      setCases((current) => current.map((item) => ({ ...item, scorer_config: withCurrentScorerDefaults(item.scorer_config) })));
      setNotice(t("eval_old_defaults_updated"));
      setError(null);
    } catch (cause) {
      setError({ message: t("eval_old_defaults_failed"), cause });
    } finally {
      setBusy(null);
    }
  };

  const persistSuite = async (force: boolean) => {
    const existing = manager?.suites.find((suite) => suite.suite_kind === "owner_quality");
    const name = suiteName.trim() || t("eval_owner_suite_default_name");
    if (!force && manager && existing && existing.name === name && unsavedCaseChanges(cases, existing.revision?.cases ?? []) === 0) return manager;
    const value = await saveAgentEvalSuite(agentId, {
      suite_id: existing?.suite_id ?? null,
      name,
      visibility: "owner",
      cases: cases.filter((item) => item.task.trim()).map(savedCaseBody),
    });
    setManager(value);
    setSavedThisVisit(true);
    onSuiteSaved?.();
    return value;
  };

  const saveSuite = async (andRun = false) => {
    if (!manager || busy || submitting.current) return;
    submitting.current = true;
    setBusy(andRun ? "save-and-run" : "save-suite");
    setNotice(null);
    let saved = false;
    const target = manager.target;
    try {
      const value = await persistSuite(!andRun);
      saved = true;
      if (andRun) {
        const suite = value.suites.find((item) => item.suite_kind === "owner_quality");
        if (!suite?.revision) throw new Error("The saved evaluation suite has no revision");
        await startSuite(suite, target);
      }
      setError(null);
    } catch (cause) {
      setError({ message: runErrorMessage(cause, saved && andRun ? "eval_saved_run_failed" : "eval_save_failed"), cause });
      if (cause instanceof ApiError && cause.code === "agent_eval_subject_changed") void load();
    } finally {
      submitting.current = false;
      setBusy(null);
    }
  };

  const changeRegression = async (
    changes: Partial<NonNullable<AgentEvalManager["trigger_policy"]>>,
  ) => {
    if (busy) return;
    // GET no longer creates policy rows automatically, so absent policy is normal, not exceptional: on first enable,
    // merge the change into an all-off baseline and submit it (PUT itself is INSERT-or-UPDATE). Previously
    // `if (!policy) return` swallowed the request; combined with `disabled={!trigger_policy}` below,
    // it prevented owners from ever enabling automatic regression.
    const policy = manager?.trigger_policy ?? {
      enabled: false,
      on_definition_change: false,
      on_dependency_change: false,
      on_model_change: false,
      row_version: 0,
    };
    setBusy("policy");
    try {
      const updated = await updateAgentEvalTriggerPolicy(agentId, {
        ...policy,
        ...changes,
      });
      setManager((current) => current ? { ...current, trigger_policy: updated } : current);
      setError(null);
    } catch (cause) {
      setError({ message: t("eval_policy_failed"), cause });
    } finally {
      setBusy(null);
    }
  };

  const cancel = async (run: AgentEvalRun) => {
    if (busy) return;
    setBusy("cancel");
    try {
      await cancelAgentEvalRun(agentId, run.run_id);
      await load();
    } catch (cause) {
      setError({ message: t("eval_cancel_failed"), cause });
    } finally {
      setBusy(null);
    }
  };

  const toggleHeldOut = async (index: number, on: boolean) => {
    if (!on && isOnlyHeldOut(cases, index)) {
      const ok = await confirm({
        title: t("eval_held_out_off_title"),
        description: t("eval_held_out_off_body"),
        confirmLabel: t("eval_held_out_off_confirm"),
        tone: "primary",
      });
      if (!ok) return;
    }
    setCases((current) => setHeldOut(current, index, on));
  };

  // Saved cases by task, so a renumbered case still finds its saved revision and point texts.
  const ownerSuite = manager?.suites.find((suite) => suite.suite_kind === "owner_quality");
  const unsavedChanges = unsavedCaseChanges(cases, ownerSuite?.revision?.cases ?? []);
  const savedByTask = new Map<string, { caseRevisionId: string; pointTexts: Record<string, string> }>();
  for (const item of ownerSuite?.revision?.cases ?? []) {
    if (!item.case_revision_id) continue;
    savedByTask.set(caseTask(item).trim(), {
      caseRevisionId: item.case_revision_id,
      pointTexts: Object.fromEntries(casePoints(item.expected_contract).map((point) => [point.id, point.text])),
    });
  }

  // "Add cases" from the evidence card: bring the case editor into view and start an empty case.
  const focusCaseEditor = () => {
    const firstEmpty = cases.findIndex((item) => !item.task.trim());
    if (firstEmpty < 0 && cases.length < MAX_SUITE_CASES) setCases((current) => [...current, editableCase(current.length + 1)]);
    window.requestAnimationFrame(() => {
      document.getElementById("agent-eval-builder-title")?.scrollIntoView({ block: "start", behavior: "smooth" });
      const fields = document.querySelectorAll<HTMLTextAreaElement | HTMLInputElement>(".am-ev-cases textarea, .am-ev-cases input[type=text]");
      const target = firstEmpty >= 0 ? fields[firstEmpty] : fields[fields.length - 1];
      target?.focus({ preventScroll: true });
    });
  };

  focusCaseEditorRef.current = focusCaseEditor;

  const statusTone = (state: string): BadgeTone => {
    const tone = evidenceTone(state);
    return tone === "positive" ? "positive" : tone === "progress" ? "accent" : tone === "warning" ? "warning" : "danger";
  };

  return (
    <section className="am-ci am-ci-eval" aria-labelledby="agent-eval-title">
      <div className="am-ci-vhead">
        <div className="am-ci-vh"><h2 id="agent-eval-title">{t("eval_title")}</h2><p>{t("eval_body")}</p></div>
        {manager ? <Badge tone="neutral">{manager.target.kind === "candidate"
          ? t("eval_target_candidate")
          : liveVersion ? t("eval_target_live_version", { version: liveVersion }) : t("eval_target_live")}</Badge> : null}
      </div>
      <CardStatus status={error ? { tone: "error", message: error.message, cause: error.cause, onRetry: error.retry } : notice ? { tone: "success", message: notice } : null} />
      {!manager ? <LoadingSkeleton rows={3} label={t("loading")} /> : (
        <div className="am-ci-split am-ev-layout" data-coach="true">
          <div className="am-ci-col">
            {/* Agent Eval Points E1: one suite card, cases with their points, one primary action. */}
            <article className="am-ci-card am-ev-suite" aria-labelledby="agent-eval-builder-title">
              <div className="am-ev-suite-h">
                <h3 id="agent-eval-builder-title">{t("eval_suite_header", { name: suiteName.trim() || t("eval_owner_suite_default_name"), count: cases.filter((item) => item.task.trim()).length })}</h3>
                <span className="am-ci-sp" />
                <Button size="sm" variant="secondary" disabled={archived || busy !== null || cases.length >= MAX_SUITE_CASES} onClick={() => setPickingConversations(true)}>{t("eval_from_conversations")}</Button>
                <Button size="sm" variant="secondary" aria-expanded={suggestOpen} disabled={archived || busy !== null} onClick={() => setSuggestOpen((value) => !value)}>{t("eval_suggest")}</Button>
                <RowActionsMenu label={t("eval_suite_menu")} actions={[{ id: "rename", label: t("eval_suite_rename"), disabled: archived || busy !== null, onSelect: () => setRenaming((value) => !value) }]} />
              </div>
              <p className="am-ci-note">{t("eval_builder_body")}</p>
              {renaming || !ownerSuite ? (
                <div className="am-ci-fld"><label htmlFor="agent-eval-suite-name">{t("eval_suite_name")}</label><input id="agent-eval-suite-name" className="settings-input" value={suiteName} maxLength={120} disabled={archived || busy !== null} onChange={(event) => setSuiteName(event.target.value)} placeholder={t("eval_owner_suite_default_name")} /></div>
              ) : null}
              {suggestOpen ? (
                <div className="am-ev-suggest">
                  <div className="am-ci-fld"><label htmlFor="agent-eval-goal">{t("eval_goal")}</label><textarea id="agent-eval-goal" className="settings-input" rows={2} value={goal} maxLength={4000} disabled={archived || busy !== null} onChange={(event) => setGoal(event.target.value)} placeholder={t("eval_goal_placeholder")} /></div>
                  <div className="am-ci-acts"><Button size="sm" variant="secondary" disabled={archived || busy !== null} loading={busy === "suggest"} onClick={() => void suggest()}>{t("eval_suggest_run")}</Button></div>
                </div>
              ) : null}
              {alignment && ownerSuite && suiteReview && suiteReview.revisionId === ownerSuite.revision?.suite_revision_id ? (
                <SuiteReviewCard
                  review={suiteReview.value}
                  cases={ownerSuite.revision?.cases ?? []}
                  open={suiteReviewOpen}
                  onToggle={() => setSuiteReviewOpen((value) => !value)}
                  dismissed={dismissedAdvice}
                  onDismiss={(key) => setDismissedAdvice((current) => new Set(current).add(key))}
                  onAskCoach={onAskCoach ? (gap) => onAskCoach(t("eval_review_draft_case_prefill", { name: agentName || t("eval_review_this_agent"), gap })) : undefined}
                  onRemoveCase={archived || busy !== null ? undefined : (key) => {
                    setTaskToggled(new Set());
                    setCases((current) => current.filter((candidate) => candidate.case_key !== key));
                    setNotice(t("eval_suite_review_removed_note"));
                  }}
                />
              ) : null}
              <p className="am-ci-note">{t(privateAgent ? "eval_points_rule_private" : "eval_points_rule")}</p>
              {cases.length === 0 ? <p className="am-ci-note">{t("version_test_no_cases_body")}</p> : null}
              <div className="am-ev-cases">
                {cases.map((item, index) => {
                  const points = casePoints(item.expected_contract);
                  const editingTask = item.case_revision_id ? taskToggled.has(index) : !taskToggled.has(index);
                  return (
                    <EvalCaseCard
                      key={`${item.case_key}:${index}`}
                      number={index + 1}
                      task={item.task}
                      editingTask={editingTask}
                      onEditTask={(editing) => setTaskToggled((current) => {
                        const next = new Set(current);
                        if (editing === Boolean(item.case_revision_id)) next.add(index); else next.delete(index);
                        return next;
                      })}
                      onTaskChange={(task) => setCases((current) => current.map((candidate, candidateIndex) => candidateIndex === index ? { ...candidate, task } : candidate))}
                      heldOut={isHeldOut(item)}
                      onHeldOutChange={(on) => void toggleHeldOut(index, on)}
                      onRemove={() => { setTaskToggled(new Set()); setCases((current) => current.filter((_, candidateIndex) => candidateIndex !== index)); }}
                      disabled={archived || busy !== null}
                      pointCount={points.length}
                    >
                      <EvalCasePoints
                        agentId={agentId}
                        caseNumber={index + 1}
                        task={item.task}
                        points={points}
                        saved={savedByTask.get(item.task.trim()) ?? null}
                        pointStatus={ownerSuite?.point_status}
                        suiteId={ownerSuite?.revision ? ownerSuite.suite_id : null}
                        onCheckOnAnswers={onAskCoach
                          ? (point) => onAskCoach(t("eval_point_check_prefill", { name: agentName || t("eval_review_this_agent"), text: point.text.trim(), task: item.task.trim() }))
                          : undefined}
                        disabled={archived || busy !== null}
                        tools={tools}
                        onChange={(next) => setCases((current) => current.map((candidate, candidateIndex) => candidateIndex === index
                          ? { ...candidate, expected_contract: { ...candidate.expected_contract, points: next } }
                          : candidate))}
                      />
                    </EvalCaseCard>
                  );
                })}
              </div>
              {/* Sticky save bar (Item 5): it says what's unsaved, so collapsing a case never hides that the suite isn't saved. */}
              <div className="am-ci-acts am-ev-save" data-dirty={unsavedChanges > 0 || undefined}>
                {unsavedChanges > 0 ? <span className="am-ev-save-dot" aria-hidden /> : null}
                <span className="am-ci-note" role="status">{unsavedChanges > 0 ? t("eval_save_unsaved", { count: unsavedChanges }) : savedThisVisit ? t("eval_save_saved") : null}</span>
                <span className="am-ci-sp" />
                <Button variant="ghost" disabled={archived || busy !== null || cases.length >= MAX_SUITE_CASES} onClick={() => setCases((current) => [...current, editableCase(current.length + 1)])}>{t("eval_add_case")}</Button>
                <Button variant="secondary" disabled={archived || busy !== null} loading={busy === "save-suite"} onClick={() => void saveSuite()}>{t("eval_save_suite")}</Button>
                <Button variant="primary" disabled={archived || busy !== null || Boolean(activeRun) || !workspaceId || !cases.some((item) => item.task.trim())} loading={busy === "save-and-run"} onClick={() => void saveSuite(true)}>{t(unsavedChanges > 0 || !ownerSuite || suiteName.trim() !== ownerSuite.name ? "eval_save_and_run" : "eval_run_suite")}</Button>
              </div>
            </article>
          </div>

          <div className="am-ci-col am-ci-aside">
            {manager.suites.map((suite) => {
              const summary = suite.evidence.run?.summary;
              const lastRun = suite.evidence.run;
              const lastRunAt = lastRun?.finished_at ?? lastRun?.started_at ?? lastRun?.created_at ?? null;
              const runId = suite.evidence.run?.run_id;
              const runStatus = suite.evidence.run?.status;
              const inspection = runId ? inspectedRuns[runId] : null;
              // Attempts without a verdict, by cause: a time limit or a Tool the evaluation can't run is not an environment fault.
              const unfinished = inspection?.results?.filter((result) => result.status === "infra_failed") ?? [];
              const timeLimited = unfinished.filter((result) => TIME_LIMIT_FAILURE_CODES.has(result.failure_code ?? "")).length;
              const toolNotRun = unfinished.filter((result) => TOOL_NOT_RUN_FAILURE_CODES.has(result.failure_code ?? "")).length;
              // Some answers depended on a Tool evaluation can't run yet (not only Tools the cases require).
              const usedToolNotRun = unfinished.some((result) => result.failure_code === TOOL_NOT_RUN_CODE);
              const infraFailures = unfinished.length - timeLimited - toolNotRun;
              // Attempts without a verdict by cause (evaluation model, the Agent's model, a Tool, a cancellation, or a
              // platform error with its reference), each with its cases and fix, as on Release.
              const attemptCauses = new Map<string, { cases: string[]; tools: string[]; causes: (string | null)[]; references: string[]; roles: string[]; owner: string | null; workspace: string | null }>();
              for (const result of inspection?.results ?? []) {
                const stored = storedAttemptFailure(result.deterministic);
                const reason = attemptFailureReason(result.status, result.failure_code, stored.cause);
                if (!reason) continue;
                const entry = attemptCauses.get(reason) ?? { cases: [], tools: [], causes: [], references: [], roles: [], owner: stored.owner, workspace: stored.workspace };
                const saved = suite.revision?.cases.find((item) => item.case_key === result.case_key);
                const question = (result.task ?? (saved ? caseTask(saved) : "")).trim();
                const name = t("version_quoted", { name: question ? (question.length > 40 ? `${question.slice(0, 39)}…` : question) : result.case_key });
                if (!entry.cases.includes(name)) entry.cases.push(name);
                if (stored.tool && !entry.tools.includes(stored.tool)) entry.tools.push(stored.tool);
                if (stored.reference && entry.references.length < 3 && !entry.references.includes(stored.reference)) entry.references.push(stored.reference);
                if (stored.role) entry.roles.push(stored.role);
                entry.causes.push(stored.cause);
                attemptCauses.set(reason, entry);
              }
              const translate = (key: string, values?: Record<string, string>) => t(key as never, values as never);
              const attemptLines = [...attemptCauses.entries()].map(([reason, entry]) => {
                const detail = attemptReasonDetail(reason, {
                  cases: entry.cases.join(t("version_list_comma")),
                  tools: entry.tools,
                  toolNames: entry.tools.length ? quotedToolNames(entry.tools, t) : undefined,
                  causes: entry.causes,
                  references: entry.references,
                  role: modelRoleLabel(entry.roles[0], translate),
                  owner: entry.owner,
                  workspace: entry.workspace,
                }, translate);
                return [t(gateReasonKey(reason) as never), detail].filter(Boolean).join(t("version_list_separator"));
              });
              const failedCases = inspection?.results?.filter((result) => result.status === "failed").length ?? 0;
              // Name the cases (by their question) and the Tools, so "split the case" or "change the point" has a target.
              const caseNames = (codes: (code: string) => boolean) => [...new Set(unfinished
                .filter((result) => codes(result.failure_code ?? ""))
                .map((result) => {
                  const saved = suite.revision?.cases.find((item) => item.case_key === result.case_key);
                  const question = (result.task ?? (saved ? caseTask(saved) : "")).trim();
                  return t("version_quoted", { name: question ? (question.length > 40 ? `${question.slice(0, 39)}…` : question) : result.case_key });
                }))].join(t("version_list_comma"));
              const notRunTools = [...new Set(unfinished
                .filter((result) => TOOL_NOT_RUN_FAILURE_CODES.has(result.failure_code ?? ""))
                .flatMap((result) => {
                  const stored = result.deterministic as { required_tools_not_run?: unknown; tools_not_run?: unknown } | undefined;
                  const groups = stored?.required_tools_not_run ?? stored?.tools_not_run;
                  return Array.isArray(groups) ? groups.flatMap((group) => typeof group === "string" ? group.split("|") : []) : [];
                }))];
              const titleId = `agent-eval-suite-${suite.suite_id}`;
              return (
                <article key={suite.suite_id} className="am-ci-card" aria-labelledby={titleId}>
                  <CiCardHead
                    titleId={titleId}
                    title={suite.suite_kind === "platform_contract" ? t("eval_platform_suite_name") : suite.name}
                    body={suite.suite_kind === "platform_contract" ? t("eval_platform_suite_body") : t("eval_owner_suite_body")}
                    // A suite's state counts only "Run this suite" runs; "Not run on its own" says so (a passed
                    // "Test this change" before a release isn't counted here).
                    badge={<Badge tone={statusTone(suite.evidence.state)}>{suite.evidence.state === "valid" ? t("eval_execution_passed") : t(suite.evidence.state === "missing" ? "eval_state_missing_own" : `eval_state_${suite.evidence.state}` as never)}</Badge>}
                  />
                  {lastRun ? (
                    <p className="am-ci-note am-ci-run-source">
                      {/* PRD R20: say which evaluation these numbers come from; the release check is a different one. */}
                      {lastRunAt
                        ? t("eval_last_run_source", { target: t(lastRun.target_kind === "candidate" ? "eval_last_run_candidate" : "eval_last_run_live"), date: format.dateTime(lastRunAt) })
                        : t("eval_last_run_source_undated", { target: t(lastRun.target_kind === "candidate" ? "eval_last_run_candidate" : "eval_last_run_live") })}
                    </p>
                  ) : null}
                  {summary ? (
                    <p className="am-ci-meta">
                      <span>{t("eval_cases")} <b>{summary.case_count ?? 0}</b></span>
                      <span>{t("eval_execution_passed_count")} <b>{summary["pass@k_cases"] ?? 0}</b></span>
                      <span>{t("eval_flaky")} <b>{summary.flaky_cases ?? 0}</b></span>
                    </p>
                  ) : null}
                  <AnswerQuality quality={inspection?.quality ?? lastRun?.quality} />
                  {suite.suite_kind === "owner_quality" && suite.revision?.cases_on_old_defaults?.length ? (
                    <div className="am-ci-ist" data-tone="info">
                      <span className="am-ci-ist-text">{t("eval_old_defaults_notice", { count: suite.revision.cases_on_old_defaults.length })}</span>
                      <span className="am-ci-ist-actions">
                        <Button size="sm" variant="secondary" disabled={archived || busy !== null} loading={busy === `defaults:${suite.suite_id}`} onClick={() => void updateOldDefaults(suite)}>{t("eval_old_defaults_update")}</Button>
                      </span>
                    </div>
                  ) : null}
                  {inspection && runStatus !== "passed" ? <p className="am-ci-note" role="status">{attemptLines.length
                    ? attemptLines.join(" ")
                    : infraFailures > 0
                    ? t("eval_infrastructure_failure", { count: infraFailures })
                    : toolNotRun > 0
                    ? `${t(usedToolNotRun ? "eval_tool_not_run" : "eval_required_tool_not_run", { count: toolNotRun })} ${notRunTools.length
                      ? t("version_reason_detail_tool_not_run", {
                        tools: usedToolNotRun ? listedToolNames(notRunTools, t) : quotedToolNames(notRunTools, t),
                        cases: caseNames((code) => TOOL_NOT_RUN_FAILURE_CODES.has(code)),
                      })
                      : t("version_reason_detail_cases", { cases: caseNames((code) => TOOL_NOT_RUN_FAILURE_CODES.has(code)) })}`
                    : timeLimited > 0
                    ? `${t("eval_time_limit_failure", { count: timeLimited })} ${t("version_reason_detail_time_limit", { cases: caseNames((code) => TIME_LIMIT_FAILURE_CODES.has(code)) })}`
                    : failedCases > 0
                      ? t("eval_failed_cases", { count: failedCases })
                      : t("eval_incomplete_result")}</p> : null}
                  <div className="am-ci-acts">
                    {suite.suite_kind !== "owner_quality" ? <Button variant="secondary" disabled={archived || busy !== null || Boolean(activeRun) || !workspaceId || !suite.revision} loading={busy === `run:${suite.suite_id}`} onClick={() => void runSuite(suite)}>{t("eval_run_suite")}</Button> : null}
                    {runId && runStatus !== "queued" && runStatus !== "running" ? (
                      <Button variant="ghost" disabled={inspectingRunId !== null} loading={inspectingRunId === runId} onClick={() => inspection ? setInspectedRuns((current) => { const next = { ...current }; delete next[runId]; return next; }) : void inspectRun(runId)}>{t(inspection ? "eval_hide_results" : "eval_view_results")}</Button>
                    ) : null}
                  </div>
                  {inspection ? <AgentEvalRunResults key={inspection.run_id} agentId={agentId} run={inspection} /> : null}
                </article>
              );
            })}
            {activeRun ? (
              <div className="am-ci-ist" data-tone="info">
                <span className="am-ci-ist-text" role="status">{t("eval_running")}</span>
                <span className="am-ci-ist-actions"><Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => void cancel(activeRun)}>{t("eval_cancel")}</Button></span>
              </div>
            ) : null}
            {(() => {
              // The title says what the evidence shows, not only whether the gate allows a release (§9).
              const evidence = releaseEvidence(manager.release_gate, manager.suites);
              if (evidence.kind === "waiting") {
                return (
                  <div className="am-ci-card" data-evidence="waiting">
                    <CiCardHead icon={<CircleAlert size={16} />} tone="warning"
                      title={t(privateAgent ? "eval_gate_waiting_private" : "eval_gate_waiting")}
                      body={t(manager.release_gate.required ? (privateAgent ? "eval_gate_candidate_body_private" : "eval_gate_candidate_body") : "eval_gate_live_body")} />
                  </div>
                );
              }
              if (evidence.kind === "safety_only") {
                if (hideSafetyOnly) return null;
                return (
                  <div className="am-ci-card" data-evidence="safety_only">
                    <CiCardHead icon={<ShieldCheck size={16} />} tone="neutral" title={t("eval_gate_safety_only")} body={t("eval_gate_safety_only_body")} />
                    <div className="am-ci-acts"><Button size="sm" variant="secondary" disabled={archived} onClick={focusCaseEditor}>{t("eval_gate_add_cases")}</Button></div>
                  </div>
                );
              }
              const suiteLabel = evidence.suite.name.trim() || t("eval_owner_suite_default_name");
              if (evidence.kind === "not_run") {
                return (
                  <div className="am-ci-card" data-evidence="not_run">
                    <CiCardHead icon={<Clock size={16} />} tone="warning" title={t("eval_gate_not_run")} body={t("eval_gate_not_run_body", { suite: suiteLabel, count: evidence.count, target: manager.target.kind === "candidate"
                      ? t("eval_last_run_candidate")
                      : liveVersion ? t("eval_last_run_live_version", { version: liveVersion }) : t("eval_last_run_live") })} />

                  </div>
                );
              }
              return null;
            })()}
            <details className="am-ci-card">
              <summary>{t("eval_auto_regression")}</summary>
              <label className="am-ci-ctl">
                <input type="checkbox" checked={manager.trigger_policy?.enabled ?? false} disabled={archived || busy !== null} onChange={() => void changeRegression({ enabled: !(manager.trigger_policy?.enabled ?? false) })} />
                <span>{t("eval_auto_regression")}</span>
                <small>{t("eval_auto_regression_body")}</small>
              </label>
              {onAskCoach ? <Button size="sm" variant="ghost" disabled={archived || busy !== null} onClick={() => onAskCoach(t("eval_auto_baseline_prefill", { name: agentName || t("eval_review_this_agent") }))}>{t("eval_auto_baseline_action")}</Button> : null}
              <fieldset className="am-ci-scopes">
                <legend>{t("eval_auto_events")}</legend>
                {(["definition", "dependency", "model"] as const).map((kind) => {
                  const key = `on_${kind}_change` as const;
                  return <label key={kind} className="am-ci-ctl" data-disabled={!manager.trigger_policy?.enabled || undefined}><input type="checkbox" checked={manager.trigger_policy?.[key] ?? false} disabled={archived || busy !== null || !manager.trigger_policy?.enabled} onChange={() => void changeRegression({ [key]: !(manager.trigger_policy?.[key] ?? false) })} /><span>{t(`eval_auto_${kind}` as never)}</span></label>;
                })}
              </fieldset>
            </details>
          </div>
        </div>
      )}
      {confirmDialog}
      {pickingConversations ? (
        <AgentEvalConversationPicker
          agentId={agentId}
          limit={MAX_SUITE_CASES - (cases.length === 1 && !cases[0].task.trim() ? 0 : cases.length)}
          onDrafted={addDraftedCases}
          onClose={() => setPickingConversations(false)}
        />
      ) : null}
    </section>
  );
}

/** "Coach reviewed this suite: N suggestions" — coverage gaps and duplicate cases, advice only (R19). */
function SuiteReviewCard({
  review,
  cases,
  open,
  onToggle,
  dismissed,
  onDismiss,
  onAskCoach,
  onRemoveCase,
}: {
  review: SuiteReview;
  cases: AgentEvalCase[];
  open: boolean;
  onToggle: () => void;
  dismissed: Set<string>;
  onDismiss: (key: string) => void;
  onAskCoach?: (gap: string) => void;
  /** Remove a case from the suite being edited; the Owner saves it with the usual suite save. */
  onRemoveCase?: (caseKey: string) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const [comparing, setComparing] = useState<string | null>(null);
  if (review.status !== "reviewed") return null;
  const number = (key: string) => {
    const index = cases.findIndex((item) => item.case_key === key);
    return index >= 0 ? index + 1 : null;
  };
  const gaps = review.gaps.map((gap, index) => ({ key: `gap:${index}:${gap.text}`, gap })).filter((item) => !dismissed.has(item.key));
  const duplicates = review.duplicate_cases
    .map(([first, second]) => ({ key: `dup:${first}:${second}`, keys: [first, second] as const, first: number(first), second: number(second) }))
    .filter((item) => item.first !== null && item.second !== null && !dismissed.has(item.key));
  const count = gaps.length + duplicates.length;
  if (!count) return null;
  if (!open) {
    return (
      <ChatCard intent="info" size="row" icon={Bot} live="off" title={t("eval_suite_review_row", { count })} data-testid="suite-review-row"
        trailing={<ChatCardButton variant="ghost" aria-expanded={false} onClick={onToggle}>{t("eval_suite_review_view")}</ChatCardButton>} />
    );
  }
  return (
    <ChatCard intent="info" icon={Bot} live="off" eyebrow={t("eval_suite_review_title")} meta={t("eval_suite_review_count", { count })} data-testid="suite-review"
      trailing={<ChatCardButton variant="ghost" aria-expanded onClick={onToggle}>{t("eval_suite_review_hide")}</ChatCardButton>}>
      {gaps.map(({ key, gap }, index) => (
        <CheckRow key={key} first={index === 0} excerpt={gap.text} control={() => (
          <span className="am-ci-review-acts">
            {onAskCoach ? <ChatCardButton variant="ghost" onClick={() => onAskCoach(gap.suggested_task || gap.text)}>{t("eval_suite_review_draft_case")}</ChatCardButton> : null}
            <ChatCardButton onClick={() => onDismiss(key)}>{t("eval_suite_review_dismiss")}</ChatCardButton>
          </span>
        )} />
      ))}
      {duplicates.map((item, index) => (
        <div key={item.key}>
          <CheckRow first={index === 0 && !gaps.length}
            excerpt={t("eval_suite_review_duplicate", { first: item.first as number, second: item.second as number })}
            control={() => (
              <span className="am-ci-review-acts">
                <ChatCardButton variant="ghost" aria-expanded={comparing === item.key} onClick={() => setComparing((value) => value === item.key ? null : item.key)}>
                  {t(comparing === item.key ? "eval_suite_review_side_by_side_hide" : "eval_suite_review_side_by_side")}
                </ChatCardButton>
                <ChatCardButton onClick={() => onDismiss(item.key)}>{t("eval_suite_review_dismiss")}</ChatCardButton>
              </span>
            )} />
          {comparing === item.key ? (
            <div className="am-ev-dup" data-testid="suite-review-compare">
              {item.keys.map((key) => {
                const shown = cases.find((candidate) => candidate.case_key === key);
                const caseNumber = number(key) as number;
                return (
                  <div key={key} className="am-ev-dup-col">
                    <small>{t("eval_suite_review_case", { number: caseNumber })}</small>
                    <b>{shown ? caseTask(shown) : key}</b>
                    <ul>{casePoints(shown?.expected_contract).map((point) => <li key={point.id}>{point.text}</li>)}</ul>
                    {onRemoveCase ? (
                      <ChatCardButton variant="ghost" onClick={() => { onRemoveCase(key); onDismiss(item.key); setComparing(null); }}>
                        {t("eval_suite_review_remove_case", { number: caseNumber })}
                      </ChatCardButton>
                    ) : null}
                  </div>
                );
              })}
              <div className="am-ev-dup-acts">
                <ChatCardButton onClick={() => { onDismiss(item.key); setComparing(null); }}>{t("eval_suite_review_keep_both")}</ChatCardButton>
              </div>
            </div>
          ) : null}
        </div>
      ))}
    </ChatCard>
  );
}
