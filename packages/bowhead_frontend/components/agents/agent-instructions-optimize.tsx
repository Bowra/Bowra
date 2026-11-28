"use client";

/**
 * AI 优化 of 工作说明 (slices-2-5-tech-design §3.2, canvas ⑬; FEATURE_AGENT_RELEASE_BAR). One Coach drafting call
 * through the existing proposal endpoints, tagged `ai_optimize:…`; the result shows inline, before and after side
 * by side, and lands in the draft only through 用这个版本 (apply). The full page and the Canvas-hosted editor use
 * this same hook; the page places the button beside the field label and the panel under the field.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  applyAgentChangeProposal,
  createAgentCoachProposal,
  dismissAgentChangeProposal,
  getAgentWorkingCopy,
  type AgentChangeProposal,
} from "@/lib/agents-api";
import { appPath } from "@/lib/base-path";
import { draftSaveStep, confirmedDraftFields, type DraftFieldKey } from "@/lib/agent-trial";
import {
  aiOptimizeSourceRef,
  buildOptimizeGoal,
  isShortOriginal,
  optimizeFailureKind,
  parseOptimizeRationale,
  proposedSystemPrompt,
  systemPromptGroupIds,
  taggedSuggestionLines,
  type OptimizeFacts,
  type OptimizeFailureKind,
} from "@/lib/agent-optimize";
import { randomUuid } from "@/lib/random-id";
import { JUDGE_MODEL_SETTINGS_PATH, useCanConfigureModels } from "@/lib/use-can-configure-models";
import { toast } from "@/lib/use-toast";
import { withHttpFailureReason } from "@/lib/user-facing-error";
import { useDraftFieldList, useDraftSaveConfirm } from "./agent-trial-start";

/** After this long the status says 还在优化… and offers 不等了 (the server allows 240 s). */
export const OPTIMIZE_LONG_WAIT_MS = 30_000;
/** A dropped or proxy-answered request is asked again with the same key this often, until the server's cap. */
export const OPTIMIZE_REPLAY_INTERVAL_MS = 20_000;
const OPTIMIZE_REPLAY_DEADLINE_MS = 260_000;

type Phase = "idle" | "saving" | "waiting" | "detached" | "applying";
type FailureKind = OptimizeFailureKind | "save_failed" | "save_blocked" | "no_prompt_group";

interface Failure {
  kind: FailureKind;
  cause?: unknown;
}

export interface InstructionsOptimizeOptions {
  agentId: string;
  agentName: string;
  /** Offered only to an Owner who can edit this non-archived, versioned Agent. */
  enabled: boolean;
  /** 工作说明 as the editor holds it. */
  text: string;
  /** Unsaved Configure fields: only 工作说明 saves without asking (the trial's rule, without 只用已保存的草稿). */
  dirtyFields: readonly DraftFieldKey[];
  /** A saved draft exists; without one the save creates it (the endpoint needs a draft, S4). */
  hasDraft: boolean;
  save: () => Promise<boolean>;
  /** Why the draft can't be saved yet, with the section that fixes it. */
  saveBlocked: { problem: string; section: "does" | "uses" | "who" } | null;
  onOpenSection?: (section: "does" | "uses" | "who") => void;
  /** The released version ("v2"), named in the save confirmation. */
  live: string;
  /** The configured names the request states as facts, read when the Owner presses the button. */
  facts: () => Promise<OptimizeFacts>;
  /** The newest open AI 优化 proposal from the workbench: a result from before a reload, or one that came late. */
  pending: AgentChangeProposal | null;
  /**
   * Reread the editor and the workbench after the draft or a proposal changed. `afterApply`: the merge rewrote the
   * saved 工作说明 right after a save, so the editor takes the saved draft even if a key was pressed meanwhile.
   */
  onChanged: (afterApply: boolean) => Promise<void>;
}

export interface InstructionsOptimize {
  /** The quiet AI 优化 button and its hint, for the field's label row. */
  button: ReactNode;
  /** 工作说明 is read-only while the page waits for the result. */
  waiting: boolean;
  /** Status, result and failures, for under the field. */
  panel: ReactNode;
  dialogs: ReactNode;
}

