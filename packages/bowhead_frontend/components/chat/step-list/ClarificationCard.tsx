"use client";

import { useTranslations } from "next-intl";
import { Check, Clock, Info, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard, ChatCardButton, ChatCardHint } from "@/components/ui/card";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { projectAnswerValues } from "@/lib/clarification-answers";
import type { ErrorDiagnostics } from "@/lib/error-presentation";
import type { ClarificationAnswer, ThetaClarificationQuestion } from "@/lib/types";

export interface ClarificationMultiSubmit {
  values: string[];
  answers: ClarificationAnswer[];
}

export interface ClarificationAnsweredSummaryProps {
  /** User-selected values; empty on skip. */
  values: string[];
  skipped?: boolean;
  /** Resume still running → a progress row "Continuing with your choice…" follows the answered row. */
  resuming?: boolean;
  /** The server no longer accepts this question (expired or already handled). */
  expired?: boolean;
  /** Put the question back into the composer (expired row; never sends). */
  onAskAgain?: () => void;
  /** The answer was saved but the turn could not continue. */
  resumeFailed?: boolean;
  onRefresh?: () => void;
}

/**
 * Settled clarification rows (Chat Cards Spec §1): answered (success), skipped / expired (info),
 * saved-but-not-continued (failure). TurnContainer renders them after the submit (local optimism) or on
 * `turn.resumed` / `clarification.resolved`, so live and replay show the same row.
 */
export function ClarificationAnsweredSummary({
  values,
  skipped = false,
  resuming = false,
  expired = false,
  onAskAgain,
  resumeFailed = false,
  onRefresh,
}: ClarificationAnsweredSummaryProps) {
  const t = useTranslations("clarification");
  const bold = (chunks: ReactNode) => <b>{chunks}</b>;
  // The answered row replaces the card that had focus: take focus so keyboard and screen reader users are not
  // dropped to the page start. Only when focus was lost (never steal it from the composer or elsewhere).
  const slotRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const active = document.activeElement;
    if (!active || active === document.body) slotRef.current?.focus({ preventScroll: true });
  }, []);
  if (expired) {
    return (
      <div className="ui-card-stack ui-card-turn-slot" ref={slotRef} tabIndex={-1}>
        <ChatCard
          intent="info"
          size="row"
          icon={Clock}
          data-testid="clarification-expired"
          title={t.rich("expired_or_handled", { b: bold })}
          trailing={
            onAskAgain ? (
              <ChatCardButton variant="ghost" onClick={onAskAgain}>
                {t("ask_again")}
              </ChatCardButton>
            ) : null
          }
        />
      </div>
    );
  }
  const answered = skipped || values.length === 0 ? null : values.join("；");
  return (
    <div className="ui-card-stack ui-card-turn-slot" ref={slotRef} tabIndex={-1}>
      {resumeFailed ? (
        <ChatCard
          intent="failure"
          size="row"
          live="off"
          data-testid="clarification-resume-failed"
          title={t.rich("resume_failed_refresh", { b: bold })}
          trailing={
            onRefresh ? (
              <ChatCardButton variant="secondary" onClick={onRefresh}>
                {t("refresh")}
              </ChatCardButton>
            ) : null
          }
        />
      ) : answered === null ? (
        <ChatCard
          intent="info"
          size="row"
          icon={Info}
          data-testid="clarification-answered"
          title={t.rich("answered_skipped", { b: bold })}
        />
      ) : (
        <ChatCard
          intent="success"
          size="row"
          icon={Check}
          data-testid="clarification-answered"
          title={t.rich("answered_line", { b: bold, answer: answered })}
        />
      )}
      {resuming && !resumeFailed ? (
        <ChatCard
          intent="progress"
          size="row"
          data-testid="clarification-resuming"
          title={t.rich("answered_resuming", { b: bold })}
        />
      ) : null}
    </div>
  );
}

