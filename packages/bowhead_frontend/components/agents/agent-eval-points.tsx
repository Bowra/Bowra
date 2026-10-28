"use client";

import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { Plus } from "lucide-react";

import {
  getAgentEvalAlignment,
  getAgentEvalPointCalibration,
  originDisplay,
  pointTextHash,
  reviewAgentEvalPoints,
  type AlignmentLoop,
  type PointCalibration,
  type PointCalibrationExample,
  type PointReviewItem,
} from "@/lib/agent-eval-alignment-api";
import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { appPath } from "@/lib/base-path";
import { JUDGE_MODEL_SETTINGS_PATH, useCanConfigureModels } from "@/lib/use-can-configure-models";
import { ReviewLine, type ReviewLineAction } from "@/components/ui/review-line";
import { useAgentEvalAlignmentCapability } from "@/lib/use-agent-eval-alignment-capability";
import { toolLabel } from "@/lib/tool-labels";

import {
  agentEvalAttemptAnswerRef,
  markAgentEvalPointGoodAnswer,
  previewAgentEvalPoints,
  type AgentEvalPoint,
  type AgentEvalPointKind,
  type AgentEvalPointPreview,
  type AgentEvalPointStatus,
} from "@/lib/agents-api";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { HelpPopover } from "@/components/ui/help-popover";
import { RowActionsMenu, type RowAction } from "@/components/ui/row-actions-menu";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import { Switch } from "@/components/ui/switch";
import { useCiFormat } from "./agent-ci-shared";

// The order of the add form's segmented control (Agent Eval Points E1-b).
const KINDS: AgentEvalPointKind[] = ["data_fact", "required_action", "judgment"];
const MAX_POINTS = 12;

type GradedPoint = AgentEvalPoint & { kind: "data_fact" | "judgment" };

function isGraded(point: AgentEvalPoint): point is GradedPoint {
  return point.kind === "data_fact" || point.kind === "judgment";
}

