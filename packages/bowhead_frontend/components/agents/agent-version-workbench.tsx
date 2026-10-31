"use client";

import { AGENT_PENDING_ANCHOR } from "@/lib/agent-case-proposal-path";

/**
 * Agent version workbench: change proposals and Validate and release › Release check (Claude Design
 * "Agent CI Spec" part 1). This component loads and polls the workbench and hands each view its
 * slice; the views live in agent-version-validation.tsx. The Automation view was removed (design
 * agent-release-beginner-experience §10).
 *
 * The workbench root is an inline-size container (`version-wb`), so the Canvas pane (about 480) and a
 * phone get the narrow layout while the full page keeps the wide one at the same viewport.
 */

import { useCallback, useEffect, useId, useMemo, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { ChevronRight } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { toast } from "@/lib/use-toast";
import {
  acceptAgentProposalSuggestedCases,
  applyAgentChangeProposal,
  dismissAgentChangeProposal,
  dismissAgentProposalSuggestedCases,
  getAgentEvalManager,
  getAgentVersionWorkbench,
  handleAgentMemberFeedback,
  type AgentChangeProposal,
  type AgentMemberFeedback,
  type AgentEvalSuite,
  type AgentSuggestedCaseSpec,
  type AgentVersionWorkbench,
  type AgentWorkingCopyValue,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { draftFieldLabelKey, pendingProposals, type AgentConfigSectionKey } from "@/lib/agent-draft-changes";
import { releaseResultAt } from "@/lib/agent-release-seen";
import { agentCiMerges, isCiProposal, recordCiMerge, settleCiMerges, useCiMerges, waitingCiMerges } from "@/lib/agent-ci-merges";
import { MISSING, previewAtomicGroup, proposalDocument, type FieldDiff, type PointerValue, type ProposalBaseDocument } from "@/lib/agent-proposal-operations";
import { lineDiff } from "@/lib/line-diff";
import { useMergeWindowClock, useWorkbenchRefresh, workbenchShouldPoll } from "@/lib/use-workbench-refresh";
import { CardStatus, useCiFormat, type CardStatusValue } from "./agent-ci-shared";
import { MemberFeedbackView, memberFeedbackKey } from "./agent-member-feedback";
import { AgentVersionValidation, type TestChangeState } from "./agent-version-validation";
import { CoachChangesSection } from "./coach-changes-section";
import { casePoints } from "./agent-eval-points";

type Availability = "checking" | "enabled" | "disabled";

function suggestedCaseKey(proposalId: string, caseId: string): string {
  return `coach.${proposalId.slice(-12)}.${caseId}`.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 120);
}

function caseReviewFields(value: string): Pick<AgentSuggestedCaseSpec, "expected_contract" | "scorer_config"> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const fields = parsed as Record<string, unknown>;
    if (Object.keys(fields).length !== 2) return null;
    for (const key of ["expected_contract", "scorer_config"]) {
      if (!fields[key] || typeof fields[key] !== "object" || Array.isArray(fields[key])) return null;
    }
    return fields as Pick<AgentSuggestedCaseSpec, "expected_contract" | "scorer_config">;
  } catch {
    return null;
  }
}


function toggleAtomicGroup(
  groups: Array<{ id: string; depends_on: string[] }>,
  selected: string[],
  groupId: string,
  checked: boolean,
): string[] {
  const next = new Set(selected);
  if (checked) {
    const add = (id: string) => {
      if (next.has(id)) return;
      next.add(id);
      groups.find((item) => item.id === id)?.depends_on.forEach(add);
    };
    add(groupId);
  } else {
    next.delete(groupId);
    let changed = true;
    while (changed) {
      changed = false;
      for (const group of groups) {
        if (next.has(group.id) && group.depends_on.some((dependency) => !next.has(dependency))) {
          next.delete(group.id);
          changed = true;
        }
      }
    }
  }
  return [...next];
}

/** First line of a proposal's rationale, for undo toasts ("Dismissed Coach proposal “…”"). */
export { AGENT_PENDING_ANCHOR } from "@/lib/agent-case-proposal-path";


function proposalName(proposal: AgentChangeProposal): string {
  return proposal.rationale.split("\n")[0]?.trim().slice(0, 60) ?? "";
}

/** A failed merge names the failed group rule and the groups it found, not only "couldn't be merged". */
export function proposalApplyFailure(cause: unknown, t: ReturnType<typeof useTranslations>): string {
  if (!(cause instanceof ApiError) || cause.code !== "agent_change_groups_invalid") return t("version_failed_merge");
  const details = cause.details ?? {};
  const groupIds = Array.isArray(details.group_ids) ? details.group_ids.map(String) : [];
  if (details.rule === "selected_group_unknown" && groupIds.length) {
    return t("version_failed_merge_groups_unknown", { groups: groupIds.join(", ") });
  }
  return t("version_failed_merge_groups_invalid", { rule: typeof details.rule === "string" ? details.rule : cause.code });
}

