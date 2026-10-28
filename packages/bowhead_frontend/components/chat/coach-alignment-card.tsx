"use client";

/**
 * Coach alignment loop cards in Chat (Claude Design "Agent Eval Alignment Loop" L1–L4; technical design §13.1).
 *
 * Both cards come from the Turn's `coach.alignment_card` pointers, which live and replayed Turns project
 * through the same dispatcher, and read the loop through the Owner's own route. The question card lists
 * Coach's verdicts; the Owner marks only the rows Coach got wrong or can't tell, then presses "The rest are
 * right". Settled cards collapse to one line in place, so live and replayed Turns show the same thing.
 */
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Info, ListChecks, Lock, SquarePen } from "lucide-react";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import { CheckRow } from "@/components/ui/check-row";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import {
  cardLabelsBody,
  cardRows,
  getAgentEvalAlignment,
  labelAgentEvalAlignment,
  latestCardSeq,
  repinAgentEvalAlignment,
  rewordAgentEvalAlignmentPoint,
  type AlignmentCount,
  type AlignmentLabel,
  type AlignmentLoop,
  type AlignmentPassing,
  type AlignmentQuestion,
  type AlignmentReport,
} from "@/lib/agent-eval-alignment-api";
import { getAgentEvalManager } from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { appPath } from "@/lib/base-path";
import { JUDGE_MODEL_SETTINGS_PATH, useCanConfigureModels } from "@/lib/use-can-configure-models";
import { useAgentEvalAlignmentCapability } from "@/lib/use-agent-eval-alignment-capability";
import {
  coachAlignmentCards,
  coachAlignmentCardsVersion,
  isLatestAlignmentPointer,
  requestCoachAlignmentContinue,
  subscribeCoachAlignmentCards,
  type CoachAlignmentCardOpen,
} from "@/lib/coach-alignment-cards";

const LABELS: AlignmentLabel[] = ["right", "wrong", "unclear"];
/** The continue message for the server's outcome after the Owner's labels (technical design §5.4). */
const CONTINUE_BY_OUTCOME: Record<string, "continue_labels_confirmed" | "continue_labels_counterexample" | "continue_labels_reword" | "continue_labels_unclear" | "continue_labels_more"> = {
  confirmed: "continue_labels_confirmed",
  confirm: "continue_labels_confirmed",
  need_counterexample: "continue_labels_counterexample",
  rewrite: "continue_labels_reword",
  split_or_informational: "continue_labels_reword",
  unclear: "continue_labels_unclear",
  // Every row labelled but too few count yet (a right on an abstained verdict): Coach asks another card.
  ask_more_answers: "continue_labels_more",
};
/**
 * Where the loop is and the one next step, in plain words, from the point's server-side `next_step`.
 * Read from the loop itself, so a live card and its replay say the same thing.
 */
const STEP_TEXT: Record<string, "step_propose_cases" | "step_confirm" | "step_propose_rewrite" | "step_split" | "step_restate" | "step_counterexample" | "step_more_answers"> = {
  propose_cases: "step_propose_cases",
  confirm: "step_confirm",
  propose_rewrite: "step_propose_rewrite",
  suggest_split_or_informational: "step_split",
  ask_owner_to_restate: "step_restate",
  ask_counterexample: "step_counterexample",
  ask_more_answers: "step_more_answers",
};
const HELD_OUT_HELP = "/help/?article=agent-evaluation-with-coach";

type LoadState =
  | { phase: "loading" }
  | { phase: "denied" }
  | { phase: "missing" }
  | { phase: "failed" }
  | { phase: "ready"; loop: AlignmentLoop };

