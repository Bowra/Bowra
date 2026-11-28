"use client";

/**
 * The chat page's Owner draft trial (trial-tech-design §7): which session is a trial, the candidate its next
 * message names, the save rule and C5 before each message, the ended state and the answer actions. The page
 * passes the result into `AgentTrialChatContext`; ordinary chats get `null` and are unchanged.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { useDraftSaveConfirm, useTrialFailureText, useTrialSaveCopy } from "@/components/agents/agent-trial-start";
import {
  getAgentVersionWorkbench,
  getAgentWorkingCopy,
  publishAgentWorkingCopy,
  type AgentVersionWorkbench,
} from "@/lib/agents-api";
import { startDraftTrial, trialCandidatePlan, trialEndedRelease, type TrialReference } from "@/lib/agent-trial";
import { saveTrialTurnAsCase } from "@/lib/agent-trial-case";
import { subscribeAgentTrialSend } from "@/lib/agent-trial-notice";
import { subscribeAgentReleased } from "@/lib/agent-release-events";
import { useTrialEditorDirtyFields } from "@/lib/agent-trial-editor-state";
import type { CoachEditorPort } from "@/lib/coach-canvas-target";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import { randomUuid } from "@/lib/random-id";
import type { AgentTrialChatValue, AgentTrialNotice } from "./agent-trial-chat";

/** A trial opened from the Agent page, before (sessionId null) and after its first message. */
export interface TrialHandoff {
  agentId: string;
  label: string;
  candidateId: string;
  revisionRef: string;
  /** The saved draft or the version in review; absent in an older hand-off, read as the draft. */
  candidateKind?: "draft" | "submitted";
}

export interface AgentTrialChatControllerOptions {
  /** FEATURE_AGENT_DRAFT_TRIAL_V1 and reference input are on. */
  enabled: boolean;
  /** A trial hand-off this page consumed (a new trial chat). */
  handoff: TrialHandoff | null;
  activeSessionId: string | null;
  /** The opened session is an `agent_trial` session (session DTO `origin`, `trial_agent_id`). */
  sessionTrial: { sessionId: string; agentId: string | null } | null;
  workspaceId: string | null;
  /** The Canvas editor of this Agent, when it is open. */
  editorPort: (agentId: string) => CoachEditorPort | null;
  /** Open the Agent editor in the Canvas; null when the Canvas can't host it yet. */
  openEditor: ((agentId: string) => void) | null;
  navigate: (path: string) => void;
  /** Changes on "New work": an unsent trial hand-off doesn't follow into the new chat. */
  resetKey: number;
  /**
   * `composer.agent_start_use`: the new chat admits a released Agent hand-off. Without it 开始使用 vN and
   * 用已发布 vN 问同一个问题 would land on 「暂时无法在对话中选中这个智能体」, so they are not offered.
   */
  startUseAvailable: boolean;
}

export interface AgentTrialChatController {
  value: AgentTrialChatValue | null;
  dialogs: ReactNode;
  /** Stable for the whole trial (before and after the first message); null outside a trial. */
  scopeKey: string | null;
  /** "handoff": a new trial opened from the Agent page; "session": a trial session reopened or reloaded. */
  origin: "handoff" | "session" | null;
}

interface AgentFacts {
  agentId: string;
  name: string;
  liveVersion: number;
  hasDraft: boolean;
}

