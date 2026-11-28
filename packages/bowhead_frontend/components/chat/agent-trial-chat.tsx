"use client";

/**
 * Chat trial mode (trial-tech-design §7, canvas ⑪/⑫): the violet band, the per-answer draft label and divider,
 * and the two actions under each answer. The page provides the trial through `AgentTrialChatContext`; without
 * it (every ordinary chat) these components render nothing.
 */

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Check, FlaskConical } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import { agentCaseProposalPath } from "@/lib/agent-case-proposal-path";
import { appPath } from "@/lib/base-path";
import { formatTime } from "@/lib/locale";
import { draftSaveStep, parseTrialTime, trialDraftChanged, type DraftFieldKey, type TrialAttribution, type TrialReference } from "@/lib/agent-trial";
import { getAgentEvalManager, type AgentEvalManager } from "@/lib/agents-api";
import { useDraftFieldList } from "@/components/agents/agent-trial-start";
import { TrialCaseError } from "@/lib/agent-trial-case";
import { useTurnTrialAttribution } from "@/lib/turn-trial-attribution";
import { withHttpFailureReason } from "@/lib/user-facing-error";

export interface AgentTrialNotice {
  tone: "info" | "error";
  text: string;
}

export interface AgentTrialChatValue {
  agentId: string;
  agentName: string;
  /** The candidate the next message names; null while a reopened trial reads it. */
  reference: TrialReference | null;
  /** "submitted": in review, the trial runs the submitted version. */
  candidateKind: "draft" | "submitted";
  /** The released version, for 用已发布 vN 问同一个问题. */
  liveVersion: number | null;
  /** The draft this trial used was released as vN: the trial has ended. */
  endedVersion: number | null;
  /**
   * The draft this trial used was withdrawn or replaced and the saved draft now equals the release: nothing can be
   * sent; the band says why and points to a new trial from the Agent page.
   */
  unavailable: boolean;
  notice: AgentTrialNotice | null;
  /** Saving or freezing the draft before a message. */
  preparing: boolean;
  /** The save rule and C5 before each message; resolves the reference to send, or null to keep the question. */
  prepareSend: () => Promise<TrialReference | null>;
  backToAgent: () => void;
  /** Opens the Agent editor in the Canvas; null when the Canvas isn't available. */
  openEditor: (() => void) | null;
  /** 开始使用 vN and 用已发布 vN 问同一个问题; null when the new chat can't take the Agent (`agent_start_use` off). */
  startReleased: (() => void) | null;
  askReleased: ((question: string) => void) | null;
  /** Adds the trial Turn's question to 测试用例. */
  saveAsCase: (turnId: string) => Promise<void>;
  dismissNotice: () => void;
  /** The Canvas editor's unsaved fields: sending saves them first (only 工作说明 without asking). */
  unsavedFields: readonly DraftFieldKey[];
}

export const AgentTrialChatContext = createContext<AgentTrialChatValue | null>(null);

export function useAgentTrialChat(): AgentTrialChatValue | null {
  return useContext(AgentTrialChatContext);
}

function useTrialTime(): (value: string) => string {
  const locale = useLocale();
  return (value) => {
    const date = parseTrialTime(value);
    return date ? formatTime(date, locale) : "";
  };
}

/** The sticky band on top of the trial chat; the ended band once the draft was released. */
export function AgentTrialBand() {
  const trial = useAgentTrialChat();
  return trial ? <TrialBand trial={trial} /> : null;
}