function useAlignmentLoop(open: CoachAlignmentCardOpen, refreshKey: unknown) {
  const auth = useOptionalAuthSession();
  const me = auth?.me?.id ?? null;
  const foreign = Boolean(me && me !== open.userId);
  const [state, setState] = useState<LoadState>({ phase: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (foreign) {
      setState({ phase: "denied" });
      return;
    }
    const controller = new AbortController();
    void getAgentEvalAlignment(open.pointer.agent_id, open.pointer.loop_id, controller.signal)
      .then((loop) => { if (!controller.signal.aborted) setState({ phase: "ready", loop }); })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        // Only "not your loop" reads as Owner-only; a loop that no longer exists says so.
        if (cause instanceof ApiError && cause.status === 404) setState({ phase: cause.code === "agent_eval_alignment_not_found" ? "missing" : "denied" });
        else setState({ phase: "failed" });
      });
    return () => controller.abort();
  }, [open.pointer.agent_id, open.pointer.loop_id, foreign, attempt, refreshKey]);
  const setLoop = useCallback((loop: AlignmentLoop) => setState({ phase: "ready", loop }), []);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  return { state, setLoop, retry };
}

/** Every alignment card a Turn's Coach Tools pointed at, for the conversation it belongs to. */
export function CoachAlignmentCards({ turnId, sessionId }: { turnId: string | null | undefined; sessionId: string | null | undefined }) {
  const t = useTranslations("agent_alignment");
  const revision = useSyncExternalStore(subscribeCoachAlignmentCards, coachAlignmentCardsVersion, coachAlignmentCardsVersion);
  const enabled = useAgentEvalAlignmentCapability();
  if (!turnId || !sessionId) return null;
  const items = coachAlignmentCards(turnId).filter((open) => open.sessionId === sessionId);
  if (!items.length) return null;
  // FEATURE_AGENT_EVAL_ALIGNMENT off: a pointer replayed from when it was on shows one neutral line,
  // and no card reads the loop (its routes answer 404 while off).
  if (!enabled) return <StateRow icon={Info} text={t("flow_unavailable")} />;
  return (
    <>
      {items.map((open) => open.pointer.kind === "agent_eval_alignment_card"
        ? <AlignmentQuestionCard key={open.requestId} open={open} latest={isLatestAlignmentPointer(open)} revision={revision} />
        : <AlignmentReportCard key={open.requestId} open={open} revision={revision} />)}
    </>
  );
}

function errorReason(cause: unknown): string {
  return cause instanceof Error && cause.message ? cause.message : "";
}

function StateRow({ icon, text, action }: { icon: typeof Info; text: string; action?: { label: string; onSelect: () => void } }) {
  return (
    <ChatCard
      intent="info"
      size="row"
      icon={icon}
      title={text}
      live="off"
      className="ui-alignment-card"
      trailing={action ? <ChatCardButton variant="ghost" onClick={action.onSelect}>{action.label}</ChatCardButton> : undefined}
    />
  );
}

type CardMode =
  | { kind: "answer" }
  | { kind: "reword"; text: string }
  | { kind: "done"; note: string | null; continued: boolean; message: string }
  | { kind: "stale" }
  | { kind: "superseded" }
  | { kind: "closed" }
  | { kind: "resolved" };

