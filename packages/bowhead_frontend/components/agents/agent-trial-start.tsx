"use client";

/**
 * Start an Owner draft trial (trial-tech-design §7, canvas ⑫): save per the rule, reuse or freeze the
 * candidate, then open a new trial chat through the existing Agent hand-off. The save confirmation is the shared
 * one AI 优化 will reuse with its own button words (`useDraftSaveConfirm`).
 */

import { useCallback, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import {
  getAgentVersionWorkbench,
  getAgentWorkingCopy,
  publishAgentWorkingCopy,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import {
  startDraftTrial,
  type DraftFieldKey,
  type DraftSaveChoice,
  type TrialStartResult,
} from "@/lib/agent-trial";
import { randomUuid } from "@/lib/random-id";
import { withHttpFailureReason } from "@/lib/user-facing-error";
import {
  agentReleaseBlockedText,
  agentReleaseErrorKey,
  agentReleaseReasonKey,
  agentReleaseReasonSection,
  blockedReasonCodes,
} from "./agent-release-reason";

/** The words of one save confirmation: 试用 says 保存并试用, a trial message 保存并发送, AI 优化 its own. */
export interface DraftSaveConfirmCopy {
  title: string;
  body: string;
  cancel: string;
  savedOnly: string;
  confirm: string;
  /** Start with focus on the confirm button (测试这次修改 with unsaved cases: 保存并测试). */
  focusConfirm?: boolean;
}

/** The shared "save the other fields first?" dialog; `ask` resolves with the Owner's choice. */
export function useDraftSaveConfirm(): {
  ask: (copy: DraftSaveConfirmCopy, options: { offerSavedOnly: boolean }) => Promise<DraftSaveChoice>;
  dialog: ReactNode;
} {
  const bodyId = useId();
  const [request, setRequest] = useState<{ copy: DraftSaveConfirmCopy; offerSavedOnly: boolean; resolve: (choice: DraftSaveChoice) => void } | null>(null);
  const ask = useCallback((copy: DraftSaveConfirmCopy, options: { offerSavedOnly: boolean }) => new Promise<DraftSaveChoice>((resolve) => {
    setRequest({ copy, offerSavedOnly: options.offerSavedOnly, resolve });
  }), []);
  const finish = (choice: DraftSaveChoice) => {
    request?.resolve(choice);
    setRequest(null);
  };
  const dialog = request ? (
    <Dialog
      title={request.copy.title}
      role="alertdialog"
      describedBy={bodyId}
      layer="top"
      onClose={() => finish("cancel")}
      footer={<>
        <Button variant="ghost" onClick={() => finish("cancel")}>{request.copy.cancel}</Button>
        {request.offerSavedOnly ? <Button variant="secondary" onClick={() => finish("saved_only")}>{request.copy.savedOnly}</Button> : null}
        <Button variant="primary" data-dialog-initial-focus={request.copy.focusConfirm ? "" : undefined} onClick={() => finish("save")}>{request.copy.confirm}</Button>
      </>}
    >
      <p id={bodyId}>{request.copy.body}</p>
    </Dialog>
  ) : null;
  return { ask, dialog };
}

/** 「谁能用」和「一句话介绍」: the unsaved fields, each quoted, joined the locale's way. */
export function useDraftFieldList(): (fields: readonly DraftFieldKey[]) => string {
  const t = useTranslations("agent_trial");
  const locale = useLocale();
  const formatter = useMemo(() => new Intl.ListFormat(locale, { style: "long", type: "conjunction" }), [locale]);
  return useCallback((fields) => formatter.format(fields.map((field) => t("field_quoted", { name: t(`field_${field}` as never) }))), [formatter, t]);
}

/**
 * The trial's save confirmation: 「先保存另外 N 项修改？」 (工作说明 is unsaved too and is saved anyway) or 「先保存 N 项修改？」,
 * with 取消 / 只用已保存的草稿 / 保存并试用 (or 保存并发送).
 */
export function useTrialSaveCopy(): (fields: readonly DraftFieldKey[], options: { live: string; action: "try" | "send"; withInstructions?: boolean }) => DraftSaveConfirmCopy {
  const t = useTranslations("agent_trial");
  const list = useDraftFieldList();
  return useCallback((fields, { live, action, withInstructions = true }) => ({
    title: t(withInstructions ? "save_confirm_title" : "save_confirm_title_only", { count: fields.length }),
    body: t(withInstructions ? "save_confirm_body" : "save_confirm_body_only", { fields: list(fields), live }),
    cancel: t("save_confirm_cancel"),
    savedOnly: t("save_confirm_saved_only"),
    confirm: t(action === "try" ? "save_confirm_try" : "save_confirm_send"),
  }), [list, t]);
}

export type TrialFailure =
  | Exclude<TrialStartResult, { kind: "ready" } | { kind: "cancelled" } | { kind: "save_blocked" }>
  /** `problem`: what keeps the draft from saving, as the release bar words it. */
  | { kind: "save_blocked"; problem?: string }
  | { kind: "workspace_required" };

/** Freezing refused because the draft or its candidate changed meanwhile (another tab, a test, a submission). */
const TRIAL_CHANGED_CODES = new Set([
  "agent_release_candidate_version_conflict",
  "agent_release_candidate_state_conflict",
  "agent_release_candidate_not_replaceable",
  "agent_working_copy_version_conflict",
]);

/** Why the draft can't be tried, with the failing check named (never a bare "try again"). */
export function useTrialFailureText(): (failure: TrialFailure, live: string) => { body: string; section: "does" | "uses" | "who" | null; rebase: boolean } {
  const t = useTranslations("agent_trial");
  const tm = useTranslations("agents_marketplace");
  return useCallback((failure, live) => {
    if (failure.kind === "save_failed") return { body: t("cannot_try_save_failed"), section: null, rebase: false };
    if (failure.kind === "save_blocked") {
      return { body: failure.problem ? t("cannot_try_save_blocked", { problem: failure.problem }) : t("cannot_try_save_failed"), section: null, rebase: false };
    }
    if (failure.kind === "nothing_to_try") return { body: t("cannot_try_nothing", { live }), section: null, rebase: false };
    if (failure.kind === "rebase_required") return { body: t("cannot_try_rebase", { live }), section: null, rebase: true };
    if (failure.kind === "workspace_required") return { body: tm("version_workspace_required"), section: null, rebase: false };
    const cause = failure.cause;
    if (cause instanceof ApiError) {
      // Refusals that a retry (which rereads the workbench) or one known step resolves.
      if (cause.code === "agent_eval_policy_version_conflict") return { body: t("cannot_try_policy_changed"), section: null, rebase: false };
      if (TRIAL_CHANGED_CODES.has(cause.code ?? "")) return { body: t("cannot_try_changed"), section: null, rebase: false };
      if (cause.code === "agent_eval_workspace_required") return { body: tm("version_workspace_required"), section: null, rebase: false };
      if (cause.code === "agent_working_copy_base_stale") return { body: t("cannot_try_rebase", { live }), section: null, rebase: true };
      const codes = blockedReasonCodes(cause);
      if (codes.length) {
        const reasons = [...new Set(codes.map((code) => tm(agentReleaseReasonKey(code) as never)))].join(tm("version_list_semicolon"));
        return { body: t("cannot_try_check", { reasons }), section: agentReleaseReasonSection(codes[0]), rebase: false };
      }
      const items = agentReleaseBlockedText(cause, (key, values) => tm(key as never, values as never));
      if (items) return { body: t("cannot_try_check", { reasons: items }), section: "uses", rebase: false };
      const named = agentReleaseErrorKey(cause.code);
      if (named) return { body: t("cannot_try_check", { reasons: tm(named as never) }), section: null, rebase: false };
    }
    return { body: withHttpFailureReason(t("cannot_try_generic"), cause), section: null, rebase: false };
  }, [t, tm]);
}

export interface AgentTrialStartOptions {
  agentId: string;
  agentName: string;
  /** "v2": the released version, named in the confirmation and the failures. */
  live: string;
  workspaceId: string | null;
  dirtyFields: readonly DraftFieldKey[];
  savedDraftEqualsRelease: boolean;
  save: () => Promise<boolean>;
  /**
   * The unsaved edits can't be saved yet (the bar's 「还不能保存：…」): trying then names `problem` instead of offering
   * 保存并试用, with a button (`fixLabel`) to the section that fixes it.
   */
  saveBlocked?: { problem: string; fixLabel: string; onFix: () => void } | null;
  /** Open the Configure section a failing check points at. */
  onOpenSection?: (section: "does" | "uses" | "who") => void;
  /** Go to Configure (a draft behind a newer release is updated there). */
  onOpenConfig?: () => void;
  /** Replaced in tests; opens the new trial chat. */
  openChat?: typeof handoffAgentToNewChat;
}

/** 试用草稿 / 新的试用: one action, one flow. */
export function useAgentTrialStart(options: AgentTrialStartOptions): { start: () => Promise<void>; busy: boolean; dialogs: ReactNode } {
  const t = useTranslations("agent_trial");
  const { ask, dialog } = useDraftSaveConfirm();
  const saveCopy = useTrialSaveCopy();
  const failureText = useTrialFailureText();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<TrialFailure | null>(null);
  const latest = useRef(options);
  latest.current = options;

  const start = useCallback(async () => {
    if (busy) return;
    const current = latest.current;
    if (!current.workspaceId) {
      setFailure({ kind: "workspace_required" });
      return;
    }
    const workspaceId = current.workspaceId;
    setBusy(true);
    setFailure(null);
    try {
      const result = await startDraftTrial({
        dirtyFields: current.dirtyFields,
        savedDraftEqualsRelease: current.savedDraftEqualsRelease,
        saveBlocked: Boolean(current.saveBlocked),
        confirmSave: (fields, { offerSavedOnly, withInstructions }) => ask(saveCopy(fields, { live: current.live, action: "try", withInstructions }), { offerSavedOnly }),
        save: current.save,
        loadWorkbench: () => getAgentVersionWorkbench(current.agentId),
        loadWorkingCopy: async () => (await getAgentWorkingCopy(current.agentId)).working_copy,
        prepare: (request) => publishAgentWorkingCopy(current.agentId, { ...request, release_note: null, workspace_id: workspaceId }, randomUuid()),
      }).catch((cause: unknown): TrialStartResult => ({ kind: "prepare_failed", cause }));
      if (result.kind === "cancelled") return;
      if (result.kind === "save_blocked") {
        setFailure({ kind: "save_blocked", problem: current.saveBlocked?.problem });
        return;
      }
      if (result.kind !== "ready") {
        setFailure(result);
        return;
      }
      (current.openChat ?? handoffAgentToNewChat)({
        agentId: current.agentId,
        label: current.agentName,
        trial: { candidateId: result.candidateId, revisionRef: result.revisionRef, candidateKind: result.candidateKind },
      });
    } finally {
      setBusy(false);
    }
  }, [ask, busy, saveCopy]);

  const shown = failure ? failureText(failure, options.live) : null;
  // A blocked save links to the section the bar names; a failing check to its section; an old base to Configure.
  const blockedFix = failure?.kind === "save_blocked" ? options.saveBlocked ?? null : null;
  const goTo = blockedFix ? { label: blockedFix.fixLabel, run: () => { setFailure(null); blockedFix.onFix(); } }
    : shown?.section ? { label: t(`cannot_try_go_${shown.section}` as never), run: () => { setFailure(null); options.onOpenSection?.(shown.section!); } }
      : shown?.rebase && options.onOpenConfig ? { label: t("cannot_try_go_config"), run: () => { setFailure(null); options.onOpenConfig?.(); } } : null;
  const dialogs = <>
    {dialog}
    {failure && shown ? (
      <Dialog
        title={t("cannot_try_title")}
        role="alertdialog"
        onClose={() => setFailure(null)}
        footer={<>
          <Button variant={goTo ? "ghost" : "primary"} onClick={() => setFailure(null)}>{t(goTo ? "cannot_try_cancel" : "cannot_try_ok")}</Button>
          {goTo ? <Button variant="primary" onClick={goTo.run}>{goTo.label}</Button> : null}
        </>}
      >
        <p style={{ whiteSpace: "pre-line" }}>{shown.body}</p>
      </Dialog>
    ) : null}
  </>;
  return { start, busy, dialogs };
}