function TrialBand({ trial }: { trial: AgentTrialChatValue }) {
  const t = useTranslations("agent_trial");
  const notice = trial.notice;
  if (trial.endedVersion !== null) {
    const version = `v${trial.endedVersion}`;
    return (
      <div className="bh-trial-band" role="region" aria-label={t("band_region")} data-state="ended">
        <span className="bh-trial-band-text"><span>{t("ended_title", { version })}</span></span>
        {trial.startReleased ? (
          <span className="bh-trial-band-actions">
            <Button size="sm" variant="secondary" onClick={trial.startReleased}>{t("ended_start", { version })}</Button>
          </span>
        ) : null}
      </div>
    );
  }
  if (trial.unavailable) {
    return (
      <div className="bh-trial-band" role="region" aria-label={t("band_region")} data-state="ended">
        <span className="bh-trial-band-text"><span>{t("unavailable_title", { live: `v${trial.liveVersion ?? 0}` })}</span></span>
        <span className="bh-trial-band-actions">
          <Button size="sm" variant="secondary" onClick={trial.backToAgent}>{t("band_back")}</Button>
        </span>
      </div>
    );
  }
  const review = trial.candidateKind === "submitted";
  return (
    <>
      <div className="bh-trial-band" role="region" aria-label={t("band_region")} data-state="open">
        <FlaskConical size={18} aria-hidden className="bh-trial-band-icon" />
        <span className="bh-trial-band-text">
          <b>{t(review ? "band_title_review" : "band_title", { agent: trial.agentName })}</b>
          <span className="bh-trial-band-meta">{t(review ? "band_meta_review" : "band_meta")}</span>
        </span>
        <span className="bh-trial-band-actions">
          {trial.openEditor ? <Button size="sm" variant="ghost" onClick={trial.openEditor}>{t("band_edit")}</Button> : null}
          <Button size="sm" variant="secondary" onClick={trial.backToAgent}>{t("band_back")}</Button>
        </span>
      </div>
      {notice ? (
        <p className="bh-trial-notice" data-tone={notice.tone} role={notice.tone === "error" ? "alert" : "status"}>
          <span>{notice.text}</span>
          <button type="button" className="bh-trial-notice-close" onClick={trial.dismissNotice}>{t("notice_dismiss")}</button>
        </p>
      ) : null}
    </>
  );
}

/** 「草稿 · 14:32 保存」 / 「提交的版本 · 17:47」 above a trial answer. */
export function TrialAnswerLabel(props: { turnId: string | null | undefined; settled: boolean }) {
  // Ordinary chats render nothing and read nothing.
  return useAgentTrialChat() && props.turnId ? <TrialLabel turnId={props.turnId} settled={props.settled} /> : null;
}

function TrialLabel({ turnId, settled }: { turnId: string; settled: boolean }) {
  const t = useTranslations("agent_trial");
  const time = useTrialTime();
  const attribution = useTurnTrialAttribution(turnId, true, settled);
  if (!attribution) return null;
  return <TrialDraftChip attribution={attribution} label={(value) => t(attribution.candidateKind === "submitted" ? "answer_label_submitted" : "answer_label_draft", { time: time(value) })} />;
}

function TrialDraftChip({ attribution, label }: { attribution: TrialAttribution; label: (value: string) => string }) {
  return <span className="bh-trial-chip" data-kind={attribution.candidateKind}>{label(attribution.frozenAt)}</span>;
}

/** 「草稿已更新 · 14:40 保存 · 下面的回答用新草稿」 between two answers from different drafts (C5). */
export function TrialDraftDivider(props: { previousTurnId: string | null | undefined; turnId: string | null | undefined; settled: boolean }) {
  return useAgentTrialChat() && props.previousTurnId && props.turnId
    ? <TrialDivider previousTurnId={props.previousTurnId} turnId={props.turnId} settled={props.settled} />
    : null;
}

function TrialDivider({ previousTurnId, turnId, settled }: { previousTurnId: string; turnId: string; settled: boolean }) {
  const t = useTranslations("agent_trial");
  const time = useTrialTime();
  const previous = useTurnTrialAttribution(previousTurnId, true, true);
  const current = useTurnTrialAttribution(turnId, true, settled);
  if (!trialDraftChanged(previous, current) || !current) return null;
  return <p className="bh-trial-divider" role="status">{t("divider", { time: time(current.frozenAt) })}</p>;
}

/** 存为用例 and 用已发布 vN 问同一个问题 under a trial answer; both accessible names include the question. */
export function TrialAnswerActions(props: { turnId: string | null | undefined; question: string }) {
  const trial = useAgentTrialChat();
  return trial && props.turnId ? <TrialActions trial={trial} turnId={props.turnId} question={props.question} /> : null;
}