export function AlignmentQuestionCard({ open, latest, revision }: { open: CoachAlignmentCardOpen; latest: boolean; revision?: number }) {
  const t = useTranslations("agent_alignment");
  const pointer = open.pointer.kind === "agent_eval_alignment_card" ? open.pointer : null;
  // Any new alignment pointer in the conversation (a newer card, a replacement row, a report) may settle this
  // card: read the loop again rather than trusting what was read when the card first appeared.
  const { state, setLoop, retry } = useAlignmentLoop(open, `${latest}:${revision ?? 0}`);
  const [marks, setMarks] = useState<Record<string, AlignmentLabel | null>>({});
  const [mode, setMode] = useState<CardMode>({ kind: "answer" });
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState<"labels" | "reword" | "keep" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loop = state.phase === "ready" ? state.loop : null;
  const point = loop && pointer ? loop.points.find((item) => item.point_id === pointer.point_id) ?? null : null;
  const rows = useMemo(() => (loop && pointer ? cardRows(loop, pointer.point_id, pointer.card_seq) : []), [loop, pointer]);
  // Rows can disappear between reads (a member withdrew a share): drop marks for rows that are gone.
  useEffect(() => {
    setMarks((current) => {
      const ids = new Set(rows.map((row) => row.question_id));
      const kept = Object.fromEntries(Object.entries(current).filter(([id]) => ids.has(id)));
      return Object.keys(kept).length === Object.keys(current).length ? current : kept;
    });
  }, [rows]);

  // A message the conversation took but couldn't send after all (Coach unavailable, the Owner started typing)
  // brings back the hint to message Coach themselves.
  const sendContinue = (text: string) => requestCoachAlignmentContinue({
    sessionId: open.sessionId,
    text,
    onUnsent: () => setMode((current) => (current.kind === "done" ? { ...current, continued: false } : current)),
  });

  if (!pointer) return null;
  if (state.phase === "loading") return <ChatCard intent="progress" size="row" title={t("loading")} live="off" className="ui-alignment-card" />;
  if (state.phase === "denied") return <StateRow icon={Lock} text={t("owner_only")} />;
  if (state.phase === "missing") return <StateRow icon={Info} text={t("loop_missing")} />;
  if (state.phase === "failed" || !loop) return <StateRow icon={Info} text={t("load_failed")} action={{ label: t("retry"), onSelect: retry }} />;
  if (loop.state === "abandoned" || mode.kind === "closed") return <StateRow icon={Info} text={t("closed")} />;
  if (!point) return <StateRow icon={Info} text={t("gone")} />;
  if (mode.kind === "superseded") return <StateRow icon={ListChecks} text={t("superseded")} />;
  if (mode.kind === "stale") {
    return <StateRow icon={Info} text={t("stale")} action={{ label: t("stale_action"), onSelect: () => { sendContinue(t("continue_stale")); setMode({ kind: "resolved" }); } }} />;
  }
  if (mode.kind === "resolved" && point.status === "confirmed") return <StateRow icon={ListChecks} text={t("resolved")} />;

  const labelled = rows.filter((row) => row.label !== null);
  const recheck = point.status !== "confirmed" && latest ? rows.filter((row) => point.recheck_question_ids.includes(row.question_id)) : [];
  const newest = latestCardSeq(loop) === pointer.card_seq;
  // Rows asked under wording the point no longer has: Coach asks again, so this card only reports that.
  const reworded = point.status !== "confirmed" && rows.some((row) => row.stale);
  const settled = point.status === "confirmed" || !latest || !newest || reworded || mode.kind === "done" || mode.kind === "resolved"
    || (rows.length > 0 && labelled.length === rows.length && !recheck.length);

  if (!rows.length) return <StateRow icon={Info} text={t("gone")} />;

  const rowNote = (row: AlignmentQuestion) => row.pick_reason === "counterexample"
    ? t("note_edited")
    : row.generated
      ? t("note_generated")
      : row.pick_reason === "marked_bad" || row.pick_reason === "feedback_conflict"
        // A shared Turn names who gave the thumbs-down (read live); the Owner's own Turn names no one.
        ? row.shared_by ? t("note_marked_bad_by", { name: row.shared_by }) : t("note_marked_bad")
        : null;
  const verdictText = (row: AlignmentQuestion) => row.verdict.met === true
    ? t("verdict_met")
    : row.verdict.met === false
      ? row.verdict.reason?.trim() ? t("verdict_not_met", { reason: row.verdict.reason.trim() }) : t("verdict_not_met_plain")
      : t("verdict_unknown");

  const choice = (row: AlignmentQuestion, value: AlignmentLabel | null, editable: boolean) => ({ verdictId }: { verdictId: string }) => (
    <SegmentedControl<AlignmentLabel>
      value={value}
      activation="manual"
      aria-label={t("label_group")}
      aria-describedby={verdictId}
      aria-disabled={!editable || undefined}
      busy={busy !== null}
      data-testid={`alignment-choice-${row.question_id}`}
      onValueChange={(next) => {
        if (!editable) return;
        // Pressing the chosen segment again goes back to "agree with Coach".
        setMarks((current) => ({ ...current, [row.question_id]: current[row.question_id] === next ? null : next }));
      }}
    >
      {LABELS.map((label) => <Segment key={label} value={label} disabled={!editable}>{t(`label_${label}`)}</Segment>)}
    </SegmentedControl>
  );

  const renderRow = (row: AlignmentQuestion, editable: boolean, value: AlignmentLabel | null) => (
    <CheckRow
      key={row.question_id}
      question={row.question}
      note={rowNote(row)}
      excerpt={row.answer_excerpt}
      quote={row.verdict.quote}
      verdict={verdictText(row)}
      moreLabel={t("more")}
      control={choice(row, value, editable)}
      data-testid={`alignment-row-${row.question_id}`}
    />
  );

  const handleFailure = (cause: unknown, failedKey: "labels_failed" | "reword_failed") => {
    if (cause instanceof ApiError && cause.status === 409 && cause.code === "agent_eval_alignment_point_changed") {
      setMode({ kind: "stale" });
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_card_stale") {
      // A newer card for this point exists: settle here and point to it.
      setMode({ kind: "superseded" });
      retry();
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_closed") {
      setMode({ kind: "closed" });
      return;
    }
    if (cause instanceof ApiError && cause.status === 409 && cause.code === "agent_eval_alignment_point_resolved") {
      setMode({ kind: "resolved" });
      retry();
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_point_conflict") {
      setError(t("point_conflict"));
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_point_not_found") {
      setError(t("point_not_found"));
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_card_full") {
      const limit = Number(cause.details?.limit);
      setError(t("card_full", { limit: Number.isSafeInteger(limit) && limit > 0 ? limit : 5 }));
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_case_held_out") {
      setError(t("case_held_out"));
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_action_denied") {
      setError(t("owner_only"));
      return;
    }
    if (cause instanceof ApiError && cause.code === "agent_eval_alignment_not_found") {
      setError(t("loop_missing"));
      return;
    }
    setError(t(failedKey, { reason: errorReason(cause) || t("reason_unknown") }));
  };

  const submit = async (onlyRows: AlignmentQuestion[] | null) => {
    if (busy) return;
    setBusy("labels");
    setError(null);
    try {
      // Decide from fresh data: a newer card, a reworded point or a confirmation may have happened meanwhile.
      const fresh = await getAgentEvalAlignment(pointer.agent_id, pointer.loop_id);
      const freshPoint = fresh.points.find((item) => item.point_id === pointer.point_id);
      const freshRows = cardRows(fresh, pointer.point_id, pointer.card_seq);
      if (!freshPoint || freshPoint.status === "confirmed" || latestCardSeq(fresh) !== pointer.card_seq || freshRows.some((row) => row.stale)
        || fresh.state === "abandoned") {
        setLoop(fresh);
        return;
      }
      const ids = new Set(freshRows.map((row) => row.question_id));
      const body = onlyRows
        ? {
            labels: onlyRows.filter((row) => ids.has(row.question_id) && marks[row.question_id])
              .map((row) => ({ question_id: row.question_id, label: marks[row.question_id] as AlignmentLabel })),
            rest_right: false,
            card_seq: pointer.card_seq,
          }
        : cardLabelsBody(freshRows.filter((row) => row.label === null || marks[row.question_id]), marks, pointer.card_seq);
      const result = await labelAgentEvalAlignment(pointer.agent_id, pointer.loop_id, body);
      setLoop(result.loop);
      setMarks({});
      // Rows anywhere in the loop may have gone (a withdrawn share): read the loop again.
      if (result.dropped_question_ids.length) retry();
      const again = result.recheck_question_ids.filter((id) => rows.some((row) => row.question_id === id));
      if (again.length) return; // Ask about that row alone, on this card (L3-e).
      const dropped = result.dropped_question_ids.filter((id) => rows.some((row) => row.question_id === id)).length;
      // Say what the server decided, so Coach continues from the outcome rather than guessing it.
      const outcome = result.outcomes[pointer.point_id];
      const step = result.loop.points.find((item) => item.point_id === pointer.point_id)?.next_step;
      const key = outcome === "continue" && step === "ask_more_answers" ? step : outcome;
      const message = key && key in CONTINUE_BY_OUTCOME ? t(CONTINUE_BY_OUTCOME[key]) : t("continue_labels");
      setMode({ kind: "done", note: dropped ? t("dropped", { count: dropped }) : null, continued: sendContinue(message), message });
    } catch (cause) {
      handleFailure(cause, "labels_failed");
    } finally {
      setBusy(null);
    }
  };

  const reword = async (text: string, kind: "reword" | "keep") => {
    const value = text.trim().replace(/\s+/g, " ");
    if (!value || busy) return;
    setBusy(kind);
    setError(null);
    try {
      const updated = await rewordAgentEvalAlignmentPoint(pointer.agent_id, pointer.loop_id, pointer.point_id, value);
      setLoop(updated);
      const message = kind === "keep" ? t("continue_kept") : t("continue_reworded", { text: value });
      setMode({ kind: "done", note: kind === "keep" ? t("kept_original") : t("reworded", { text: value }), continued: sendContinue(message), message });
    } catch (cause) {
      handleFailure(cause, "reword_failed");
    } finally {
      setBusy(null);
    }
  };

  if (settled) {
    const note = mode.kind === "done" ? mode.note : reworded ? t("wording_changed") : null;
    // A newer card for this point carries the conversation on; otherwise say where the point stands and what happens next.
    const stepKey = STEP_TEXT[point.status === "confirmed" ? "propose_cases" : point.next_step];
    const step = !reworded && (!latest || !newest) && point.status !== "confirmed" ? t("step_newer_card") : stepKey && !reworded ? t(stepKey) : null;
    const resend = () => {
      if (mode.kind !== "done") return;
      setMode({ ...mode, continued: sendContinue(mode.message) });
    };
    return (
      <>
        <ChatCard
          intent="info"
          size="row"
          icon={ListChecks}
          live="off"
          className="ui-alignment-card"
          data-testid="alignment-card-settled"
          title={<>{note ?? point.text}{labelled.length ? <span className="ui-card__row-meta"> · {t("checked", { count: labelled.length })}</span> : null}</>}
          trailing={<ChatCardButton variant="ghost" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{t(expanded ? "hide" : "view")}</ChatCardButton>}
        />
        {step ? <p className="ui-card__note" data-testid="alignment-card-step">{step}</p> : null}
        {mode.kind === "done" && !mode.continued ? (
          <p className="ui-card__note">
            {t("continue_unsent")}{" "}
            <ChatCardButton variant="ghost" onClick={resend}>{t("continue_send")}</ChatCardButton>
          </p>
        ) : null}
        {expanded ? (
          <ChatCard intent="info" icon={ListChecks} eyebrow={t("eyebrow_point")} title={point.text} live="off" className="ui-alignment-card">
            {rows.map((row) => renderRow(row, false, row.label))}
          </ChatCard>
        ) : null}
      </>
    );
  }

  if (recheck.length) {
    const chosen = recheck.every((row) => marks[row.question_id]);
    return (
      <ChatCard
        intent="info"
        icon={ListChecks}
        title={t("recheck_title")}
        live="off"
        className="ui-alignment-card"
        data-testid="alignment-card-recheck"
        error={error ? { message: error } : undefined}
        primary={{ label: t("recheck_submit"), onSelect: () => submit(recheck), busy: busy === "labels", disabled: !chosen, testId: "alignment-recheck-submit" }}
      >
        {recheck.map((row, index) => (
          <CheckRow
            key={row.question_id}
            first={index === 0}
            question={row.question}
            note={rowNote(row)}
            excerpt={row.answer_excerpt}
            quote={row.verdict.quote}
            verdict={verdictText(row)}
            moreLabel={t("more")}
            control={choice(row, marks[row.question_id] ?? null, true)}
          />
        ))}
      </ChatCard>
    );
  }

  if (mode.kind === "reword") {
    const inputId = `alignment-reword-${pointer.point_id}-${pointer.card_seq}`;
    return (
      <ChatCard
        intent="info"
        icon={ListChecks}
        eyebrow={<label htmlFor={inputId}>{t("eyebrow_point")}</label>}
        live="off"
        className="ui-alignment-card"
        data-testid="alignment-card-reword"
        error={error ? { message: error } : undefined}
        secondary={[{ label: t("reword_cancel"), onSelect: () => { setMode({ kind: "answer" }); setError(null); }, disabled: busy !== null }]}
        primary={{
          label: t("reword_save"),
          onSelect: () => reword(mode.text, "reword"),
          busy: busy === "reword",
          disabled: !mode.text.trim() || mode.text.trim() === point.text,
          testId: "alignment-reword-save",
        }}
      >
        <input id={inputId} className="settings-input" value={mode.text} maxLength={300} autoFocus
          onChange={(event) => setMode({ kind: "reword", text: event.target.value })} />
        <span className="ui-card__note">{t("reword_hint")}</span>
      </ChatCard>
    );
  }

  const open_ = rows.filter((row) => row.label === null);
  const marked = rows.filter((row) => marks[row.question_id]);
  const rest = open_.filter((row) => !marks[row.question_id]).length;
  const real = rows.filter((row) => !row.generated).length;
  const title = point.previous_text ? (
    <><del className="ui-card__del">{point.previous_text}</del>{" "}<ins className="ui-card__ins">{point.text}</ins></>
  ) : point.text;
  return (
    <ChatCard
      intent="info"
      icon={ListChecks}
      eyebrow={t("eyebrow_point")}
      meta={t("rows_count", { count: rows.length })}
      title={title}
      live="off"
      className="ui-alignment-card"
      data-testid="alignment-card"
      error={error ? { message: error } : undefined}
      secondary={[
        { label: t("reword"), icon: SquarePen, onSelect: () => setMode({ kind: "reword", text: point.text }), disabled: busy !== null, testId: "alignment-reword" },
        ...(point.previous_text ? [{ label: t("keep_original"), onSelect: () => reword(point.previous_text as string, "keep"), busy: busy === "keep", testId: "alignment-keep-original" }] : []),
      ]}
      primary={{
        label: rest > 0 ? t("rest_right") : t("submit"),
        ariaLabel: rest > 0 ? t("rest_right_aria", { count: rest }) : undefined,
        onSelect: () => submit(null),
        busy: busy === "labels",
        disabled: rest === 0 && marked.length === 0,
        testId: "alignment-rest-right",
      }}
    >
      {real < 2 ? <span className="ui-card__text">{t("few_real", { count: real })}</span> : null}
      {rows.map((row) => renderRow(row, row.label === null || Boolean(marks[row.question_id]), marks[row.question_id] ?? row.label))}
      {open.notice === "held_out_once_per_point" ? (
        <span className="ui-card__notice">
          <Info size={14} aria-hidden />
          <span>{t("held_out_notice")}{" "}<a href={appPath(HELD_OUT_HELP)} target="_blank" rel="noreferrer">{t("held_out_learn")}</a></span>
        </span>
      ) : null}
    </ChatCard>
  );
}