export interface ClarificationCardProps {
  questions: ThetaClarificationQuestion[];
  /** Optional title for the multi-question case; a single question is promoted to the title. */
  intro?: string | null;
  /** Submit in flight — locks options and spins the primary action. */
  busy?: boolean;
  /** One question: submit the selected (or typed) answer.
   *  Include opts.remember (tool_context_select) only when showRemember. */
  onSubmitSingle: (answer: ClarificationAnswer, opts?: { remember?: boolean }) => void;
  /** Several questions / multi-select: submit accumulated answers across all groups. */
  onSubmitMulti: (payload: ClarificationMultiSubmit) => void;
  /** Let the model decide without a user-selected answer. */
  onSkip?: () => void;
  /** Datarc workspace selection: "Remember my choice" checkbox at the footer start. */
  showRemember?: boolean;
  /** The initial checkbox value; missing values keep the existing default. */
  rememberDefault?: boolean;
  /** Native source choices stay within the candidates on the original card. */
  allowCustomAnswers?: boolean;
  /** Footer hint (e.g. say "switch to XX for queries" at any time to switch). */
  footHint?: string | null;
  /** Answers of a failed submission: the card comes back with the same choice selected. */
  restoredAnswers?: ClarificationAnswer[] | null;
  /** Submission failed; shown inside the card above the footer (the selection is kept). */
  error?: string | null;
  /** Diagnostics of that failure: collapsed "Technical details" with "Copy diagnostic information". */
  errorDiagnostics?: ErrorDiagnostics;
  /** The answer cannot be accepted yet (a document task is still saving its progress). */
  unavailableReason?: string | null;
  /** The question pauses a document task (spec §3): document eyebrow and meta, "Continue generating", and a
   *  note that the request, template and data are kept. The draft's file name is not in the payload yet. */
  documentDraft?: boolean;
}

function initialSelection(
  questions: ThetaClarificationQuestion[],
  restored: ClarificationAnswer[] | null | undefined,
): { sel: number[][]; custom: string[] } {
  const sel = questions.map(() => [] as number[]);
  const custom = questions.map(() => "");
  for (const answer of restored ?? []) {
    const qi = questions.findIndex((q) => q.question === answer.question);
    if (qi < 0 || answer.source === "skip") continue;
    if (answer.source === "custom") {
      custom[qi] = answer.answer ?? "";
      continue;
    }
    const oi = typeof answer.option_index === "number"
      ? answer.option_index
      : questions[qi].options.indexOf(answer.answer ?? "");
    if (oi >= 0 && oi < questions[qi].options.length) sel[qi] = [...sel[qi], oi];
  }
  return { sel, custom };
}

/**
 * Clarification card (Chat Cards Spec §1, decision D2): the "needs your input" card with radio / checkbox
 * options, an "Other, type your own" field, and one primary action. Picking an option only selects it;
 * "Continue" (or Enter) submits, so a mis-click can be corrected. A single question is the title; several
 * questions are grouped under "Question N". Until something is answered the primary action is unavailable
 * and "Skip and let the model decide" is the way forward.
 *
 * rank_priorities was removed from the tool schema on 2026-09-07; the backend maps it to multi_select.
 */