function newPointId(): string {
  const bytes = new Uint8Array(4);
  crypto.getRandomValues(bytes);
  return `p_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

/** Read the points stored on a case; anything malformed is left out rather than guessed. */
export function casePoints(expected: Record<string, unknown> | undefined): AgentEvalPoint[] {
  const value = expected?.points;
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is AgentEvalPoint => (
    typeof item === "object" && item !== null && typeof (item as AgentEvalPoint).id === "string"
    && typeof (item as AgentEvalPoint).text === "string"
  ));
}

/** The preview grades only points the Owner hasn't confirmed yet (PRD R21). */
function previewTargets(points: AgentEvalPoint[]): GradedPoint[] {
  return points.filter((point): point is GradedPoint => isGraded(point) && point.state === "suggested" && Boolean(point.text.trim()));
}

function previewKey(task: string, targets: GradedPoint[]): string {
  return JSON.stringify([task.trim(), targets.map((point) => [point.id, point.kind, point.text.trim()])]);
}

/** Points Coach reviews: ones the Owner added or edited here, not ones whose Owner labels already back them. */
export function pointsToReview(
  points: AgentEvalPoint[],
  touched: Record<string, "added" | "edited">,
  reviewed: Record<string, string>,
  editing: string | null,
): AgentEvalPoint[] {
  return points.filter((point) => touched[point.id] && !point.origin?.loop_id && point.id !== editing
    && Boolean(point.text.trim()) && reviewed[point.id] !== point.text.trim());
}

const REVIEW_DELAY_MS = 900;

/** One Tool the Agent can use, by its stable reference and the name the Owner sees in the picker. */
export interface EvalPointTool {
  ref: string;
  name: string;
}

// The server accepts at most five alternatives on one required action.
const MAX_ACTION_TOOLS = 5;

/** A required action from the picked Tools: one Tool, or several of which any one meets the point. */
export function pointAction(refs: readonly string[]): { tool: string } | { any_of: string[] } | null {
  const names = [...new Set(refs.map((name) => name.trim()).filter(Boolean))];
  if (!names.length) return null;
  return names.length === 1 ? { tool: names[0] } : { any_of: names };
}

function actionTools(action: AgentEvalPoint["action"]): string[] {
  if (!action) return [];
  return "tool" in action ? [action.tool] : [...action.any_of];
}

/**
 * Picks a required action's Tools from the Agent's own Tools, by display name. A Tool a saved point names that
 * the Agent no longer has stays listed (checked), so editing never drops it silently.
 */
function ToolPicker({ id, tools, selected, disabled, onChange }: {
  id: string;
  tools: readonly EvalPointTool[];
  selected: readonly string[];
  disabled: boolean;
  onChange: (refs: string[]) => void;
}) {
  const t = useTranslations("agents_marketplace");
  const extra = selected.filter((ref) => !tools.some((tool) => tool.ref === ref)).map((ref) => ({ ref, name: toolLabel(ref).name }));
  const options = [...tools, ...extra];
  if (!options.length) return <p id={`${id}-hint`} className="am-ci-note" data-tone="warning">{t("eval_point_tools_none")}</p>;
  const full = selected.length >= MAX_ACTION_TOOLS;
  return (
    <fieldset className="am-ci-scopes am-ev-tools" aria-describedby={`${id}-hint`}>
      <legend>{t("eval_point_tool_label")}</legend>
      {options.map((tool) => {
        const checked = selected.includes(tool.ref);
        const off = disabled || (!checked && full);
        return (
          <label key={tool.ref} className="am-ci-ctl" data-disabled={off || undefined}>
            <input type="checkbox" checked={checked} disabled={off}
              onChange={() => onChange(checked ? selected.filter((ref) => ref !== tool.ref) : [...selected, tool.ref])} />
            <span>{tool.name}</span>
          </label>
        );
      })}
      <p id={`${id}-hint`} className="am-ci-note">{t("eval_point_tool_hint")}</p>
    </fieldset>
  );
}

// Several cases of a suite carry points from the same loop: read each loop once per page.
const loopReads = new Map<string, Promise<AlignmentLoop>>();
function readLoop(agentId: string, loopId: string): Promise<AlignmentLoop> {
  const key = `${agentId}:${loopId}`;
  let read = loopReads.get(key);
  if (!read) {
    read = getAgentEvalAlignment(agentId, loopId);
    loopReads.set(key, read);
    read.catch(() => loopReads.delete(key));
  }
  return read;
}

/** Test hook: forget the cached loop reads. */
export function resetLoopReadsForTest(): void {
  loopReads.clear();
}

export function EvalCasePoints({
  agentId,
  caseNumber,
  task,
  points,
  saved,
  pointStatus,
  disabled,
  onChange,
  suiteId,
  onCheckOnAnswers,
  tools = [],
}: {
  agentId: string;
  caseNumber: number;
  task: string;
  points: AgentEvalPoint[];
  /** The saved case revision with this task, and the point texts it holds. */
  saved: { caseRevisionId: string; pointTexts: Record<string, string> } | null;
  pointStatus?: Record<string, AgentEvalPointStatus>;
  disabled: boolean;
  onChange: (points: AgentEvalPoint[]) => void;
  /** The saved Owner suite, for 「查看校准」 on a saved confirmed point. */
  suiteId?: string | null;
  /** 「在回答上核对」: ask Coach to align this point on real answers. */
  onCheckOnAnswers?: (point: AgentEvalPoint) => void;
  /** The Agent's own Tools, for a required action's picker. */
  tools?: readonly EvalPointTool[];
}) {
  const t = useTranslations("agents_marketplace");
  const canConfigureModels = useCanConfigureModels();
  const toolName = (ref: string) => tools.find((tool) => tool.ref === ref)?.name ?? toolLabel(ref).name;
  const id = useId();
  const [calibration, setCalibration] = useState<{ pointId: string; value: PointCalibration | "loading" | "failed" } | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftKind, setDraftKind] = useState<AgentEvalPointKind>("judgment");
  const [draftText, setDraftText] = useState("");
  const [draftTools, setDraftTools] = useState<string[]>([]);
  // Editing a point: a kind change to a required action waits until its Tool is picked.
  const [editKind, setEditKind] = useState<AgentEvalPointKind | null>(null);
  const [preview, setPreview] = useState<{ key: string; texts: Record<string, string>; value: AgentEvalPointPreview } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewFailed, setPreviewFailed] = useState(false);
  const [goodAnswer, setGoodAnswer] = useState<"saving" | "marked" | "needs_save" | "failed" | "held_out" | null>(null);
  const format = useCiFormat();
  const me = useOptionalAuthSession()?.me?.id ?? null;
  // FEATURE_AGENT_EVAL_ALIGNMENT: origin, confirmation basis and point review show only while it is on.
  const alignment = useAgentEvalAlignmentCapability();
  // Point review (advice only): which points the Owner added or edited here, and what Coach said about each text.
  const [touched, setTouched] = useState<Record<string, "added" | "edited">>({});
  const [reviews, setReviews] = useState<Record<string, { text: string; busy: boolean; item: PointReviewItem | null }>>({});
  const reviewedText = useMemo(() => Object.fromEntries(Object.entries(reviews).map(([key, value]) => [key, value.text])), [reviews]);
  const [loops, setLoops] = useState<Record<string, AlignmentLoop | "unavailable">>({});
  const [basisOpen, setBasisOpen] = useState<string | null>(null);
  const reviewControllers = useRef(new Set<AbortController>());
  const pointsRef = useRef(points);
  pointsRef.current = points;

  const targets = useMemo(() => previewTargets(points), [points]);
  const currentKey = useMemo(() => previewKey(task, targets), [task, targets]);

  const runPreview = async () => {
    if (!task.trim() || !targets.length || previewing) return;
    setPreviewing(true);
    setPreviewFailed(false);
    try {
      const value = await previewAgentEvalPoints(agentId, {
        task: task.trim(),
        points: targets.map((point) => ({ id: point.id, kind: point.kind, text: point.text.trim() })),
      });
      setPreview({ key: currentKey, texts: Object.fromEntries(targets.map((point) => [point.id, point.text.trim()])), value });
      setGoodAnswer(null);
    } catch {
      setPreviewFailed(true);
    } finally {
      setPreviewing(false);
    }
  };

  // One judge call per case when the Owner asks to see the Live answer (PRD R27); later edits offer "Preview again".
  useEffect(() => {
    if (previewOpen && preview === null && !previewFailed && !disabled) void runPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewOpen]);

  const update = (pointId: string, change: Partial<AgentEvalPoint>) => {
    if (change.text !== undefined || change.kind !== undefined || "action" in change) {
      setTouched((current) => ({ ...current, [pointId]: current[pointId] ?? "edited" }));
    }
    onChange(points.map((point) => point.id === pointId ? { ...point, ...change } : point));
  };

  /** Change a point's kind while editing it; only a required action names Tools (picked below the kinds). */
  const changeKind = (point: AgentEvalPoint, kind: AgentEvalPointKind) => {
    setEditKind(kind);
    if (kind === point.kind) return;
    if (kind !== "required_action") update(point.id, { kind, action: undefined });
  };
  const changeTools = (point: AgentEvalPoint, refs: string[]) => {
    // A required action keeps at least one Tool; to drop them all, pick another kind.
    const action = pointAction(refs);
    if (action) update(point.id, { kind: "required_action", action });
  };
  const toggleEditing = (pointId: string | null) => {
    setEditKind(null);
    setEditing(pointId);
  };

  // Coach reviews a point a little after it is added or edited (R17); nothing shows when it has nothing to say.
  const toReview = useMemo(() => pointsToReview(points, touched, reviewedText, editing), [points, touched, reviewedText, editing]);
  useEffect(() => {
    if (!alignment || !toReview.length || !task.trim() || disabled) return;
    const timer = window.setTimeout(() => {
      // Marking the targets busy changes `toReview`; the request outlives that re-run and stops only on unmount.
      const controller = new AbortController();
      reviewControllers.current.add(controller);
      const targets = toReview.slice(0, 12).map((point) => ({ ...point, text: point.text.trim() }));
      const ids = new Set(targets.map((point) => point.id));
      setReviews((current) => ({ ...current, ...Object.fromEntries(targets.map((point) => [point.id, { text: point.text, busy: true, item: null }])) }));
      const body = (point: AgentEvalPoint) => ({ id: point.id, kind: point.kind, text: point.text.trim(), review_dismissed_hash: point.review_dismissed_hash ?? null });
      void reviewAgentEvalPoints(agentId, {
        task: task.trim().slice(0, 8000),
        points: targets.map(body),
        other_points: pointsRef.current.filter((point) => !ids.has(point.id) && point.text.trim()).slice(0, 24).map(body),
      }, controller.signal).then((result) => {
        setReviews((current) => ({
          ...current,
          ...Object.fromEntries(targets.map((point) => [point.id, {
            text: point.text,
            busy: false,
            item: result.points.find((item) => item.id === point.id && item.problem) ?? null,
          }])),
        }));
      }).catch(() => undefined).finally(() => reviewControllers.current.delete(controller));
    }, REVIEW_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [agentId, alignment, disabled, task, toReview]);
  useEffect(() => () => reviewControllers.current.forEach((controller) => controller.abort()), []);

  // Where aligned points were confirmed: the loop's labelled answers.
  const loopIds = useMemo(() => [...new Set(points.map((point) => point.origin?.loop_id).filter((id): id is string => Boolean(id)))], [points]);
  useEffect(() => {
    let live = true;
    if (!alignment) return;
    for (const loopId of loopIds.filter((value) => !(value in loops))) {
      void readLoop(agentId, loopId)
        .then((loop) => { if (live) setLoops((current) => ({ ...current, [loopId]: loop })); })
        .catch(() => { if (live) setLoops((current) => ({ ...current, [loopId]: "unavailable" })); });
    }
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, alignment, loopIds.join(",")]);

  const keepWording = async (point: AgentEvalPoint) => {
    // "Keep as is" dismisses the advice for this exact text; it is saved with the next suite revision (R20).
    const hash = await pointTextHash(point.text.trim());
    onChange(pointsRef.current.map((item) => item.id === point.id ? { ...item, review_dismissed_hash: hash } : item));
    setReviews((current) => ({ ...current, [point.id]: { text: point.text.trim(), busy: false, item: null } }));
  };

  /** "Split in two": the point is replaced, in place, by two points of the same kind and state. */
  const splitPoint = (point: AgentEvalPoint, texts: [string, string]) => {
    const parts: AgentEvalPoint[] = texts.map((text) => ({
      id: newPointId(),
      kind: point.kind,
      text,
      // The Owner's edit: the new points carry no loop origin or dismissed review of the old text.
      source: point.source === "coach_alignment" ? "owner" : point.source,
      state: point.state,
    }));
    setReviews((current) => ({ ...current, ...Object.fromEntries(parts.map((part) => [part.id, { text: part.text, busy: false, item: null }])) }));
    onChange(pointsRef.current.flatMap((item) => item.id === point.id ? parts : [item]));
  };

  const toggleCalibration = (point: AgentEvalPoint) => {
    if (calibration?.pointId === point.id) {
      setCalibration(null);
      return;
    }
    if (!suiteId) return;
    setCalibration({ pointId: point.id, value: "loading" });
    void getAgentEvalPointCalibration(agentId, suiteId, point.id)
      .then((value) => setCalibration((current) => current?.pointId === point.id ? { pointId: point.id, value } : current))
      .catch(() => setCalibration((current) => current?.pointId === point.id ? { pointId: point.id, value: "failed" } : current));
  };

  const exampleName = (example: PointCalibrationExample) => {
    if (example.label_source === "marked_good") return t("eval_calibration_example_marked_good");
    if (example.label_source === "judged_wrongly" || example.kind === "judged_wrongly") return t("eval_calibration_example_judged_wrongly");
    if (example.label_source === "owner_alignment") return t(example.answer_kind === "coach" ? "eval_calibration_example_aligned_coach" : "eval_calibration_example_aligned");
    return t(example.kind === "good" ? "eval_calibration_example_coach_good" : "eval_calibration_example_coach_bad");
  };

  /** E3: the examples a point's calibration rests on, how each should and did grade, the judge and the status. */
  const calibrationPanel = (point: AgentEvalPoint) => {
    if (!alignment || calibration?.pointId !== point.id) return null;
    const value = calibration.value;
    if (value === "loading") return <p className="am-ci-note">{t("eval_calibration_loading")}</p>;
    if (value === "failed") return <p className="am-ci-note" role="status">{t("eval_calibration_failed_load")}</p>;
    const verdict = (met: boolean | null) => t(met === true ? "eval_point_basis_met" : met === false ? "eval_point_basis_not_met" : "eval_point_basis_not_graded");
    const state = value.calibration?.state === "calibrated" && value.status.reason !== "calibrating" && value.status.reason !== "judge_changed"
      ? "calibrated"
      : value.calibration?.state === "failed" && value.status.reason === "calibration_failed" ? "failed" : "pending";
    const suggested = value.calibration?.rewording ?? value.status.rewording;
    const rewording = suggested?.trim() !== point.text.trim() ? suggested : null;
    return (
      <div className="am-ev-calibration" data-testid={`calibration-${point.id}`}>
        <p className="am-ci-note">{t(`eval_calibration_state_${state}`)}</p>
        {value.examples.length ? (
          <ul className="am-ev-basis" aria-label={t("eval_calibration_title")}>
            {value.examples.map((example) => {
              const wrong = example.graded_current && example.graded_met !== null && example.graded_met !== example.expected_met;
              return (
                <li key={example.id} data-wrong={wrong || undefined}>
                  <span className="am-ev-basis-q" title={example.question ?? undefined}>{exampleName(example)}{example.question ? ` · ${example.question}` : ""}</span>
                  <span className="am-ev-basis-v">
                    <span>{t("eval_calibration_expected", { verdict: verdict(example.expected_met) })}</span>
                    <span className={wrong ? "am-ev-basis-you" : undefined}>{t("eval_calibration_actual", { verdict: example.graded_current ? verdict(example.graded_met) : verdict(null) })}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        ) : <p className="am-ci-note">{t("eval_calibration_empty")}</p>}
        {rewording && state === "failed" ? (
          <ReviewLine actions={[{ label: t("eval_point_review_use"), onSelect: () => applyRewording(point, rewording), disabled, testId: "calibration-use" }]}>
            {t("eval_calibration_rewording", { text: rewording })}
          </ReviewLine>
        ) : null}
        {/* By name only, as team model settings show it; never a fingerprint or prompt version. */}
        {value.judge_model_name ? <p className="am-ci-note">{t("eval_calibration_judge", { model: value.judge_model_name })}</p> : null}
      </div>
    );
  };

  const applyRewording = (point: AgentEvalPoint, text: string) => {
    // The Owner chose Coach's text: don't review it again until they change it.
    setReviews((current) => ({ ...current, [point.id]: { text, busy: false, item: null } }));
    onChange(pointsRef.current.map((item) => item.id === point.id ? { ...item, text } : item));
  };

  const reviewLine = (point: AgentEvalPoint) => {
    if (!alignment) return null;
    const review = reviews[point.id];
    if (!review || review.text !== point.text.trim()) return null;
    if (review.busy) return <ReviewLine busy>{t("eval_point_review_busy")}</ReviewLine>;
    const item = review.item;
    if (!item?.problem) return null;
    const relatedIndex = item.related_point_id ? points.findIndex((other) => other.id === item.related_point_id) : -1;
    const related = relatedIndex >= 0 ? points[relatedIndex] : null;
    const rewording = item.rewording?.trim() && item.rewording.trim() !== point.text.trim() ? item.rewording.trim() : null;
    const actions: ReviewLineAction[] = [];
    let sentence: string;
    if (item.problem === "duplicate" || item.problem === "contradiction") {
      const name = related ? { number: relatedIndex + 1, text: related.text.length > 24 ? `${related.text.slice(0, 24)}…` : related.text } : null;
      sentence = name
        ? t(item.problem === "duplicate" ? "eval_point_review_duplicate" : "eval_point_review_contradiction", name)
        : t(item.problem === "duplicate" ? "eval_point_review_duplicate_unnamed" : "eval_point_review_contradiction_unnamed");
      if (item.problem === "duplicate") actions.push({ label: t("eval_point_review_delete"), onSelect: () => onChange(points.filter((other) => other.id !== point.id)), disabled, testId: "point-review-delete" });
    } else if (item.problem === "multiple" && item.split && point.kind !== "required_action") {
      const [first, second] = item.split;
      sentence = t("eval_point_review_multiple_split", { first, second });
      actions.push({ label: t("eval_point_review_split"), onSelect: () => splitPoint(point, [first, second]), disabled, testId: "point-review-split" });
    } else {
      sentence = t(`eval_point_review_${item.problem}` as never);
      if (rewording) {
        sentence = `${sentence} ${t("eval_point_review_try", { text: rewording })}`;
        actions.push({ label: t("eval_point_review_use"), onSelect: () => applyRewording(point, rewording), disabled, testId: "point-review-use" });
      }
    }
    actions.push({ label: t("eval_point_review_keep"), onSelect: () => void keepWording(point), disabled, testId: "point-review-keep" });
    return <ReviewLine actions={actions} data-testid={`point-review-${point.id}`}>{sentence}</ReviewLine>;
  };

  const originLine = (point: AgentEvalPoint) => {
    if (!alignment) return null;
    const shown = originDisplay(point.origin, me);
    if (!shown) return null;
    const date = shown.at ? format.dateTime(shown.at) : null;
    if (shown.kind === "owner") {
      return <p className="am-ci-note">{date ? t("eval_point_origin_owner", { date, words: shown.words }) : t("eval_point_origin_owner_undated", { words: shown.words })}</p>;
    }
    return <p className="am-ci-note">{t("eval_point_origin_member")}</p>;
  };

  /** "Checked on N answers" and, on View, stacked rows that bring out only the answers the Owner disagreed with. */
  const basisSummary = (point: AgentEvalPoint) => {
    const loopId = point.origin?.loop_id;
    const loop = alignment && loopId ? loops[loopId] : undefined;
    if (!loop || loop === "unavailable") return null;
    const rows = loop.questions.filter((question) => question.point_id === point.id && (question.label === "right" || question.label === "wrong"));
    if (!rows.length) return null;
    return { rows, wrong: rows.filter((row) => row.label === "wrong"), others: rows.filter((row) => row.label !== "wrong") };
  };

  const basisToggle = (point: AgentEvalPoint) => {
    const summary = basisSummary(point);
    if (!summary) return null;
    const listId = `${id}-${point.id}-basis`;
    return (
      <span className="am-ev-basis-line">
        <span>{summary.wrong.length
          ? t("eval_point_basis_some_wrong", { count: summary.rows.length, wrong: summary.wrong.length })
          : t("eval_point_basis_all_right", { count: summary.rows.length })}</span>
        <button type="button" className="am-ci-linkbtn" aria-expanded={basisOpen === point.id} aria-controls={listId} onClick={() => setBasisOpen((value) => value === point.id ? null : point.id)}>
          {t(basisOpen === point.id ? "eval_point_basis_hide" : "eval_point_basis_view")}
        </button>
      </span>
    );
  };

  const basisRows = (point: AgentEvalPoint) => {
    const summary = basisSummary(point);
    if (!summary || basisOpen !== point.id) return null;
    const verdict = (met: boolean | null) => t(met === true ? "eval_point_basis_met" : met === false ? "eval_point_basis_not_met" : "eval_point_basis_not_graded");
    return (
      <ul id={`${id}-${point.id}-basis`} className="am-ev-basis" aria-label={t("eval_point_basis_answer")}>
        {summary.wrong.map((row) => (
          <li key={row.question_id} data-wrong="true">
            <span className="am-ev-basis-q" title={row.question}>{row.question}</span>
            <span className="am-ev-basis-v">
              <span>{t("eval_point_basis_coach_says", { verdict: verdict(row.verdict.met) })}</span>
              <span className="am-ev-basis-you">{t("eval_point_basis_you_say", { label: t("eval_point_basis_label_wrong") })}</span>
            </span>
          </li>
        ))}
        {summary.others.length ? (
          <li data-summary="true">
            <span className="am-ev-basis-q">{summary.wrong.length ? t("eval_point_basis_others", { count: summary.others.length }) : t("eval_point_basis_all", { count: summary.others.length })}</span>
            <span className="am-ev-basis-v">
              <span>{t("eval_point_basis_coach_says", { verdict: t("eval_point_basis_split", {
                met: summary.others.filter((row) => row.verdict.met === true).length,
                unmet: summary.others.filter((row) => row.verdict.met === false).length,
              }) })}</span>
              <span className="am-ev-basis-you">{t("eval_point_basis_you_say", { label: t("eval_point_basis_label_right") })}</span>
            </span>
          </li>
        ) : null}
      </ul>
    );
  };

  const addPoint = () => {
    const text = draftText.trim();
    const action = pointAction(draftTools);
    if (!text || (draftKind === "required_action" && !action) || points.length >= MAX_POINTS) return;
    // A point the Owner writes is confirmed when the suite is saved (PRD R11).
    const pointId = newPointId();
    setTouched((current) => ({ ...current, [pointId]: "added" }));
    onChange([...points, {
      id: pointId,
      kind: draftKind,
      text,
      source: "owner",
      state: "confirmed",
      ...(draftKind === "required_action" && action ? { action } : {}),
    }]);
    setDraftText("");
    setDraftTools([]);
    setAdding(false);
  };

  const markGood = async () => {
    const answer = preview?.value.answer;
    if (!answer) return;
    const savedGraded = saved
      ? points.filter((point) => isGraded(point) && saved.pointTexts[point.id] === point.text)
      : [];
    if (!saved || !savedGraded.length) {
      setGoodAnswer("needs_save");
      return;
    }
    setGoodAnswer("saving");
    try {
      for (const point of savedGraded) {
        await markAgentEvalPointGoodAnswer(agentId, point.id, {
          case_revision_id: saved.caseRevisionId,
          answer_ref: agentEvalAttemptAnswerRef(answer.run_id, answer.case_revision_id, answer.attempt),
        });
      }
      setGoodAnswer("marked");
    } catch (cause) {
      setGoodAnswer(cause instanceof ApiError && cause.code === "agent_eval_case_held_out" ? "held_out" : "failed");
    }
  };

  const verdict = (point: AgentEvalPoint) => {
    if (!preview || point.state !== "suggested" || !isGraded(point) || preview.texts[point.id] !== point.text.trim()) return null;
    const row = preview.value.points.find((item) => item.id === point.id);
    if (!row) return null;
    // A data fact is checked against the numbers a run read from its Tools, which a preview doesn't have,
    // so the preview only shows the value the answer states and never calls it met.
    if (point.kind === "data_fact") {
      if (row.value) return <p className="am-ci-note am-ci-point-verdict">{t("eval_point_preview_value", { value: row.value })}{row.quote ? ` · “${row.quote}”` : ""}</p>;
      return <p className="am-ci-note am-ci-point-verdict">{t(row.met === null ? "eval_point_preview_not_graded" : "eval_point_preview_no_value")}</p>;
    }
    if (row.met === true) {
      return <p className="am-ci-note am-ci-point-verdict" data-met="true">{t("eval_point_preview_met", { quote: row.quote ?? "" })}</p>;
    }
    if (row.met === false) {
      return <p className="am-ci-note am-ci-point-verdict" data-met="false">{row.quote ? t("eval_point_preview_not_met", { quote: row.quote }) : t("eval_point_preview_not_met_no_quote")}</p>;
    }
    return <p className="am-ci-note am-ci-point-verdict">{t("eval_point_preview_not_graded")}</p>;
  };

  /**
   * One line of status per point (Agent Eval Points E1). Until the dry run validates model grading, only a
   * required action blocks a release (`BLOCKING_POINT_KINDS`); judgment and data points are informational.
   */
  const statusParts = (point: AgentEvalPoint) => {
    const parts: ReactNode[] = [];
    if (point.action) {
      parts.push(<span key="tool">{"tool" in point.action
        ? t("eval_point_calls", { tool: toolName(point.action.tool) })
        : t("eval_point_tool_any_of", { tools: point.action.any_of.map(toolName).join(t("version_list_comma")) })}</span>);
    }
    if (point.state === "suggested") {
      parts.push(<span key="state">{t("eval_point_suggested_status")}</span>);
      if (point.source !== "owner") parts.push(<span key="source">{t(`eval_point_source_${point.source}` as never)}</span>);
    } else {
      const status = pointStatus?.[point.id];
      // Every confirmed point says whether it stops a bad release (Item 5): only a counted required action does.
      const blocks = point.kind === "required_action" && (!status || status.counts);
      parts.push(<Badge key="badge" size="sm" tone={blocks ? "warning" : "neutral"}>{t(blocks ? "eval_point_blocks" : "eval_point_informational")}</Badge>);
      if (status && !status.counts) {
        parts.push(<span key="state">{t(`eval_point_reason_${status.reason ?? "calibrating"}` as never)}{status.reason === "calibration_failed" && status.rewording && status.rewording.trim() !== point.text.trim() ? ` ${t("eval_point_rewording", { text: status.rewording })}` : null}{status.reason === "judge_unavailable" && canConfigureModels ? <> <a href={appPath(JUDGE_MODEL_SETTINGS_PATH)}>{t("eval_judge_settings_link")}</a></> : null}</span>);
      }
      if (point.source !== "owner" && point.source !== "coach_alignment") parts.push(<span key="source">{t(`eval_point_source_${point.source}` as never)}</span>);
    }
    if (touched[point.id] && editing !== point.id) parts.push(<span key="touched">{t(touched[point.id] === "added" ? "eval_point_just_added" : "eval_point_just_edited")}</span>);
    const checked = basisToggle(point);
    if (checked) parts.push(<span key="basis">{checked}</span>);
    return parts;
  };

  const menu = (point: AgentEvalPoint): RowAction[] => [
    { id: "edit", label: t(editing === point.id ? "eval_point_done" : "eval_point_edit"), disabled, onSelect: () => toggleEditing(editing === point.id ? null : point.id) },
    // FEATURE_AGENT_EVAL_ALIGNMENT: both items exist only while the loop does, and only for model-graded points.
    ...(alignment && point.kind !== "required_action" && onCheckOnAnswers && point.text.trim()
      ? [{ id: "check", label: t("eval_point_check_on_answers"), disabled, onSelect: () => onCheckOnAnswers(point) }]
      : []),
    ...(alignment && point.kind !== "required_action" && suiteId && point.state === "confirmed" && saved?.pointTexts[point.id] !== undefined
      ? [{ id: "calibration", label: t(calibration?.pointId === point.id ? "eval_point_hide_calibration" : "eval_point_view_calibration"), onSelect: () => toggleCalibration(point) }]
      : []),
    { id: "delete", label: t("eval_point_delete"), danger: true, confirms: false, disabled, onSelect: () => onChange(points.filter((item) => item.id !== point.id)) },
  ];

  const answer = preview?.value.answer ?? null;
  return (
    <div className="am-ev-points">
      {points.length ? (
        <ul className="am-ev-pts">
          {points.map((point) => {
            const textId = `${id}-${point.id}-text`;
            const parts = statusParts(point);
            return (
              <li key={point.id} className="am-ev-pt" data-suggested={point.state === "suggested" || undefined} data-testid={`point-${point.id}`}>
                <div className="am-ev-pt-tx">
                  {editing === point.id ? (() => {
                    // Friction 4: the kind and the Tool can change in place; no need to delete and add the point again.
                    const kind = editKind ?? point.kind;
                    const pending = kind === "required_action" && point.kind !== "required_action";
                    return (
                      <div className="am-ev-edit">
                        <SegmentedControl<AgentEvalPointKind> value={kind} onValueChange={(value) => changeKind(point, value)} aria-label={t("eval_point_kind_label")}>
                          {KINDS.map((value) => <Segment key={value} value={value} disabled={disabled}>{t(`eval_point_kind_${value}` as never)}</Segment>)}
                        </SegmentedControl>
                        <div className="am-ci-fld">
                          <label htmlFor={textId}>{t("eval_point_text_label")}</label>
                          <input id={textId} className="settings-input" value={point.text} maxLength={300} autoFocus onChange={(event) => update(point.id, { text: event.target.value })}
                            onKeyDown={(event) => { if (event.key === "Enter") toggleEditing(null); }} />
                        </div>
                        {kind === "required_action" ? (
                          <ToolPicker id={`${id}-${point.id}-tools`} tools={tools} selected={actionTools(point.action)} disabled={disabled}
                            onChange={(refs) => changeTools(point, refs)} />
                        ) : null}
                        {pending ? <p className="am-ci-note" data-tone="warning">{t("eval_point_kind_pending_tool")}</p> : null}
                      </div>
                    );
                  })() : <b>{point.text}</b>}
                  {parts.length ? <small className="am-ev-pt-st">{parts}</small> : null}
                  {originLine(point)}
                  {basisRows(point)}
                  {calibrationPanel(point)}
                  {verdict(point)}
                  {editing !== point.id ? reviewLine(point) : null}
                </div>
                <div className="am-ev-pt-pa">
                  {editing === point.id ? (
                    <Button size="sm" variant="ghost" onClick={() => toggleEditing(null)}>{t("eval_point_done")}</Button>
                  ) : point.state === "suggested" ? (
                    <Button size="sm" variant="secondary" disabled={disabled || !point.text.trim()} onClick={() => update(point.id, { state: "confirmed", text: point.text.trim() })}>{t("eval_point_confirm")}</Button>
                  ) : null}
                  <RowActionsMenu label={t("eval_point_menu")} actions={menu(point)} />
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      {adding ? (
        <div className="am-ev-add">
          <SegmentedControl<AgentEvalPointKind> value={draftKind} onValueChange={setDraftKind} aria-label={t("eval_point_kind_label")}>
            {KINDS.map((kind) => <Segment key={kind} value={kind} disabled={disabled}>{t(`eval_point_kind_${kind}` as never)}</Segment>)}
          </SegmentedControl>
          <p className="am-ci-note am-ev-kind-effect">
            <Badge size="sm" tone={draftKind === "required_action" ? "warning" : "neutral"}>{t(draftKind === "required_action" ? "eval_point_blocks" : "eval_point_informational")}</Badge>
            {" "}{t(draftKind === "required_action" ? "eval_point_kind_effect_blocks" : "eval_point_kind_effect_reference")}
          </p>
          <div className="am-ci-fld">
            <label htmlFor={`${id}-text`}>{t("eval_point_text_label")}</label>
            <input id={`${id}-text`} className="settings-input" value={draftText} maxLength={300} disabled={disabled} autoFocus placeholder={t("eval_point_text_placeholder")} onChange={(event) => setDraftText(event.target.value)} />
          </div>
          {draftKind === "required_action" ? (
            <ToolPicker id={`${id}-tool`} tools={tools} selected={draftTools} disabled={disabled} onChange={setDraftTools} />
          ) : null}
          <div className="am-ci-acts">
            <Button size="sm" variant="ghost" onClick={() => { setAdding(false); setDraftText(""); setDraftTools([]); }}>{t("eval_point_cancel")}</Button>
            <span className="am-ci-sp" />
            <Button size="sm" variant="primary" disabled={disabled || !draftText.trim() || (draftKind === "required_action" && !pointAction(draftTools))} onClick={addPoint}>{t("eval_point_add_submit")}</Button>
          </div>
        </div>
      ) : points.length < MAX_POINTS ? (
        <button type="button" className="am-ev-addp" disabled={disabled} onClick={() => setAdding(true)}>
          <Plus size={14} aria-hidden />{t("eval_point_add")}
        </button>
      ) : null}
      {targets.length ? (
        <div className="am-ci-point-preview" aria-live="polite">
          {!previewOpen ? (
            <Button size="sm" variant="ghost" disabled={disabled || !task.trim()} onClick={() => setPreviewOpen(true)}>{t("eval_point_preview_open")}</Button>
          ) : null}
          {previewing ? <p className="am-ci-note">{t("eval_point_preview_loading")}</p> : null}
          {previewFailed ? <p className="am-ci-note">{t("eval_point_preview_failed")}</p> : null}
          {preview && !previewing && !answer ? <p className="am-ci-note">{t("eval_point_no_live_answer")}</p> : null}
          {(previewFailed || (preview && preview.key !== currentKey)) && !previewing ? (
            <div className="am-ci-acts"><Button size="sm" variant="ghost" disabled={disabled} onClick={() => void runPreview()}>{t("eval_point_preview_again")}</Button></div>
          ) : null}
        </div>
      ) : null}
      {answer ? (
        <div className="am-ci-point-answer">
          <p className="am-ci-point-answer-label">{t("eval_point_live_answer")}</p>
          {alignment && answer.held_out ? <p className="am-ci-note">{t("eval_point_live_answer_held_out")}</p> : null}
          <p className="am-ci-point-answer-text">{answer.answer}{answer.answer_truncated ? "…" : ""}</p>
          <div className="am-ci-acts">
            <Button size="sm" variant="ghost" disabled={disabled || goodAnswer === "saving" || goodAnswer === "marked"} loading={goodAnswer === "saving"} onClick={() => void markGood()}>{t(goodAnswer === "marked" ? "eval_point_marked_good" : "eval_point_mark_good")}</Button>
            {goodAnswer === "needs_save" ? <span className="am-ci-note">{t("eval_point_mark_good_needs_save")}</span> : null}
            {goodAnswer === "failed" ? <span className="am-ci-note">{t("eval_point_mark_good_failed")}</span> : null}
            {goodAnswer === "held_out" ? <span className="am-ci-note">{t("eval_point_mark_good_held_out")}</span> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * One case of the suite (Agent Eval Points E1): the question, "Case N · M points", the held-out switch,
 * the case's ⋯ menu, then its points. The question is edited in place; new cases open in edit mode.
 */
export function EvalCaseCard({
  number,
  task,
  editingTask,
  onEditTask,
  onTaskChange,
  heldOut,
  onHeldOutChange,
  onRemove,
  disabled,
  children,
  pointCount,
}: {
  number: number;
  task: string;
  editingTask: boolean;
  onEditTask: (editing: boolean) => void;
  onTaskChange: (task: string) => void;
  heldOut: boolean;
  onHeldOutChange: (on: boolean) => void;
  onRemove: (() => void) | null;
  disabled: boolean;
  children: ReactNode;
  pointCount: number;
}) {
  const t = useTranslations("agents_marketplace");
  const id = useId();
  const alignment = useAgentEvalAlignmentCapability();
  const actions: RowAction[] = [
    ...(onRemove ? [{ id: "remove", label: t("eval_case_remove"), danger: true, confirms: false, disabled, onSelect: onRemove }] : []),
  ];
  return (
    <article className="am-ev-case" aria-labelledby={`${id}-title`} data-testid={`case-${number}`}>
      <div className="am-ev-case-h">
        <div className="am-ev-case-gn">
          {editingTask ? (
            <textarea id={`agent-eval-case-${number - 1}`} aria-label={t("eval_case_label", { number })} className="settings-input" rows={2} value={task} disabled={disabled} maxLength={4000}
              placeholder={t("eval_case_placeholder")} onChange={(event) => onTaskChange(event.target.value)} />
          ) : (
            <b id={`${id}-title`}>{task.trim() || t("eval_case_untitled")}</b>
          )}
          <small>
            <span>{t("eval_case_meta", { number, count: pointCount })}</span>
            {alignment ? <span className="am-ev-held">
              <Switch id={`agent-eval-held-${number - 1}`} checked={heldOut} disabled={disabled} aria-describedby={`${id}-held-why`}
                data-testid={`held-out-${number - 1}`} onCheckedChange={onHeldOutChange}><span /></Switch>
              <label htmlFor={`agent-eval-held-${number - 1}`}>{t("eval_held_out_label")}</label>
              <HelpPopover label={t("eval_held_out_help_label")} title={t("eval_held_out_help_title")}>
                <ul>
                  <li>{t("eval_held_out_help_coach")}</li>
                  <li>{t("eval_held_out_help_one")}</li>
                  <li>{t("eval_held_out_help_release")}</li>
                </ul>
              </HelpPopover>
            </span> : null}
          </small>
          {alignment ? <span id={`${id}-held-why`} className="am-ci-note">{t("eval_held_out_hint")}</span> : null}
        </div>
        <Button size="sm" variant="ghost" disabled={disabled} onClick={() => onEditTask(!editingTask)}>{t(editingTask ? "eval_case_done" : "eval_case_edit")}</Button>
        <RowActionsMenu label={t("eval_case_menu", { number })} actions={actions} />
      </div>
      {children}
    </article>
  );
}