export function AgentVersionWorkbenchPanel({
  agentId,
  agentName,
  view,
  workingCopy,
  released = null,
  liveActivatedAt = null,
  onAvailability,
  onRecommendedTask,
  onPendingCount,
  onResultWaiting,
  onChanged,
  onError,
  onOpenConfig,
  onOpenEvaluate,
  onOpenHistory,
  onOpenRelease,
  onAskCoach,
  onImproveWithCoach,
  editorDirty = false,
  onOpenSection,
  coachSlot = null,
  readOnly = false,
  suitesRefreshKey = 0,
  onAddCases,
  onTestState,
}: {
  agentId: string;
  agentName: string;
  /**
   * "canvas": the hosted editor, Coach's changes (rendered into `coachSlot`) and the release check.
   * "evaluate": the "Test this change" card above the evaluation suites (the same card as Release).
   */
  view: "proposals" | "canvas" | "validation" | "evaluate" | "hidden";
  /** Where the canvas places Coach's changes: above the configuration, while this panel sits below it. */
  coachSlot?: HTMLElement | null;
  workingCopy: AgentWorkingCopyValue | null;
  /** The released configuration: while no draft is saved, the draft is this, and proposals compare against it. */
  released?: ProposalBaseDocument | null;
  liveActivatedAt?: string | null;
  onAvailability: (availability: Availability) => void;
  onRecommendedTask?: (task: "config" | "release") => void;
  /** Items awaiting the Owner on Configure: open non-Coach proposals and members' shared thumbs-downs. */
  onPendingCount?: (count: number) => void;
  /** When the result waiting for the Owner on Release was produced; null when there is none. */
  onResultWaiting?: (resultAt: string | null) => void;
  onChanged: () => Promise<void>;
  /** Page-level load failure only; action failures stay in the card that caused them. */
  onError: (message: string, cause?: unknown) => void;
  onOpenConfig?: () => void;
  onOpenEvaluate?: () => void;
  onOpenHistory?: () => void;
  /** Open the Release tab (from a merged CI proposal and the Evaluate tab's release-check result). */
  onOpenRelease?: () => void;
  onAskCoach?: (failed: string[]) => void;
  /** Open Coach with a staged request; `markHandled` runs after any unsaved-change confirmation. */
  onImproveWithCoach?: (text: string, markHandled: () => Promise<void>) => Promise<void>;
  editorDirty?: boolean;
  onOpenSection?: (section: AgentConfigSectionKey) => void;
  /** A read-only view (read-only Canvas): the release check shows its notices without the actions that change it. */
  readOnly?: boolean;
  /** Bumped when the Owner saves the evaluation suite elsewhere on the page, so the test card rereads the cases. */
  suitesRefreshKey?: number;
  /** Evaluate: bring the case editor into view and start a case (the "no cases" card's primary action). */
  onAddCases?: () => void;
  /** The test card's state, so the page can leave out an evidence card that would repeat it. */
  onTestState?: (state: TestChangeState) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const [workbench, setWorkbench] = useState<AgentVersionWorkbench | null>(null);
  // Why there is no workbench yet: still loading, not offered for this Agent (404, e.g. archived), or a failed load.
  // Only `loading` shows the skeleton; the other two settle into a visible state instead of an endless skeleton.
  const [loadState, setLoadState] = useState<"loading" | "ready" | "unavailable" | "failed">("loading");

  const load = useCallback(async (quiet = false) => {
    try {
      const result = await getAgentVersionWorkbench(agentId);
      settleCiMerges(agentId, result);
      setWorkbench(result);
      setLoadState("ready");
      const pending = pendingProposals(result.change_proposals ?? []).filter((proposal) => proposal.status === "open").length
        + (result.member_feedback?.length ?? 0);
      onPendingCount?.(pending);
      onResultWaiting?.(releaseResultAt(result));
      if (result.candidate_identity || result.active_execution_summary) onRecommendedTask?.("release");
      else if (pending) onRecommendedTask?.("config");
      onAvailability("enabled");
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 404) {
        setWorkbench(null);
        setLoadState("unavailable");
        onAvailability("disabled");
        return;
      }
      // A failed background poll keeps the workbench already on screen.
      setLoadState((current) => (current === "ready" ? current : "failed"));
      onAvailability("enabled");
      if (!quiet) onError(t("version_load_failed"), cause);
    }
  }, [agentId, onAvailability, onError, onPendingCount, onRecommendedTask, onResultWaiting, t]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
  }, [view]);

  const reload = useCallback(() => load(true), [load]);
  const pendingDismissals = usePendingDismissals();
  const dismissMemberFeedback = useCallback((item: AgentMemberFeedback) => {
    deferDismissal({
      keys: [memberFeedbackKey(agentId, item.turn_id)],
      text: t("version_member_feedback_dismissed_toast", { name: item.shared_by }),
      undoLabel: t("version_undo"),
      failureText: t("version_member_feedback_failed"),
      commit: async () => {
        await handleAgentMemberFeedback(agentId, item.turn_id, "dismissed");
        await reload();
      },
    });
  }, [agentId, reload, t]);

  // One freshness rule for every workbench view: refetch on tab activation and on returning to the page;
  // poll while a comparison runs or a merged CI proposal waits for its candidate (tech-design §6).
  const allMerges = useCiMerges();
  const merges = useMemo(() => agentCiMerges(allMerges, agentId), [agentId, allMerges]);
  const waitingMerges = useMemo(() => waitingCiMerges(allMerges, agentId, workbench), [agentId, allMerges, workbench]);
  const pollClock = useMergeWindowClock(waitingMerges);
  useWorkbenchRefresh({
    refresh: reload,
    activationKey: view,
    polling: loadState === "ready" && workbenchShouldPoll(workbench, waitingMerges, pollClock),
  });

  if (!workbench || view === "hidden") {
    if ((view !== "validation" && view !== "canvas") || workbench) return null;
    if (loadState === "unavailable") {
      return (
        <div className="am-ci" data-state="unavailable">
          <Notice tone="muted">{t("version_check_unavailable")}</Notice>
        </div>
      );
    }
    if (loadState === "failed") {
      return (
        <div className="am-ci" data-state="failed">
          <Notice tone="error" className="am-ci-load-failed">
            <span>{t("version_load_failed")}</span>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setLoadState("loading");
                void load();
              }}
            >
              {t("version_retry")}
            </Button>
          </Notice>
        </div>
      );
    }
    return <div className="am-ci"><LoadingSkeleton rows={3} label={t("version_loading")} /></div>;
  }

  return (
    <div className="am-ci" data-view={view}>
      {view === "canvas" && coachSlot ? createPortal(
        <CoachChangesSection
          agentId={agentId}
          changes={workbench.draft_changes ?? []}
          workingCopy={workingCopy}
          editorDirty={editorDirty}
          onChanged={async () => {
            await onChanged();
            await reload();
          }}
          onOpenSection={onOpenSection}
        />,
        coachSlot,
      ) : null}
      {view === "proposals" ? (
        <MemberFeedbackView
          agentId={agentId}
          agentName={agentName}
          items={workbench.member_feedback ?? []}
          hidden={pendingDismissals}
          onDismiss={dismissMemberFeedback}
          onImprove={onImproveWithCoach}
          reload={reload}
        />
      ) : null}
      {view === "proposals" ? (
        <ProposalsView agentId={agentId} workbench={workbench} workingCopy={workingCopy} released={released} reload={reload} onChanged={onChanged} onOpenRelease={onOpenRelease} />
      ) : null}
      {view === "validation" || view === "canvas" ? (
        <AgentVersionValidation
          agentId={agentId}
          agentName={agentName}
          workbench={workbench}
          workingCopy={workingCopy}
          editorDirty={editorDirty}
          liveActivatedAt={liveActivatedAt}
          reload={reload}
          onChanged={onChanged}
          onOpenConfig={onOpenConfig}
          onOpenEvaluate={onOpenEvaluate}
          onOpenHistory={onOpenHistory}
          onAskCoach={onAskCoach}
          showChangeSummary={view !== "canvas"}
          ciMerge={merges[0] ?? null}
          readOnly={readOnly}
          suitesRefreshKey={suitesRefreshKey}
        />
      ) : null}
      {view === "evaluate" ? (
        // The same test card as Release, above the evaluation suites; risks and case details stay on Release.
        <AgentVersionValidation
          surface="evaluate"
          agentId={agentId}
          agentName={agentName}
          workbench={workbench}
          workingCopy={workingCopy}
          editorDirty={editorDirty}
          liveActivatedAt={liveActivatedAt}
          reload={reload}
          onChanged={onChanged}
          onOpenConfig={onOpenConfig}
          onOpenRelease={onOpenRelease}
          onOpenHistory={onOpenHistory}
          onAskCoach={onAskCoach}
          readOnly={readOnly}
          suitesRefreshKey={suitesRefreshKey}
          onAddCases={onAddCases}
          onTestState={onTestState}
        />
      ) : null}
    </div>
  );
}