export function useAgentTrialChatController(options: AgentTrialChatControllerOptions): AgentTrialChatController {
  const t = useTranslations("agent_trial");
  const tm = useTranslations("agents_marketplace");
  const { ask, dialog } = useDraftSaveConfirm();
  const saveCopy = useTrialSaveCopy();
  const failureText = useTrialFailureText();
  const { enabled, handoff: offered, activeSessionId, sessionTrial, workspaceId } = options;

  // The hand-off belongs to the new chat; its first message binds it to the session it creates.
  const [handoff, setHandoff] = useState<(TrialHandoff & { sessionId: string | null; instance: string }) | null>(null);
  const [binding, setBinding] = useState(false);
  useEffect(() => {
    if (offered && enabled) {
      setHandoff({ ...offered, sessionId: null, instance: randomUuid() });
      setBinding(false);
    }
  }, [enabled, offered]);
  useEffect(() => {
    if (!binding || !activeSessionId) return;
    setHandoff((current) => (current && current.sessionId === null ? { ...current, sessionId: activeSessionId } : current));
    setBinding(false);
  }, [activeSessionId, binding]);
  // "New work" before the first trial message leaves the trial: the new chat is an ordinary one.
  const resetKey = useRef(options.resetKey);
  useEffect(() => {
    if (resetKey.current === options.resetKey) return;
    resetKey.current = options.resetKey;
    setHandoff((current) => (current && current.sessionId === null ? null : current));
    setBinding(false);
  }, [options.resetKey]);

  const fromHandoff = Boolean(handoff && (handoff.sessionId === activeSessionId || (handoff.sessionId === null && (activeSessionId === null || binding))));
  const fromSession = Boolean(sessionTrial && sessionTrial.sessionId === activeSessionId && sessionTrial.agentId);
  const agentId = !enabled ? null : fromHandoff ? handoff!.agentId : fromSession ? sessionTrial!.agentId : null;
  const sessionId = fromHandoff ? handoff!.sessionId ?? activeSessionId : fromSession ? sessionTrial!.sessionId : null;

  const [facts, setFacts] = useState<AgentFacts | null>(null);
  const [workbench, setWorkbench] = useState<{ agentId: string; value: AgentVersionWorkbench } | null>(null);
  const [reference, setReference] = useState<TrialReference | null>(null);
  // The kind of the candidate the last message of this trial named (keyed by the trial, so another trial's never leaks).
  const [sentKind, setSentKind] = useState<{ key: string; kind: "draft" | "submitted" } | null>(null);
  const [notice, setNotice] = useState<AgentTrialNotice | null>(null);
  const [preparing, setPreparing] = useState(false);

  const loadWorkbench = useCallback(async (id: string) => {
    const value = await getAgentVersionWorkbench(id);
    setWorkbench({ agentId: id, value });
    return value;
  }, []);

  // A release of this Agent elsewhere on the page (the editor beside the chat) rereads its facts, so the trial
  // ends at once instead of after a reload.
  const [releaseRead, setReleaseRead] = useState(0);
  useEffect(() => {
    if (!agentId) return undefined;
    return subscribeAgentReleased((released) => {
      if (released === agentId) setReleaseRead((value) => value + 1);
    });
  }, [agentId]);

  // A trial (new or reopened) reads the Agent's name, released version and workbench once per Agent (and again
  // after a release on this page).
  useEffect(() => {
    if (!agentId) {
      setFacts(null);
      setWorkbench(null);
      setNotice(null);
      return;
    }
    let cancelled = false;
    void getAgentWorkingCopy(agentId).then((detail) => {
      if (cancelled) return;
      const listing = (detail.working_copy?.listing ?? detail.live.listing) as { display_name?: unknown };
      setFacts({
        agentId,
        name: typeof listing.display_name === "string" && listing.display_name.trim() ? listing.display_name : detail.aggregate.name,
        liveVersion: detail.live.release_sequence,
        hasDraft: Boolean(detail.working_copy),
      });
    }).catch(() => undefined);
    void loadWorkbench(agentId).catch(() => undefined);
    return () => { cancelled = true; };
  }, [agentId, loadWorkbench, releaseRead]);

  // The hand-off names its candidate; a reopened trial starts from the current one (the next message rechecks).
  useEffect(() => {
    if (!agentId) {
      setReference(null);
      return;
    }
    if (fromHandoff && handoff) {
      setReference((current) => current?.agentId === agentId ? current : { agentId, label: handoff.label, candidateId: handoff.candidateId, revisionRef: handoff.revisionRef });
      return;
    }
    const value = workbench?.agentId === agentId ? workbench.value : null;
    if (!value) return;
    const plan = trialCandidatePlan(value);
    if (plan.kind === "reuse") {
      setReference((current) => current?.agentId === agentId ? current : { agentId, label: facts?.name ?? "", candidateId: plan.candidateId, revisionRef: plan.revisionRef });
    }
  }, [agentId, facts?.name, fromHandoff, handoff, workbench]);

  const agentName = facts?.agentId === agentId ? facts.name : fromHandoff ? handoff?.label ?? "" : "";
  const trialKey = fromHandoff && handoff ? `handoff:${handoff.instance}` : sessionId ? `session:${sessionId}` : null;
  const latest = useRef({ agentId, agentName, facts, reference, workspaceId, sessionId, trialKey, options });
  latest.current = { agentId, agentName, facts, reference, workspaceId, sessionId, trialKey, options };

  const currentWorkbench = workbench && workbench.agentId === agentId ? workbench.value : null;
  const endedVersion = trialEndedRelease(currentWorkbench, sessionId);
  const nextPlan = currentWorkbench ? trialCandidatePlan(currentWorkbench) : null;
  // What the band and the chip name from the first screen on (F27): the last message's candidate, else the
  // hand-off's, else the one a reopened trial would reuse; a message that would freeze the saved draft is a draft.
  const candidateKind = (sentKind && sentKind.key === trialKey ? sentKind.kind : null)
    ?? (fromHandoff ? handoff?.candidateKind : null)
    ?? (nextPlan?.kind === "reuse" ? nextPlan.candidateKind : null)
    ?? "draft";
  // F34: a trial whose draft was withdrawn or replaced, with the saved draft now equal to the release, can send
  // nothing; it says so up front instead of after a refused send. The first screen of a new trial never is.
  const unavailable = endedVersion === null && sessionId !== null && nextPlan?.kind === "nothing";

  // TD1 notice and refusals that may mean the draft was released.
  useEffect(() => {
    if (!agentId) return undefined;
    return subscribeAgentTrialSend((signal) => {
      if (signal.kind === "notice" && signal.code === "agent_trial_candidate_under_test") {
        setNotice({ tone: "info", text: t("under_test_notice") });
      } else if (signal.kind === "refused") {
        void loadWorkbench(agentId).catch(() => undefined);
      }
    });
  }, [agentId, loadWorkbench, t]);

  const prepareSend = useCallback(async (): Promise<TrialReference | null> => {
    const { agentId: id, facts: known, workspaceId: space, options: current } = latest.current;
    if (!id) return null;
    if (!space) {
      setNotice({ tone: "error", text: tm("version_workspace_required") });
      return null;
    }
    const live = `v${known?.liveVersion ?? 0}`;
    const port = current.editorPort(id);
    const snapshot = port?.capture?.() ?? null;
    const dirtyFields = snapshot?.dirty ? [...(port?.dirtyFields?.() ?? ["advanced" as const])] : [];
    // What keeps the editor's input from saving (its own 「还不能保存」 reason), so sending names it instead of
    // asking to save first (F8).
    const blockedProblem = port?.saveBlockedProblem?.() ?? null;
    setPreparing(true);
    setNotice(null);
    try {
      let workbenchRead: AgentVersionWorkbench | null = null;
      const result = await startDraftTrial({
        dirtyFields,
        savedDraftEqualsRelease: !known?.hasDraft,
        saveBlocked: Boolean(blockedProblem),
        confirmSave: (fields, { offerSavedOnly, withInstructions }) => ask(saveCopy(fields, { live, action: "send", withInstructions }), { offerSavedOnly }),
        save: async () => Boolean(await port?.save?.()),
        loadWorkbench: async () => (workbenchRead = await loadWorkbench(id)),
        loadWorkingCopy: async () => (await getAgentWorkingCopy(id)).working_copy,
        prepare: (request) => publishAgentWorkingCopy(id, { ...request, release_note: null, workspace_id: space }, randomUuid()),
      }).catch((cause: unknown) => ({ kind: "prepare_failed" as const, cause }));
      if (result.kind === "ready") {
        const label = known?.name || latest.current.reference?.label || latest.current.agentName;
        const next = { agentId: id, label, candidateId: result.candidateId, revisionRef: result.revisionRef };
        setReference(next);
        const key = latest.current.trialKey;
        if (key) setSentKind({ key, kind: result.candidateKind });
        if (!latest.current.sessionId) setBinding(true);
        return next;
      }
      if (result.kind === "cancelled") return null;
      // Released meanwhile: the band ends the trial; nothing else to say.
      if (result.kind === "nothing_to_try" && trialEndedRelease(workbenchRead, latest.current.sessionId) !== null) return null;
      if (result.kind === "save_blocked") setNotice({ tone: "error", text: t("send_save_blocked", { problem: blockedProblem ?? "" }) });
      else if (result.kind === "save_failed") setNotice({ tone: "error", text: t("send_save_failed") });
      else if (result.kind === "rebase_required") setNotice({ tone: "error", text: t("send_rebase", { live }) });
      else if (result.kind === "nothing_to_try") setNotice({ tone: "error", text: t("send_nothing", { live }) });
      else setNotice({ tone: "error", text: t("send_prepare_failed", { reason: failureText(result, live).body }) });
      return null;
    } finally {
      setPreparing(false);
    }
  }, [ask, failureText, loadWorkbench, saveCopy, t, tm]);

  const liveVersion = facts?.agentId === agentId ? facts.liveVersion : null;
  const unsavedFields = useTrialEditorDirtyFields(agentId);
  const openEditor = options.openEditor;
  const navigate = options.navigate;
  const startUse = options.startUseAvailable;
  const value = useMemo<AgentTrialChatValue | null>(() => {
    if (!agentId || !agentName) return null;
    return {
      agentId,
      agentName,
      reference,
      candidateKind,
      liveVersion,
      endedVersion,
      unavailable,
      notice,
      preparing,
      prepareSend,
      backToAgent: () => navigate(`/agents/manage/created/?${new URLSearchParams({ agent_id: agentId, section: "config" })}`),
      openEditor: openEditor ? () => openEditor(agentId) : null,
      startReleased: startUse ? () => handoffAgentToNewChat({ agentId, label: agentName }) : null,
      askReleased: startUse ? (question) => handoffAgentToNewChat({ agentId, label: agentName, text: question }) : null,
      saveAsCase: async (turnId) => { await saveTrialTurnAsCase(agentId, turnId, tm("eval_owner_suite_default_name")); },
      dismissNotice: () => setNotice(null),
      unsavedFields,
    };
  }, [agentId, agentName, candidateKind, endedVersion, liveVersion, navigate, notice, openEditor, preparing, prepareSend, reference, startUse, tm, unavailable, unsavedFields]);

  // One Canvas scope for the whole trial, so the editor opened before the first message survives the session it creates.
  const scopeKey = !value ? null : fromHandoff && handoff ? `agent_trial:${handoff.instance}` : sessionId ? `agent_trial_session:${sessionId}` : null;
  return { value, dialogs: dialog, scopeKey, origin: !value ? null : fromHandoff ? "handoff" : "session" };
}