export function ClarificationCard({
  questions,
  intro,
  busy = false,
  onSubmitSingle,
  onSubmitMulti,
  onSkip,
  showRemember = false,
  rememberDefault = true,
  allowCustomAnswers = true,
  footHint = null,
  restoredAnswers = null,
  error = null,
  errorDiagnostics,
  unavailableReason = null,
  documentDraft = false,
}: ClarificationCardProps) {
  const t = useTranslations("clarification");
  // Unchecked keeps the workspace choice scoped to this session.
  const [remember, setRemember] = useState(rememberDefault);
  const [initial] = useState(() => initialSelection(questions, allowCustomAnswers ? restoredAnswers : restoredAnswers?.filter((answer) => answer.source !== "custom")));
  const [sel, setSel] = useState<number[][]>(initial.sel);
  const [customAnswers, setCustomAnswers] = useState<string[]>(initial.custom);
  const slotRef = useRef<HTMLDivElement>(null);
  // A failed submission brings the card back: focus its primary action (Retry), right under the error, when
  // focus was lost with the card.
  useEffect(() => {
    if (!error) return;
    const active = document.activeElement;
    if (active && active !== document.body && slotRef.current?.contains(active) !== true) return;
    slotRef.current?.querySelector<HTMLButtonElement>(".ui-card__footer > button:last-child")?.focus();
  }, [error]);

  if (questions.length === 0) return null;

  const oneQuestion = questions.length === 1;
  const singleMode = oneQuestion && questions[0].type === "single_select";
  const locked = busy || Boolean(unavailableReason);

  const answeredAt = (qi: number) => (sel[qi]?.length ?? 0) > 0 || Boolean(customAnswers[qi]?.trim());
  const answeredCount = questions.reduce((n, _q, qi) => n + (answeredAt(qi) ? 1 : 0), 0);

  const isQuestionSingle = (q: ThetaClarificationQuestion) => q.type === "single_select";

  const selectOption = (qi: number, oi: number) => {
    if (locked) return;
    const single = isQuestionSingle(questions[qi]);
    setSel((prev) =>
      prev.map((arr, i) => {
        if (i !== qi) return arr;
        if (single) return [oi];
        return arr.includes(oi) ? arr.filter((x) => x !== oi) : [...arr, oi];
      }),
    );
    // A single-choice question has one answer: picking an option replaces typed text.
    if (single) setCustomAnswers((prev) => prev.map((v, i) => (i === qi ? "" : v)));
  };

  const setCustomAnswer = (qi: number, value: string) => {
    if (locked) return;
    setCustomAnswers((prev) => prev.map((v, i) => (i === qi ? value : v)));
    // Typing selects "Other": a single-choice question drops its option selection.
    if (value.trim() && isQuestionSingle(questions[qi])) {
      setSel((prev) => prev.map((arr, i) => (i === qi ? [] : arr)));
    }
  };

  const submit = () => {
    if (locked || answeredCount === 0) return;
    if (singleMode) {
      const q = questions[0];
      const custom = customAnswers[0]?.trim();
      const oi = sel[0]?.[0];
      const answer: ClarificationAnswer = custom
        ? { question: q.question, source: "custom", answer: custom }
        : { question: q.question, source: "candidate", answer: q.options[oi], option_index: oi };
      onSubmitSingle(answer, showRemember ? { remember } : undefined);
      return;
    }
    const answers = questions.flatMap((q, qi): ClarificationAnswer[] => {
      const optionAnswers = (sel[qi] ?? []).flatMap((oi) => {
        const answer = q.options[oi];
        return answer
          ? [{ question: q.question, source: "candidate" as const, answer, option_index: oi }]
          : [];
      });
      const custom = customAnswers[qi]?.trim();
      return [
        ...optionAnswers,
        ...(custom ? [{ question: q.question, source: "custom" as const, answer: custom }] : []),
      ];
    });
    // Projection lives in lib/clarification-answers; callers reproject after relabeling with the same function.
    const values = projectAnswerValues(answers, { multiQuestion: questions.length > 1 });
    onSubmitMulti({ values, answers });
  };

  // Enter submits (D2); Space and arrows only select.
  const onOptionsKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    // keyCode 229: Safari ends an IME composition with an Enter whose isComposing is already false.
    if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    submit();
  };

  const firstUnansweredIndex = questions.findIndex((_q, qi) => !answeredAt(qi));
  const showGroupHint = !oneQuestion && answeredCount > 0 && firstUnansweredIndex >= 0;
  const typeHint = (q: ThetaClarificationQuestion) =>
    q.type === "free_text" ? t("free_text_hint") : isQuestionSingle(q) ? t("single_group_hint") : t("multi_hint");

  const options = (q: ThetaClarificationQuestion, qi: number) => {
    const custom = customAnswers[qi] ?? "";
    const customField = allowCustomAnswers ? (
      <input
        type="text"
        className="ui-input ui-card-option-input"
        value={custom}
        placeholder={t("custom_answer_placeholder")}
        disabled={locked}
        maxLength={240}
        aria-label={t("custom_answer_aria", { question: q.question })}
        onChange={(e) => setCustomAnswer(qi, e.target.value.slice(0, 240))}
        onKeyDown={(event) => {
          event.stopPropagation();
          onOptionsKeyDown(event);
        }}
      />
    ) : null;
    if (isQuestionSingle(q)) {
      const value = sel[qi]?.[0];
      return (
        <div className="ui-card-options">
          {q.options.length > 0 ? (
            // Arrows / Home / End move and select within the group; one Tab stop.
            <RadioGroup
              appearance="none"
              className="ui-card-options"
              orientation="both"
              aria-label={q.question}
              value={value === undefined ? null : String(value)}
              onValueChange={(v) => selectOption(qi, Number(v))}
              onKeyDown={onOptionsKeyDown}
            >
              {q.options.map((label, oi) => {
                const on = sel[qi]?.includes(oi) ?? false;
                return (
                  <Radio
                    key={oi}
                    value={String(oi)}
                    disabled={locked}
                    className={"ui-card-option" + (on ? " is-on" : "")}
                    onClick={(event) => {
                      event.preventDefault();
                      selectOption(qi, oi);
                    }}
                  >
                    <span className="ui-card-option__radio" aria-hidden="true" />
                    <span className="ui-card-option__label">{label}</span>
                  </Radio>
                );
              })}
            </RadioGroup>
          ) : null}
          {customField}
        </div>
      );
    }
    return (
      <div className="ui-card-options" role="group" aria-label={q.question} onKeyDown={onOptionsKeyDown}>
        {q.options.map((label, oi) => {
          const on = sel[qi]?.includes(oi) ?? false;
          return (
            <button
              key={oi}
              type="button"
              role="checkbox"
              aria-checked={on}
              disabled={locked}
              className={"ui-card-option" + (on ? " is-on" : "")}
              onClick={() => selectOption(qi, oi)}
            >
              <span className="ui-card-option__box" aria-hidden="true">
                {on ? <LucideGlyph icon={Check} size={14} aria-hidden="true" /> : null}
              </span>
              <span className="ui-card-option__label">{label}</span>
            </button>
          );
        })}
        {customField}
      </div>
    );
  };

  const primaryLabel = documentDraft
    ? t("document_continue")
    : !oneQuestion && answeredCount > 0
      ? t("continue_n", { count: answeredCount })
      : t("continue");
  const questionMeta = oneQuestion ? typeHint(questions[0]) : t("answered_progress", { done: answeredCount, total: questions.length });

  return (
    <div className="ui-card-turn-slot" ref={slotRef}>
      <ChatCard
        intent="input"
        data-testid="clarification-card"
        eyebrow={documentDraft ? t("document_eyebrow") : t("needs_answer")}
        meta={documentDraft ? `${t("document_meta")} · ${questionMeta}` : questionMeta}
        data-state={documentDraft ? "document_draft" : undefined}
        title={oneQuestion ? questions[0].question : intro || t("multi_title")}
        error={error ? { message: error } : undefined}
        details={error && errorDiagnostics ? { diagnostics: errorDiagnostics, surface: "chat" } : undefined}
        footerStart={
          showRemember ? (
            <label className="ui-card-check">
              <input
                type="checkbox"
                checked={remember}
                disabled={locked}
                onChange={(e) => setRemember(e.target.checked)}
              />
              <span>{t("remember_choice")}</span>
            </label>
          ) : undefined
        }
        secondary={onSkip ? [{ label: t(questions.length > 1 ? "skip.all_multi" : "skip.all"), onSelect: onSkip, disabled: locked }] : undefined}
        primary={{
          label: error ? t("retry") : primaryLabel,
          icon: error ? RotateCcw : undefined,
          busy,
          busyLabel: t("submitting"),
          disabled: answeredCount === 0 && !unavailableReason,
          disabledReason: unavailableReason ?? undefined,
          disabledReasonPending: Boolean(unavailableReason),
          onSelect: submit,
        }}
      >
        {oneQuestion ? (
          options(questions[0], 0)
        ) : (
          questions.map((q, qi) => (
            <div className="ui-card-question" key={qi}>
              <div className="ui-card-question__head">
                <b>{t("question_n", { index: qi + 1 })}</b>
                <span className="ui-card-question__text">{q.question}</span>
                <span className="ui-card-question__type">{typeHint(q)}</span>
              </div>
              {options(q, qi)}
            </div>
          ))
        )}
        {showGroupHint ? (
          <ChatCardHint>{t("group_unanswered", { index: firstUnansweredIndex + 1 })}</ChatCardHint>
        ) : null}
        {footHint ? <ChatCardHint>{footHint}</ChatCardHint> : null}
        {documentDraft ? <ChatCardHint>{t("document_note")}</ChatCardHint> : null}
      </ChatCard>
    </div>
  );
}