/*
 * Deferred dismissals (D9) live outside the view: the proposals view unmounts when the Owner switches
 * tasks, and a proposal dismissed seconds earlier must stay hidden (and not be dismissed twice) until its
 * Undo toast closes and the commit settles.
 */
const pendingDismissalKeys = new Set<string>();
let pendingSnapshot: ReadonlySet<string> = new Set();
const pendingListeners = new Set<() => void>();

function setPending(keys: string[], pending: boolean) {
  for (const key of keys) {
    if (pending) pendingDismissalKeys.add(key);
    else pendingDismissalKeys.delete(key);
  }
  pendingSnapshot = new Set(pendingDismissalKeys);
  pendingListeners.forEach((listener) => listener());
}

function subscribePending(listener: () => void) {
  pendingListeners.add(listener);
  return () => {
    pendingListeners.delete(listener);
  };
}

function usePendingDismissals(): ReadonlySet<string> {
  return useSyncExternalStore(subscribePending, () => pendingSnapshot, () => pendingSnapshot);
}

function proposalKey(agentId: string, proposalId: string): string {
  return `${agentId}:proposal:${proposalId}`;
}

function caseKey(agentId: string, proposalId: string, caseId: string): string {
  return `${agentId}:case:${proposalId}:${caseId}`;
}

/** Hide now, commit when the 8-second Undo toast closes; a key already pending is not enqueued again. */
export function deferDismissal({ keys, text, undoLabel, failureText, commit }: {
  keys: string[];
  text: string;
  undoLabel: string;
  failureText: string;
  commit: () => Promise<void>;
}): boolean {
  if (!keys.length || keys.some((key) => pendingDismissalKeys.has(key))) return false;
  setPending(keys, true);
  toast({
    kind: "info",
    undo: true,
    text,
    action: { label: undoLabel, onAction: () => setPending(keys, false) },
    onDismiss: () => {
      void commit().then(
        () => setPending(keys, false),
        () => {
          setPending(keys, false);
          toast({ kind: "error", text: failureText });
        },
      );
    },
  });
  return true;
}

/**
 * Review one related update as a whole; optional detail controls retain selective adoption. Dismissing a
 * proposal or suggested cases is committed only after an 8-second Undo toast (D9).
 */