// ── Report card (L4) ───────────────────────────────────────────────────────

function useCaseNames(agentId: string, keys: string[]): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>({});
  const wanted = keys.join("\n");
  useEffect(() => {
    if (!wanted) return;
    const controller = new AbortController();
    void getAgentEvalManager(agentId).then((manager) => {
      if (controller.signal.aborted) return;
      const found: Record<string, string> = {};
      for (const suite of manager.suites) {
        for (const item of suite.revision?.cases ?? []) {
          const task = (item.task ?? item.input?.task ?? "").trim();
          if (task) found[item.case_key] = task;
        }
      }
      setNames(found);
    }).catch(() => undefined);
    return () => controller.abort();
  }, [agentId, wanted]);
  return names;
}

export function AlignmentReportCard({ open, revision }: { open: CoachAlignmentCardOpen; revision?: number }) {
  const canConfigureModels = useCanConfigureModels();
  const t = useTranslations("agent_alignment");
  const { state, setLoop, retry } = useAlignmentLoop(open, revision ?? 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<null | { continued: boolean; note: string }>(null);
  const onUnsent = () => setSent((current) => (current ? { ...current, continued: false } : current));
  const report: AlignmentReport | null = state.phase === "ready" ? state.loop.report : null;
  const names = useCaseNames(open.pointer.agent_id, report?.status === "measured" ? [...(report.regressions ?? []), ...(report.to_read ?? []).map((item) => item.case_key)] : []);
  const locale = useLocale();
  const percent = useMemo(() => new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }), [locale]);

  if (state.phase === "loading") return <ChatCard intent="progress" size="row" title={t("loading_report")} live="off" className="ui-alignment-card" />;
  if (state.phase === "denied") return <StateRow icon={Lock} text={t("owner_only_report")} />;
  if (state.phase === "missing") return <StateRow icon={Info} text={t("loop_missing")} />;
  if (state.phase === "failed") return <StateRow icon={Info} text={t("load_failed")} action={{ label: t("retry"), onSelect: retry }} />;
  if (sent) {
    return (
      <>
        <StateRow icon={Info} text={sent.note} />
        {!sent.continued ? <p className="ui-card__note">{t("continue_hint")}</p> : null}
      </>
    );
  }
  // A card for an older report never shows a newer check's numbers; live and replayed cards agree on this.
  const pointerReportId = open.pointer.kind === "agent_eval_alignment_report" ? open.pointer.report_id : undefined;
  // A pointer without an id stands for "no check yet": current only while the loop has no measured report.
  const replaced = pointerReportId ? report?.report_id !== pointerReportId : Boolean(report && report.status !== "no_check");
  if (replaced) return <StateRow icon={ListChecks} text={t("report_replaced")} />;
  const status = report?.status ?? "no_check";
  const caseName = (key: string) => names[key] ?? t("case_unnamed");

  if (status === "judge_changed") {
    const repin = async () => {
      setBusy(true);
      setError(null);
      try {
        const loop = await repinAgentEvalAlignment(open.pointer.agent_id, open.pointer.loop_id);
        setLoop(loop);
        setSent({ note: t("repinned"), continued: requestCoachAlignmentContinue({ sessionId: open.sessionId, text: t("continue_repinned"), onUnsent }) });
      } catch (cause) {
        setError(t("repin_failed", { reason: errorReason(cause) || t("reason_unknown") }));
      } finally {
        setBusy(false);
      }
    };
    return (
      <ChatCard intent="info" icon={Info} eyebrow={t("report_eyebrow")} title={t("judge_changed_title")} live="off" className="ui-alignment-card"
        data-testid="alignment-report" data-state="judge_changed"
        error={error ? { message: error } : undefined}
        primary={{ label: t("repin"), onSelect: repin, busy, testId: "alignment-repin" }}>
        <span className="ui-card__text">{t("judge_changed_body")}</span>
      </ChatCard>
    );
  }
  if (status === "judge_unavailable") {
    return (
      <ChatCard intent="info" icon={Info} eyebrow={t("report_eyebrow")} title={t("judge_unavailable_title")} live="off" className="ui-alignment-card" data-testid="alignment-report" data-state="judge_unavailable">
        {/* Only people who can configure models get the link; everyone else is told who can fix it. */}
        <span className="ui-card__text">
          {canConfigureModels ? <>{t("judge_unavailable_body_admin")}{" "}<a href={appPath(JUDGE_MODEL_SETTINGS_PATH)}>{t("judge_settings_link")}</a></> : t("judge_unavailable_body")}
        </span>
      </ChatCard>
    );
  }
  if (status !== "measured" || !report) {
    return (
      <ChatCard intent="info" icon={Info} eyebrow={t("report_eyebrow")} title={t("no_check_title")} live="off" className="ui-alignment-card" data-testid="alignment-report" data-state="no_check">
        <span className="ui-card__text">{t("no_check_body")}</span>
      </ChatCard>
    );
  }

  const targets = report.target_points ?? [];
  const heldOut = report.held_out;
  const regressions = report.regressions ?? [];
  const worse = regressions.length + (heldOut?.regressions ?? 0);
  const improved = targets.some((item) => item.after.met > item.before.met);
  const titleKey = worse > 0 ? (improved ? "report_improved_regressed" : "report_unchanged_regressed") : improved ? "report_improved" : "report_unchanged";
  // Shares only from three cases (R12): below that the API gives no rate and the card shows counts.
  const share = (value: AlignmentCount | AlignmentPassing, count: number) =>
    typeof value.rate === "number" ? percent.format(value.rate) : t("fraction", { count, cases: value.cases });
  const independent = targets.filter((item) => !item.independent_check);
  const fix = () => setSent({ note: t("fixing"), continued: requestCoachAlignmentContinue({ sessionId: open.sessionId, text: t("continue_fix"), onUnsent }) });

  return (
    <ChatCard
      intent="info"
      icon={ListChecks}
      eyebrow={t("report_eyebrow")}
      title={t(titleKey, { count: worse })}
      live="off"
      className="ui-alignment-card"
      data-testid="alignment-report"
      data-state="measured"
      primary={worse > 0 ? { label: t("fix_worse"), onSelect: fix, testId: "alignment-fix" } : undefined}
    >
      <div className="ui-card__stats">
        {targets.map((item) => (
          <div key={item.point_id} className="ui-card__stat" data-testid={`alignment-stat-${item.point_id}`}>
            <span>{targets.length === 1 ? t("stat_raised") : item.text}</span>
            <b>{share(item.after, item.after.met)}</b>
            <small>{t("stat_before", { value: share(item.before, item.before.met) })}</small>
          </div>
        ))}
        {heldOut && heldOut.cases > 0 ? (
          <div className="ui-card__stat" data-testid="alignment-stat-held-out" title={t("stat_held_out_tip")}>
            <span>{t("stat_held_out")}</span>
            <b>{share(heldOut.after, heldOut.after.passing)}</b>
            <small>{t("stat_before", { value: share(heldOut.before, heldOut.before.passing) })}</small>
          </div>
        ) : null}
        <div className="ui-card__stat" data-testid="alignment-stat-worse">
          <span>{t("stat_worse")}</span>
          <b>{t("stat_worse_value", { count: worse })}</b>
          {regressions.length ? <small>{regressions.slice(0, 2).map(caseName).join(" · ")}</small> : null}
        </div>
      </div>
      {independent.map((item) => (
        <span key={item.point_id} className="ui-card__note">
          {targets.length === 1 ? t("no_held_out") : t("no_held_out_named", { text: item.text })}
        </span>
      ))}
      {report.to_read?.length ? (
        <>
          <span className="ui-card__note">{t("to_read")}</span>
          {report.to_read.map((item, index) => (
            <CheckRow key={`${item.case_key}:${index}`} first={index === 0} question={caseName(item.case_key)} excerpt={item.excerpt} moreLabel={t("more")} />
          ))}
        </>
      ) : null}
    </ChatCard>
  );
}