export function useInstructionsOptimize(options: InstructionsOptimizeOptions): InstructionsOptimize {
  const t = useTranslations("agent_optimize");
  const locale = useLocale();
  const canConfigureModels = useCanConfigureModels();
  const { ask, dialog } = useDraftSaveConfirm();
  const fieldList = useDraftFieldList();
  const headingId = useId();
  const hintId = useId();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const buttonRef = useRef<HTMLSpanElement | null>(null);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const latest = useRef(options);
  latest.current = options;

  const [phase, setPhase] = useState<Phase>("idle");
  const [long, setLong] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [result, setResult] = useState<{ proposal: AgentChangeProposal; before: string } | null>(null);
  const [handled, setHandled] = useState<ReadonlySet<string>>(() => new Set());
  const [failure, setFailure] = useState<Failure | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [focusResult, setFocusResult] = useState(0);
  const replay = useRef<{ timer: number | null; deadline: number } | null>(null);

  const labels = useMemo(() => ({ kept: t("label_kept"), added: t("label_added"), unconfigured: t("label_unconfigured"), none: t("list_none") }), [t]);
  const listWords = useMemo(() => ({ none: t("list_none"), more: (count: number) => t("list_more", { count }), separator: t("list_separator") }), [t]);

  // A result from before a reload (or one that arrived after 不等了) comes from the workbench.
  const pending = options.pending && !handled.has(options.pending.proposal_id) ? options.pending : null;
  const shown = result && !handled.has(result.proposal.proposal_id)
    ? result
    : pending ? { proposal: pending, before: options.text } : null;

  useEffect(() => () => {
    if (replay.current?.timer) window.clearTimeout(replay.current.timer);
  }, []);
  useEffect(() => {
    if (phase !== "waiting") return undefined;
    setLong(false);
    const timer = window.setTimeout(() => setLong(true), OPTIMIZE_LONG_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);
  useEffect(() => {
    if (!focusResult) return;
    window.requestAnimationFrame(() => headingRef.current?.focus());
  }, [focusResult]);

  const receive = useCallback((proposal: AgentChangeProposal, before: string) => {
    if (!mounted.current) return;
    if (replay.current?.timer) window.clearTimeout(replay.current.timer);
    replay.current = null;
    setInFlight(false);
    setPhase("idle");
    setFailure(null);
    setResult({ proposal, before });
    const parsed = parseOptimizeRationale(proposal.rationale, labels);
    setAnnouncement(parsed ? t("announce_counts", { count: parsed.added.length }) : t("announce"));
    setFocusResult((value) => value + 1);
  }, [labels, t]);

  const fail = useCallback((value: Failure) => {
    if (!mounted.current) return;
    if (replay.current?.timer) window.clearTimeout(replay.current.timer);
    replay.current = null;
    setInFlight(false);
    setPhase("idle");
    setFailure(value);
  }, []);

  /** Ask again with the same key and body: the server replays a finished request and answers "running" otherwise. */
  const scheduleReplay = useCallback((request: () => Promise<AgentChangeProposal>, before: string) => {
    const deadline = replay.current?.deadline ?? Date.now() + OPTIMIZE_REPLAY_DEADLINE_MS;
    if (Date.now() > deadline) {
      replay.current = null;
      setInFlight(false);
      return;
    }
    const timer = window.setTimeout(() => {
      void request().then((proposal) => receive(proposal, before), (cause: unknown) => {
        if (!mounted.current) return;
        const kind = optimizeFailureKind(cause);
        if (kind === "running" || kind === "detached") scheduleReplay(request, before);
        else fail({ kind, cause });
      });
    }, OPTIMIZE_REPLAY_INTERVAL_MS);
    replay.current = { timer, deadline };
  }, [fail, receive]);

  /** Save per the rule; false when the Owner cancelled or the save failed (its reason is on screen). */
  const ensureSaved = useCallback(async (purpose: "optimize" | "apply"): Promise<boolean> => {
    const current = latest.current;
    const step = draftSaveStep(current.dirtyFields);
    if (step === "none" && (current.hasDraft || purpose === "apply")) return true;
    if (current.saveBlocked) {
      setFailure({ kind: "save_blocked" });
      return false;
    }
    if (step === "confirm") {
      const fields = confirmedDraftFields(current.dirtyFields);
      const choice = await ask({
        title: t("save_title", { count: fields.length }),
        body: t(purpose === "apply" ? "save_body_apply" : "save_body", { fields: fieldList(fields), live: current.live }),
        cancel: t("save_cancel"),
        savedOnly: "",
        confirm: t(purpose === "apply" ? "save_confirm_apply" : "save_confirm"),
      }, { offerSavedOnly: false });
      if (choice !== "save") return false;
    }
    setPhase("saving");
    const saved = await current.save();
    if (!mounted.current) return false;
    setPhase("idle");
    if (!saved) setFailure({ kind: "save_failed" });
    return saved;
  }, [ask, fieldList, t]);

  const start = useCallback(async () => {
    const current = latest.current;
    if (inFlight || phase !== "idle" || !current.text.trim()) return;
    setFailure(null);
    if (!await ensureSaved("optimize")) return;
    const before = latest.current.text;
    let goal: string;
    try {
      const facts = await latest.current.facts();
      goal = buildOptimizeGoal((values) => t("goal", { ...values, kept: labels.kept, added: labels.added, unconfigured: labels.unconfigured, none: labels.none }), facts, listWords);
    } catch (cause) {
      setFailure({ kind: "other", cause });
      return;
    }
    if (!mounted.current) return;
    const key = randomUuid();
    const sourceRef = aiOptimizeSourceRef(randomUuid());
    const request = () => createAgentCoachProposal(latest.current.agentId, goal, key, sourceRef);
    setInFlight(true);
    setPhase("waiting");
    setAnnouncement("");
    void request().then((proposal) => receive(proposal, before), (cause: unknown) => {
      if (!mounted.current) return;
      const kind = optimizeFailureKind(cause);
      if (kind === "detached" || kind === "running") {
        // The server may still finish: stop waiting on screen and ask again with the same key.
        setPhase("detached");
        scheduleReplay(request, before);
        return;
      }
      fail({ kind, cause });
    });
  }, [ensureSaved, fail, inFlight, labels, listWords, phase, receive, scheduleReplay, t]);

  const stopWaiting = () => setPhase("detached");

  const finish = useCallback(async (proposal: AgentChangeProposal, afterApply = false) => {
    setHandled((current) => new Set(current).add(proposal.proposal_id));
    setResult(null);
    await latest.current.onChanged(afterApply);
  }, []);

  const apply = useCallback(async () => {
    if (!shown || phase !== "idle") return;
    const proposal = shown.proposal;
    const groups = systemPromptGroupIds(proposal);
    if (!groups.length) {
      setFailure({ kind: "no_prompt_group" });
      return;
    }
    setFailure(null);
    if (!await ensureSaved("apply")) return;
    setPhase("applying");
    try {
      const fresh = await getAgentWorkingCopy(latest.current.agentId);
      const base = fresh.working_copy?.row_version ?? { liveReleaseId: fresh.live.release_id };
      await applyAgentChangeProposal(latest.current.agentId, proposal, groups, base);
      if (!mounted.current) return;
      setPhase("idle");
      setAnnouncement(t("applied"));
      toast({ kind: "success", text: t("applied") });
      await finish(proposal, true);
      // The result panel goes away; land on the rewritten 工作说明 instead of the page body further down.
      window.requestAnimationFrame(() => {
        const field = buttonRef.current?.closest(".am-cfg2-prompt")?.querySelector<HTMLTextAreaElement>("textarea");
        (field ?? buttonRef.current?.querySelector("button"))?.focus();
        field?.scrollIntoView?.({ block: "center" });
      });
    } catch (cause) {
      if (!mounted.current) return;
      setPhase("idle");
      const kind = optimizeFailureKind(cause);
      setFailure({ kind, cause });
      if (kind === "version_conflict") await latest.current.onChanged(false);
      if (kind === "stale") await finish(proposal);
    }
  }, [ensureSaved, finish, phase, shown, t]);

  const dismiss = useCallback(async () => {
    if (!shown || phase !== "idle") return;
    const proposal = shown.proposal;
    setPhase("applying");
    setFailure(null);
    try {
      await dismissAgentChangeProposal(latest.current.agentId, proposal);
      if (!mounted.current) return;
      setPhase("idle");
      setAnnouncement(t("dismissed"));
      await finish(proposal);
      window.requestAnimationFrame(() => buttonRef.current?.querySelector("button")?.focus());
    } catch (cause) {
      if (!mounted.current) return;
      setPhase("idle");
      const kind = optimizeFailureKind(cause);
      if (kind === "stale") await finish(proposal);
      setFailure({ kind, cause });
    }
  }, [finish, phase, shown, t]);

  const retry = () => {
    setFailure(null);
    void start();
  };

  const copyForAdmin = () => {
    const text = t("copy_admin_text", { agent: latest.current.agentName });
    void navigator.clipboard?.writeText(text).then(
      () => toast({ kind: "success", text: t("copied") }),
      () => toast({ kind: "error", text }),
    );
  };

  const empty = !options.text.trim();
  const awaitingDecision = Boolean(shown);
  const buttonDisabled = empty || inFlight || phase !== "idle" || awaitingDecision;
  const hint = empty ? t("empty_hint") : awaitingDecision ? t("decide_first_hint") : inFlight && phase === "idle" ? t("in_flight_hint") : null;
  const button = options.enabled ? (
    <span ref={buttonRef} className="am-opt-trigger">
      <Button
        variant="ghost"
        size="sm"
        disabled={buttonDisabled}
        loading={phase === "waiting" || phase === "saving"}
        aria-describedby={hint ? hintId : undefined}
        onClick={() => void start()}
      >
        {t("button")}
      </Button>
      {hint ? <small id={hintId} className="am-opt-hint">{hint}</small> : null}
    </span>
  ) : null;

  const failureText = (value: Failure): string => {
    switch (value.kind) {
      case "save_blocked": return t("error_save_blocked", { problem: latest.current.saveBlocked?.problem ?? "" });
      case "save_failed": return t("error_save_failed");
      case "no_prompt_group": return t("error_no_prompt_group");
      case "model_unconfigured": return t(canConfigureModels ? "error_model_unconfigured_admin" : "error_model_unconfigured");
      case "unknown": return t("error_unknown");
      case "running": return t("error_running");
      case "no_change": return t("error_no_change");
      case "bad_format": return t("error_bad_format");
      case "guarded": return t("error_guarded");
      case "owner_required": return t("error_owner_required");
      case "version_conflict": return t("error_version_conflict");
      case "merge_conflict": return t("error_merge_conflict");
      case "stale": return t("error_stale");
      default: return withHttpFailureReason(t("error_other"), value.cause);
    }
  };
  const failureActions = (value: Failure): ReactNode => {
    switch (value.kind) {
      case "save_blocked": {
        const section = latest.current.saveBlocked?.section;
        return section && options.onOpenSection ? <Button size="sm" variant="secondary" onClick={() => { setFailure(null); options.onOpenSection?.(section); }}>{t("go_fix")}</Button> : null;
      }
      case "model_unconfigured":
        return canConfigureModels
          ? <a className="settings-button settings-button--secondary settings-button--sm" href={appPath(JUDGE_MODEL_SETTINGS_PATH)}>{t("open_models")}</a>
          : <Button size="sm" variant="secondary" onClick={copyForAdmin}>{t("copy_admin")}</Button>;
      case "unknown":
      case "bad_format":
      case "guarded":
      case "other":
        return <Button size="sm" variant="secondary" onClick={retry}>{t("retry")}</Button>;
      case "no_prompt_group":
        return <>
          <Button size="sm" variant="ghost" onClick={() => void dismiss()}>{t("dismiss")}</Button>
          <Button size="sm" variant="secondary" onClick={() => { const proposal = shown?.proposal; setFailure(null); if (proposal) void dismissThenRetry(proposal); }}>{t("retry")}</Button>
        </>;
      case "merge_conflict":
        return <>
          <Button size="sm" variant="ghost" onClick={() => void dismiss()}>{t("dismiss")}</Button>
          <Button size="sm" variant="secondary" onClick={() => { const proposal = shown?.proposal; setFailure(null); if (proposal) void dismissThenRetry(proposal); }}>{t("reoptimize")}</Button>
        </>;
      default:
        return <Button size="sm" variant="ghost" onClick={() => setFailure(null)}>{t("ok")}</Button>;
    }
  };

  /** 再试一次 / 重新优化 on a result that can't be used: the old proposal is dismissed first, so one stays open. */
  const dismissThenRetry = async (proposal: AgentChangeProposal) => {
    try {
      await dismissAgentChangeProposal(latest.current.agentId, proposal);
    } catch (cause) {
      if (optimizeFailureKind(cause) !== "stale") {
        setFailure({ kind: optimizeFailureKind(cause), cause });
        return;
      }
    }
    setHandled((current) => new Set(current).add(proposal.proposal_id));
    setResult(null);
    await latest.current.onChanged(false);
    void start();
  };

  const listFormat = useMemo(() => new Intl.ListFormat(locale, { style: "short", type: "conjunction" }), [locale]);
  // Only quoted sentences found in the suggestion get a 新增 tag, so the count says how many of them are marked.
  const addedSummary = (added: readonly string[], after: string) => {
    if (!added.length) return t("explain_added_none");
    const tagged = added.filter((item) => item.trim().length >= 2 && after.includes(item.trim())).length;
    if (tagged === added.length) return t("explain_added", { count: added.length });
    return tagged ? t("explain_added_partial", { count: added.length, tagged }) : t("explain_added_untagged", { count: added.length });
  };
  const resultView = (() => {
    if (!shown) return null;
    const after = proposedSystemPrompt(shown.proposal) ?? "";
    const parsed = parseOptimizeRationale(shown.proposal.rationale, labels);
    const lines = taggedSuggestionLines(after, parsed?.added ?? []);
    const short = isShortOriginal(shown.before);
    return (
      <section className="am-opt-result" aria-labelledby={headingId} data-testid="ai-optimize-result">
        <h4 id={headingId} ref={headingRef} tabIndex={-1} className="am-opt-title">{t("result_title")}</h4>
        {parsed ? (
          <div className="am-opt-explain">
            {parsed.kept ? <p>{t("explain_kept", { text: parsed.kept })}</p> : null}
            <p>{addedSummary(parsed.added, after)}</p>
          </div>
        ) : shown.proposal.rationale.trim() ? <p className="am-opt-explain am-opt-rationale">{shown.proposal.rationale}</p> : null}
        {short || parsed?.unconfigured.length ? (
          <ul className="am-opt-cautions" aria-label={t("cautions_label")}>
            {short ? <li>{t("caution_short", { count: [...shown.before.trim()].length })}</li> : null}
            {parsed?.unconfigured.length ? <li>{t("caution_unconfigured", { names: listFormat.format(parsed.unconfigured) })}</li> : null}
          </ul>
        ) : null}
        <div className="am-opt-columns">
          <div className="am-opt-column">
            <h5>{t("column_current")}</h5>
            <div className="am-opt-text">{shown.before.split(/\r?\n/).map((line, index) => <p key={index}>{line || " "}</p>)}</div>
          </div>
          <div className="am-opt-column" data-side="suggestion">
            <h5>{t("column_suggestion")}</h5>
            <div className="am-opt-text">{lines.map((line, index) => (
              <p key={index} data-added={line.added || undefined}>
                {line.added ? <span className="am-opt-tag">{t("tag_added")}</span> : null}
                {line.text || " "}
              </p>
            ))}</div>
          </div>
        </div>
        <p className="am-ci-note">{t("apply_note")}</p>
        <div className="am-ci-acts">
          <Button variant="ghost" disabled={phase !== "idle"} onClick={() => void dismiss()}>{t("dismiss")}</Button>
          <Button variant="secondary" disabled={phase !== "idle"} loading={phase === "applying"} onClick={() => void apply()}>{t("apply")}</Button>
        </div>
      </section>
    );
  })();

  const status = phase === "waiting"
    ? <p className="am-opt-status" role="status">{long ? t("waiting_long") : t("waiting")}{long ? <Button size="sm" variant="ghost" onClick={stopWaiting}>{t("stop_waiting")}</Button> : null}</p>
    : phase === "detached" && inFlight
      ? <p className="am-opt-status" role="status">{t("detached")}</p>
      : null;

  const panel = options.enabled ? (
    <div className="am-opt-panel">
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      {status}
      {failure ? (
        <div className="am-ci-ist am-opt-failure" data-tone={["no_change", "running", "version_conflict", "stale"].includes(failure.kind) ? "info" : "error"} role="alert">
          <span className="am-ci-ist-text">{failureText(failure)}</span>
          <span className="am-ci-ist-actions">{failureActions(failure)}</span>
        </div>
      ) : null}
      {resultView}
    </div>
  ) : null;

  return { button, waiting: phase === "waiting" || phase === "saving" || phase === "applying", panel, dialogs: options.enabled ? dialog : null };
}