function ProposalsView({
  agentId,
  workbench,
  workingCopy,
  released,
  reload,
  onChanged,
  onOpenRelease,
}: {
  agentId: string;
  workbench: AgentVersionWorkbench;
  workingCopy: AgentWorkingCopyValue | null;
  released: ProposalBaseDocument | null;
  reload: () => Promise<void>;
  onChanged: () => Promise<void>;
  onOpenRelease?: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const locale = useLocale();
  const format = useCiFormat();
  const headingId = useId();
  const listFormatter = useMemo(() => new Intl.ListFormat(locale, { style: "short", type: "conjunction" }), [locale]);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<{ id: string; value: CardStatusValue } | null>(null);
  const [selectedGroups, setSelectedGroups] = useState<Record<string, string[]>>({});
  const [selectedCases, setSelectedCases] = useState<Record<string, string[]>>({});
  const [caseTasks, setCaseTasks] = useState<Record<string, string>>({});
  const [caseReview, setCaseReview] = useState<Record<string, string>>({});
  const [suiteByProposal, setSuiteByProposal] = useState<Record<string, string>>({});
  const [suiteRetry, setSuiteRetry] = useState(0);
  const [suiteRead, setSuiteRead] = useState<{
    agentId: string;
    proposals: AgentChangeProposal[];
    status: "loading" | "ready" | "failed" | "unavailable";
    suites: AgentEvalSuite[];
    cause?: unknown;
  } | null>(null);
  const pendingDismissals = usePendingDismissals();
  const allMerges = useCiMerges();

  // Merged CI proposals stay as "Merged" cards with the next step until CI's candidate appears (§6).
  const proposals = useMemo(() => {
    const listed = pendingProposals(workbench.change_proposals ?? []).filter((proposal) => proposal.agent_id === agentId);
    const merged = waitingCiMerges(allMerges, agentId, workbench)
      .map((merge) => merge.proposal)
      .filter((proposal) => !listed.some((item) => item.proposal_id === proposal.proposal_id));
    return [...listed, ...merged];
  }, [agentId, allMerges, workbench]);
  // With no saved draft, the draft is the released configuration: compare against that.
  const draftDocument = useMemo(() => {
    const base = workingCopy ?? released;
    return base ? proposalDocument(base) : null;
  }, [released, workingCopy]);
  // Unedited selections, tasks and expectations read their defaults from the proposal itself, so the
  // first render is already actionable (no effect-populated gap before the merge button enables).
  const reviewDefault = (item: AgentChangeProposal["suggested_cases"][number]) => (
    item.case_spec ? JSON.stringify({ expected_contract: item.case_spec.expected_contract, scorer_config: item.case_spec.scorer_config }, null, 2) : ""
  );
  const reviewText = (proposal: AgentChangeProposal, id: string) => {
    const item = proposal.suggested_cases.find((entry) => entry.id === id);
    return caseReview[`${proposal.proposal_id}:${id}`] ?? (item ? reviewDefault(item) : "");
  };

  const needsSuites = proposals.some((proposal) => proposal.suggested_cases.some((item) => item.decision_status === "pending"));
  const suiteState = suiteRead?.agentId === agentId && suiteRead.proposals === proposals ? suiteRead.status : "loading";
  const ownerSuites = suiteState === "ready" ? suiteRead!.suites : [];
  useEffect(() => {
    if (!needsSuites) return;
    let cancelled = false;
    setSuiteRead({ agentId, proposals, status: "loading", suites: [] });
    void getAgentEvalManager(agentId).then((manager) => {
      if (cancelled) return;
      const suites = manager.suites.filter((suite) => suite.suite_kind === "owner_quality" && suite.state !== "archived" && suite.revision);
      setSuiteRead({ agentId, proposals, status: "ready", suites });
      setSuiteByProposal((current) => {
        const next = { ...current };
        for (const proposal of proposals) next[proposal.proposal_id] ??= suites[0]?.revision?.suite_revision_id ?? "";
        return next;
      });
    }).catch((cause: unknown) => {
      if (!cancelled) setSuiteRead({ agentId, proposals, status: cause instanceof ApiError && [401, 403, 404].includes(cause.status) ? "unavailable" : "failed", suites: [], cause });
    });
    return () => { cancelled = true; };
  }, [agentId, needsSuites, proposals, suiteRetry]);

  const mutate = async <T,>(id: string, key: string, operation: () => Promise<T>, success: string, failure: string | ((cause: unknown) => string), onSuccess?: (result: T) => void): Promise<boolean> => {
    if (busy) return false;
    setBusy(key);
    setStatus(null);
    try {
      const result = await operation();
      onSuccess?.(result);
      setStatus({ id, value: { tone: "success", message: success } });
      await onChanged();
      await reload();
      return true;
    } catch (cause) {
      setStatus({ id, value: { tone: "error", message: cause instanceof ApiError && cause.code === "agent_not_active" ? t("version_agent_archived") : typeof failure === "function" ? failure(cause) : failure, cause } });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const savedDraftVersion = workingCopy?.row_version ?? workbench.next_draft_identity?.row_version ?? null;
  // No saved draft yet: the server opens it from the released version and merges in one request.
  const mergeBase: number | { liveReleaseId: string } | null = savedDraftVersion
    ?? (workbench.live_identity?.release_id ? { liveReleaseId: workbench.live_identity.release_id } : null);
  const visibleProposals = proposals.filter((proposal) => !pendingDismissals.has(proposalKey(agentId, proposal.proposal_id)));
  const firstActionable = visibleProposals.find((proposal) =>
    (proposal.status === "open" && !(proposal.base.suite_revision_id || proposal.base.initial_suite_name))
    || proposal.suggested_cases.some((item) => item.decision_status === "pending" && !pendingDismissals.has(caseKey(agentId, proposal.proposal_id, item.id))),
  );

  const dismissProposal = (proposal: AgentChangeProposal) => {
    const name = proposalName(proposal);
    deferDismissal({
      keys: [proposalKey(agentId, proposal.proposal_id)],
      text: name ? t("version_proposal_dismissed_toast", { name }) : t("version_proposal_dismissed_toast_unnamed"),
      undoLabel: t("version_undo"),
      failureText: t("version_failed_dismiss"),
      commit: async () => {
        await dismissAgentChangeProposal(agentId, proposal);
        await onChanged();
        await reload();
      },
    });
  };

  const dismissCases = (proposal: AgentChangeProposal, ids: string[]) => {
    deferDismissal({
      keys: ids.map((id) => caseKey(agentId, proposal.proposal_id, id)),
      text: t("version_suggested_cases_dismissed_toast", { count: ids.length }),
      undoLabel: t("version_undo"),
      failureText: t("version_failed_dismiss_cases"),
      commit: async () => {
        await dismissAgentProposalSuggestedCases(agentId, proposal, ids);
        await onChanged();
        await reload();
      },
    });
  };

  if (!visibleProposals.length) return null;
  return (
    <section id={AGENT_PENDING_ANCHOR} className="am-ci-proposals" aria-labelledby={headingId}>
      <div className="am-ci-vh">
        <h2 id={headingId}>{t("version_proposals_title")}</h2>
        <p>{t("version_proposals_body")}</p>
      </div>
      {visibleProposals.map((proposal) => {
        const selected = selectedGroups[proposal.proposal_id] ?? proposal.atomic_groups.map((group) => group.id);
        const pendingCases = proposal.suggested_cases.filter((item) => item.decision_status === "pending" && !pendingDismissals.has(caseKey(agentId, proposal.proposal_id, item.id)));
        if (pendingCases.length && suiteState === "unavailable") return (
          <Notice key={proposal.proposal_id} tone="muted">
            <span>{t("version_suggested_case_suites_unavailable")}</span>
            <Button variant="secondary" size="sm" onClick={() => void reload()}>{t("version_retry")}</Button>
          </Notice>
        );
        const caseSelection = (selectedCases[proposal.proposal_id] ?? pendingCases.map((item) => item.id)).filter((id) => pendingCases.some((item) => item.id === id));
        const allGroupsSelected = selected.length === proposal.atomic_groups.length;
        const allCasesSelected = caseSelection.length === pendingCases.length;
        const selectedEdits = pendingCases.filter((item) => caseSelection.includes(item.id) && item.edit).length;
        const hasEdits = pendingCases.some((item) => item.edit);
        const changedFields = [...new Set(proposal.atomic_groups.filter((group) => selected.includes(group.id))
          .flatMap((group) => group.operation_indexes.map((index) => t(draftFieldLabelKey(proposal.operations[index]?.path ?? "") as never))))];
        const caseOnly = Boolean(proposal.base.suite_revision_id || proposal.base.initial_suite_name);
        const initialSuite = Boolean(proposal.base.initial_suite_name && !proposal.case_target_suite_revision_id);
        const targetSuiteRevision = proposal.case_target_suite_revision_id ?? proposal.base.suite_revision_id ?? suiteByProposal[proposal.proposal_id] ?? "";
        const selectedSuite = ownerSuites.find((suite) => suite.revision?.suite_revision_id === targetSuiteRevision);
        const staleSuite = suiteState === "ready" && (initialSuite ? ownerSuites.length > 0 : caseOnly && !selectedSuite);
        const invalidCaseReview = caseSelection.some((id) => {
          const item = proposal.suggested_cases.find((entry) => entry.id === id)!;
          return Boolean(item.case_spec && !caseReviewFields(reviewText(proposal, id)));
        });
        const visibleCases = proposal.suggested_cases.filter((item) => !pendingDismissals.has(caseKey(agentId, proposal.proposal_id, item.id)));
        const titleId = `${headingId}-${proposal.proposal_id}`;
        return (
          <article key={proposal.proposal_id} className="am-ci-card" aria-labelledby={titleId}>
            <div className="am-ci-irow">
              <h3 id={titleId} className="am-ci-card-title">{caseOnly ? t("version_suggested_cases", { count: pendingCases.length || visibleCases.length }) : t(`version_proposal_source_${proposal.source_type}` as never)}</h3>
              <Badge tone={proposal.status === "open" ? "warning" : proposal.status === "accepted" ? "positive" : "neutral"}>{t(`version_proposal_status_${proposal.status}` as never)}</Badge>
              <span className="am-ci-sp" />
              <time className="am-ci-note" dateTime={proposal.created_at}>{format.dateTime(proposal.created_at)}</time>
            </div>
            {proposal.origin ? <ProposalOrigin origin={proposal.origin} /> : null}
            <p className="am-ci-body">{proposal.rationale || t("version_proposal_no_rationale")}</p>
            {proposal.status === "accepted" && isCiProposal(proposal) ? (
              <div className="am-ci-ist" data-tone="info" role="status">
                <span className="am-ci-ist-text">{t("version_proposal_merged_next")}</span>
                {onOpenRelease ? <span className="am-ci-ist-actions"><Button size="sm" variant="ghost" onClick={onOpenRelease}>{t("version_proposal_open_release")}</Button></span> : null}
              </div>
            ) : null}
            {proposal.status === "open" && !caseOnly ? (
              <>
                {changedFields.length ? <p className="am-ci-note">{t("version_proposal_overview", { fields: listFormatter.format(changedFields) })}</p> : null}
                <details className="am-ci-details am-ci-review">
                  <summary><ChevronRight size={14} aria-hidden />{t("version_proposal_review_details")}</summary>
                  <fieldset className="am-ci-scopes">
                    <legend>{t("version_proposal_groups")}</legend>
                    {proposal.atomic_groups.map((group) => {
                      const labels = [...new Set(group.operation_indexes.map((index) => t(draftFieldLabelKey(proposal.operations[index]?.path ?? "") as never)))];
                      return (
                        <div key={group.id} className="am-ci-group">
                          <label className="am-ci-ctl">
                            <input
                              type="checkbox"
                              checked={selected.includes(group.id)}
                              onChange={(event) => setSelectedGroups((current) => ({
                                ...current,
                                [proposal.proposal_id]: toggleAtomicGroup(proposal.atomic_groups, selected, group.id, event.target.checked),
                              }))}
                            />
                            <span>{listFormatter.format(labels)}</span>
                            <small>
                              {isCiProposal(proposal) ? <><code>{group.id}</code>{t("version_list_separator")}</> : null}
                              {t("version_proposal_change_count", { count: group.operation_indexes.length })}
                            </small>
                          </label>
                          <ProposalGroupDiff document={draftDocument} proposal={proposal} groupId={group.id} />
                        </div>
                      );
                    })}
                  </fieldset>
                </details>
                {!selected.length ? <p className="am-ci-note" role="status">{t("version_proposal_select_one")}</p> : null}
                {mergeBase === null ? <p className="am-ci-note" role="status">{t("version_proposal_no_base")}</p> : null}
                <div className="am-ci-acts">
                  <Button
                    variant={proposal === firstActionable ? "primary" : "secondary"}
                    disabled={busy !== null || !selected.length || mergeBase === null}
                    loading={busy === `proposal:${proposal.proposal_id}`}
                    onClick={() => void mutate(
                      proposal.proposal_id,
                      `proposal:${proposal.proposal_id}`,
                      () => applyAgentChangeProposal(agentId, proposal, selected, mergeBase!),
                      t("version_proposal_applied", { count: selected.reduce((total, id) => total + (proposal.atomic_groups.find((group) => group.id === id)?.operation_indexes.length ?? 0), 0) }),
                      (cause) => proposalApplyFailure(cause, t),
                      (result) => recordCiMerge(agentId, proposal, result.status, workbench),
                    )}
                  >{t(allGroupsSelected ? "version_proposal_apply_all" : "version_proposal_apply")}</Button>
                  <Button variant="ghost" disabled={busy !== null} onClick={() => dismissProposal(proposal)}>{t("version_proposal_dismiss")}</Button>
                </div>
              </>
            ) : null}
            {visibleCases.length ? (
              <section className="am-ci-cases" aria-label={t("version_suggested_cases", { count: visibleCases.length })}>
                {!caseOnly ? <h4 className="am-ci-card-title">{t("version_suggested_cases", { count: visibleCases.length })}</h4> : null}
                <p className="am-ci-note">{t(hasEdits ? "version_suggested_case_edits_note" : "version_suggested_cases_note")}</p>
                <ol className="am-ci-case-preview">
                  {visibleCases.map((item) => {
                    const fields = item.case_spec ? caseReviewFields(reviewText(proposal, item.id)) : null;
                    const points = casePoints(fields?.expected_contract);
                    return (
                      <li key={item.id}>
                        {item.edit ? <small className="am-ci-note">{t("version_suggested_case_edit")}</small> : null}
                        <p className="am-ci-body">{caseTasks[`${proposal.proposal_id}:${item.id}`] ?? item.task}</p>
                        {points.length ? <ul>{points.map((point) => <li key={point.id}>{point.text}</li>)}</ul> : null}
                        <small className="am-ci-note">{item.failure_mode}{item.decision_status !== "pending" ? <> · {t(`version_suggested_case_${item.decision_status}` as never)}</> : null}</small>
                      </li>
                    );
                  })}
                </ol>
                <details className="am-ci-details am-ci-review">
                  <summary><ChevronRight size={14} aria-hidden />{t("version_suggested_case_review_details")}</summary>
                  {visibleCases.map((item) => (
                    <div key={item.id} className="am-ci-case">
                      <input
                        type="checkbox"
                        aria-label={t("version_suggested_case_select", { task: caseTasks[`${proposal.proposal_id}:${item.id}`] ?? item.task })}
                        disabled={item.decision_status !== "pending"}
                        checked={caseSelection.includes(item.id)}
                        onChange={(event) => setSelectedCases((current) => ({
                          ...current,
                          [proposal.proposal_id]: event.target.checked ? [...new Set([...caseSelection, item.id])] : caseSelection.filter((id) => id !== item.id),
                        }))}
                      />
                      <div className="am-ci-case-body">
                        {item.edit ? <SuggestedCaseEditDiff item={item} /> : null}
                        <input
                          className="settings-input"
                          value={caseTasks[`${proposal.proposal_id}:${item.id}`] ?? item.task}
                          maxLength={8000}
                          disabled={item.decision_status !== "pending"}
                          aria-label={t("version_suggested_case_task")}
                          onChange={(event) => setCaseTasks((current) => ({ ...current, [`${proposal.proposal_id}:${item.id}`]: event.target.value }))}
                        />
                        {item.case_spec ? (
                          <textarea
                            className="settings-input bh-mono"
                            rows={7}
                            maxLength={65536}
                            disabled={item.decision_status !== "pending"}
                            aria-label={t("version_suggested_case_expectations")}
                            value={reviewText(proposal, item.id)}
                            onChange={(event) => setCaseReview((current) => ({ ...current, [`${proposal.proposal_id}:${item.id}`]: event.target.value }))}
                          />
                        ) : null}
                        <small className="am-ci-note">{item.failure_mode} · {t(`version_suggested_case_${item.decision_status}` as never)}</small>
                      </div>
                    </div>
                  ))}
                </details>
                {pendingCases.length ? (
                  <div className="am-ci-case-actions">
                    {suiteState === "loading" ? <p className="am-ci-note" role="status">{t("version_suggested_case_suites_loading")}</p> : suiteState === "failed" ? (
                      <CardStatus status={{ tone: "error", message: t("version_suggested_case_suites_failed"), cause: suiteRead?.cause, onRetry: () => { setSuiteRead(null); setSuiteRetry((value) => value + 1); } }} />
                    ) : initialSuite ? <p className="am-ci-note">{t("version_suggested_case_initial_suite", { name: proposal.base.initial_suite_name! })}</p> : selectedSuite && (caseOnly || ownerSuites.length === 1) ? (
                      <p className="am-ci-note">{t("version_suggested_case_suite")}: {selectedSuite.name}</p>
                    ) : (
                      <div className="am-ci-fld">
                        <label htmlFor={`${titleId}-suite`}>{t("version_suggested_case_suite")}</label>
                        <select
                          id={`${titleId}-suite`}
                          className="settings-select"
                          value={staleSuite ? "" : targetSuiteRevision}
                          disabled={caseOnly}
                          onChange={(event) => setSuiteByProposal((current) => ({ ...current, [proposal.proposal_id]: event.target.value }))}
                        >
                          <option value="">{t("version_suggested_case_suite_required")}</option>
                          {ownerSuites.map((suite) => <option key={suite.suite_id} value={suite.revision!.suite_revision_id}>{suite.name}</option>)}
                        </select>
                      </div>
                    )}
                    {staleSuite ? <p className="am-ci-note" role="status">{t("version_suggested_case_suite_changed")}</p> : null}
                    {invalidCaseReview ? <p className="am-ci-note" role="status">{t("version_suggested_case_expectations_invalid")}</p> : null}
                    <div className="am-ci-acts">
                      <Button
                        variant={proposal === firstActionable && (caseOnly || proposal.status !== "open") ? "primary" : "secondary"}
                        disabled={busy !== null || suiteState !== "ready" || staleSuite || invalidCaseReview || (!initialSuite && !targetSuiteRevision) || !caseSelection.length || caseSelection.some((id) => !(caseTasks[`${proposal.proposal_id}:${id}`] ?? proposal.suggested_cases.find((item) => item.id === id)!.task).trim())}
                        loading={busy === `cases:${proposal.proposal_id}`}
                        onClick={() => void mutate(
                          proposal.proposal_id,
                          `cases:${proposal.proposal_id}`,
                          () => acceptAgentProposalSuggestedCases(
                            agentId,
                            proposal,
                            initialSuite ? null : targetSuiteRevision,
                            caseSelection.map((id) => {
                              const item = proposal.suggested_cases.find((entry) => entry.id === id)!;
                              return {
                                suggestedCaseId: id,
                                caseKey: item.case_spec?.case_key ?? suggestedCaseKey(proposal.proposal_id, id),
                                task: (caseTasks[`${proposal.proposal_id}:${id}`] ?? item.task).trim(),
                                failureMode: item.failure_mode,
                                ...(item.case_spec ? { caseSpec: { ...item.case_spec, ...caseReviewFields(reviewText(proposal, id))! } } : {}),
                              };
                            }),
                          ),
                          t("version_suggested_cases_accepted"),
                          t("version_failed_accept_cases"),
                        )}
                      >{selectedEdits ? t(selectedEdits === caseSelection.length ? "version_suggested_cases_update" : "version_suggested_cases_save_changes", { count: caseSelection.length }) : allCasesSelected ? t("version_suggested_cases_accept_all", { count: caseSelection.length }) : t("version_suggested_cases_accept")}</Button>
                      <Button variant="ghost" disabled={busy !== null || !caseSelection.length} onClick={() => dismissCases(proposal, caseSelection)}>{t(allCasesSelected ? "version_suggested_cases_dismiss_all" : "version_suggested_cases_dismiss")}</Button>
                    </div>
                  </div>
                ) : null}
              </section>
            ) : null}
            <CardStatus status={status?.id === proposal.proposal_id ? status.value : null} />
          </article>
        );
      })}
    </section>
  );
}

/** S3 origin line of a CI proposal: repository · ref · short commit · the key that sent it. */
function ProposalOrigin({ origin }: { origin: NonNullable<AgentChangeProposal["origin"]> }) {
  const t = useTranslations("agents_marketplace");
  return (
    <p className="am-ci-meta am-ci-origin" aria-label={t("version_proposal_origin_label")}>
      <span>{origin.repository}</span>
      <span>{origin.ref.replace(/^refs\/heads\//, "")}</span>
      <code title={origin.commit_sha}>{origin.commit_sha.slice(0, 7)}</code>
      <span>{origin.key_name ? t("version_proposal_origin_key", { name: origin.key_name }) : t("version_proposal_origin_key_deleted")}</span>
    </p>
  );
}

const DIFF_PREVIEW_LINES = 12;

function fieldLabel(path: string, t: ReturnType<typeof useTranslations>): string {
  const key = draftFieldLabelKey(path);
  return key === "version_proposal_field_other" ? path : t(key as never);
}

function SuggestedCaseEditDiff({ item }: { item: AgentChangeProposal["suggested_cases"][number] }) {
  const t = useTranslations("agents_marketplace");
  if (!item.edit || !item.case_spec) return null;
  const before = item.edit.before;
  return (
    <div className="am-ci-pdiff">
      <p className="am-ci-note">{t("version_suggested_case_edit_diff")}</p>
      {item.edit.changed_fields.map((field) => {
        const original = field === "task" ? before.task : before.case_spec[field];
        const updated = field === "task" ? item.task : item.case_spec![field];
        const renderValue = (value: unknown) => {
          const points = field === "expected_contract" ? casePoints(value as Record<string, unknown>) : [];
          if (points.length) {
            const rest = Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([key]) => key !== "points"));
            return [points.map((point) => point.text).join("\n"), ...(Object.keys(rest).length ? [JSON.stringify(rest, null, 2)] : [])].join("\n");
          }
          return typeof value === "string" ? value : JSON.stringify(value, null, 2);
        };
        return <div key={field} className="am-ci-pdiff-row">
          <span className="am-ci-pdiff-field">{t(`version_suggested_case_field_${field}` as never)}</span>
          <pre className="am-ci-pdiff-text"><span data-kind="del"><span aria-hidden>−</span><del>{renderValue(original)}</del></span><span data-kind="add"><span aria-hidden>+</span><ins>{renderValue(updated)}</ins></span></pre>
        </div>;
      })}
    </div>
  );
}

function plainValue(value: PointerValue, t: ReturnType<typeof useTranslations>): string {
  if (value === MISSING || value === null || value === undefined) return t("coach_change_value_missing");
  return typeof value === "string" ? value : JSON.stringify(value);
}

function TextDiff({ row }: { row: Extract<FieldDiff, { kind: "text" }> }) {
  const t = useTranslations("agents_marketplace");
  const [expanded, setExpanded] = useState(false);
  const lines = useMemo(() => {
    if (row.before === null) return (row.after ?? "").split("\n").map((text) => ({ kind: "add" as const, text }));
    if (row.after === null) return row.before.split("\n").map((text) => ({ kind: "del" as const, text }));
    return lineDiff(row.before, row.after);
  }, [row.after, row.before]);
  const visible = expanded ? lines : lines.slice(0, DIFF_PREVIEW_LINES);
  return (
    <>
      <pre className="am-ci-pdiff-text">
        {visible.map((line, index) => (
          <span key={index} data-kind={line.kind}>
            <span aria-hidden>{line.kind === "del" ? "−" : line.kind === "add" ? "+" : " "}</span>
            {line.kind === "del" ? <del>{line.text}</del> : line.kind === "add" ? <ins>{line.text}</ins> : line.text}
          </span>
        ))}
      </pre>
      {lines.length > DIFF_PREVIEW_LINES ? (
        <Button size="sm" variant="ghost" onClick={() => setExpanded((value) => !value)}>
          {expanded ? t("version_proposal_diff_collapse") : t("version_proposal_diff_expand", { count: lines.length })}
        </Button>
      ) : null}
    </>
  );
}

/** Before → after for each field of one atomic group, computed against the loaded draft (§6 "Diff"). */
function ProposalGroupDiff({ document, proposal, groupId }: {
  document: Record<string, unknown> | null;
  proposal: AgentChangeProposal;
  groupId: string;
}) {
  const t = useTranslations("agents_marketplace");
  const preview = useMemo(() => previewAtomicGroup(document, proposal, groupId), [document, groupId, proposal]);
  if (!preview.rows.length) return null;
  return (
    <div className="am-ci-pdiff">
      {!preview.ok ? <p className="am-ci-note" data-tone="warning">{t(document ? "version_proposal_diff_unavailable" : "version_proposal_diff_no_draft")}</p> : null}
      {preview.rows.map((row) => (
        <div key={row.path} className="am-ci-pdiff-row" data-kind={row.kind}>
          {/* A one-field group with a known field is already named by its checkbox label. */}
          {preview.rows.length > 1 || draftFieldLabelKey(row.path) === "version_proposal_field_other" ? <span className="am-ci-pdiff-field">{fieldLabel(row.path, t)}</span> : null}
          {row.kind === "text" ? <TextDiff row={row} /> : row.kind === "list" ? (
            <span className="am-coach-diff">
              {row.added.map((item) => <ins key={`+${item}`}>{t("coach_change_added", { item })}</ins>)}
              {row.removed.map((item) => <del key={`-${item}`}>{t("coach_change_removed", { item })}</del>)}
              {!row.added.length && !row.removed.length ? <span>{t("version_proposal_diff_list_same")}</span> : null}
            </span>
          ) : (
            <span className="am-coach-diff">
              <del>{plainValue(row.before, t)}</del>
              <ins>{plainValue(row.after, t)}</ins>
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