function TrialActions({ trial, turnId, question }: { trial: AgentTrialChatValue; turnId: string; question: string }) {
  const t = useTranslations("agent_trial");
  const [state, setState] = useState<"idle" | "saving" | "saved" | { error: string }>("idle");
  const text = question.trim();
  const short = text.length > 80 ? `${text.slice(0, 79)}…` : text;
  const version = trial.liveVersion !== null ? `v${trial.liveVersion}` : null;
  const save = async () => {
    setState("saving");
    try {
      await trial.saveAsCase(turnId);
      setState("saved");
    } catch (cause) {
      setState({ error: caseFailureText(cause, t) });
    }
  };
  return (
    <>
      <div className="bh-trial-acts" role="group" aria-label={t("actions_group")}>
        {state !== "saved" ? (
          <Button size="sm" variant="ghost" loading={state === "saving"} disabled={state === "saving"} aria-label={t("action_save_case_label", { question: short })} onClick={() => void save()}>
            {t("action_save_case")}
          </Button>
        ) : null}
        {version && text && trial.askReleased ? (
          <Button size="sm" variant="ghost" aria-label={t("action_ask_released_label", { version, question: short })} onClick={() => trial.askReleased?.(text)}>
            {t("action_ask_released", { version })}
          </Button>
        ) : null}
      </div>
      {state === "saved" ? (
        <p className="bh-trial-saved" role="status">
          <Check size={14} aria-hidden />
          {t("action_save_case_done")}{t("list_separator")}
          <a href={appPath(agentCaseProposalPath(trial.agentId, false))}>{t("action_save_case_points")}</a>
        </p>
      ) : typeof state === "object" ? <p className="bh-trial-saved" data-tone="error" role="alert">{state.error}</p> : null}
    </>
  );
}

function caseFailureText(cause: unknown, t: ReturnType<typeof useTranslations>): string {
  if (cause instanceof TrialCaseError) return t(cause.code === "agent_trial_suite_full" ? "action_save_case_full" : "action_save_case_empty");
  if (cause instanceof ApiError && cause.status === 404) return t("action_save_case_unavailable");
  return withHttpFailureReason(t("action_save_case_failed"), cause);
}

/**
 * The line above the composer while the Canvas editor has unsaved edits (canvas ⑪): only 工作说明 is saved without
 * asking; other fields are named and sending asks first. Null outside a trial and when everything is saved.
 */
export function useTrialSendHint(): string | null {
  const trial = useAgentTrialChat();
  const t = useTranslations("agent_trial");
  const list = useDraftFieldList();
  const fields = trial?.unsavedFields ?? [];
  if (!trial || !fields.length || trial.endedVersion !== null || trial.unavailable) return null;
  return draftSaveStep(fields) === "silent" ? t("send_hint_instructions") : t("send_hint_fields", { fields: list(fields) });
}

/** 「这是试用，但确认后会真的执行。」 on a write-Tool confirmation in a trial (canvas ⑫). */
export function TrialConfirmationLine() {
  return useAgentTrialChat() ? <TrialConfirmationText /> : null;
}

function TrialConfirmationText() {
  const t = useTranslations("agent_trial");
  return <p className="tcf-note bh-trial-confirm-line">{t("confirm_trial_line")}</p>;
}

/** The Owner's own case questions, for the first screen of a trial (canvas ⑫): at most two, never sent by themselves. */
export function trialSuggestionQuestions(manager: Pick<AgentEvalManager, "suites"> | null, limit = 2): string[] {
  const suite = manager?.suites.find((item) => item.suite_kind === "owner_quality" && item.state !== "archived");
  const questions = (suite?.revision?.cases ?? [])
    .map((item) => (item.task ?? item.input?.task ?? "").trim())
    .filter(Boolean);
  return [...new Set(questions)].slice(0, limit);
}

/** First open of an empty trial chat: 「像成员一样问它一个问题」 and up to two of the Owner's case questions. */
export function TrialFirstQuestions({ onPick }: { onPick: (question: string) => void }) {
  const trial = useAgentTrialChat();
  return trial && trial.endedVersion === null && !trial.unavailable ? <FirstQuestions agentId={trial.agentId} onPick={onPick} /> : null;
}

function FirstQuestions({ agentId, onPick }: { agentId: string; onPick: (question: string) => void }) {
  const t = useTranslations("agent_trial");
  const [questions, setQuestions] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    // The same read as 测试用例; without cases (or without evaluation) only the first line shows.
    void getAgentEvalManager(agentId).then((manager) => {
      if (!cancelled) setQuestions(trialSuggestionQuestions(manager));
    }).catch(() => {
      if (!cancelled) setQuestions([]);
    });
    return () => { cancelled = true; };
  }, [agentId]);
  return (
    <div className="bh-trial-first">
      <p className="bh-trial-first-title">{t("first_title")}</p>
      {questions.length ? <>
        <p className="bh-trial-first-meta">{t("first_from_cases")}</p>
        <div className="bh-trial-first-list">
          {questions.map((question) => (
            <Button key={question} size="sm" variant="secondary" onClick={() => onPick(question)}>{question}</Button>
          ))}
        </div>
      </> : null}
    </div>
  );
}

/** Hides children in a trial chat (thumbs and sharing have no place there). */
export function NotInTrial({ children }: { children: ReactNode }) {
  return useAgentTrialChat() ? null : <>{children}</>;
}
