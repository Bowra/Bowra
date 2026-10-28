/**
 * Coach alignment loop and Coach's point review (alignment-loop technical design §13.1).
 *
 * Every call here is the Owner's own action through their session: Coach's Tools can't record a label,
 * confirm a point or change a held-out flag. Point and suite review are advice only.
 */
import { apiFetch, readApiError } from "./api-client";
import type { AgentEvalPointKind } from "./agents-api";

export type AlignmentLabel = "right" | "wrong" | "unclear";

export type AlignmentOutcome =
  | "confirmed"
  | "confirm"
  | "rewrite"
  | "split_or_informational"
  | "unclear"
  | "need_counterexample"
  | "recheck_row"
  | "continue";

export interface AlignmentPointOrigin {
  /** `member_feedback` carries no words or member; points saved before `kind` existed are Owner words. */
  kind?: "owner_words" | "member_feedback";
  /** May be absent; only the Owner's own words are ever shown (never a member's comment). */
  words?: string | null;
  /** The user id of whoever said the words, or a member-feedback kind with no member id. */
  by?: string | null;
  at?: string | null;
  loop_id?: string | null;
}

/**
 * What a point's "came from" line may say. The Owner's own words are quoted; anything else (a member's
 * thumbs-down, a feedback kind without a member) reads as a neutral "from member feedback" line.
 */
export function originDisplay(origin: AlignmentPointOrigin | null | undefined, currentUserId: string | null | undefined):
  | { kind: "owner"; words: string; at: string | null }
  | { kind: "member_feedback"; at: string | null }
  | null {
  // Without knowing who is reading, say nothing rather than mislabel the Owner's own words.
  if (!origin || typeof origin !== "object" || !currentUserId) return null;
  const at = typeof origin.at === "string" && origin.at ? origin.at : null;
  if (origin.kind === "member_feedback") return { kind: "member_feedback", at };
  const words = typeof origin.words === "string" ? origin.words.trim() : "";
  if (origin.by === currentUserId) return words ? { kind: "owner", words, at } : null;
  return { kind: "member_feedback", at };
}

export interface AlignmentPoint {
  point_id: string;
  kind: AgentEvalPointKind;
  text: string;
  /** The text Coach replaced; "Keep the original" rewords back to it. */
  previous_text: string | null;
  origin: AlignmentPointOrigin;
  status: "aligning" | "confirmed" | string;
  rewrites: number;
  outcome: AlignmentOutcome;
  next_step: string;
  recheck_question_ids: string[];
  labels: Record<AlignmentLabel, number>;
  case_key: string | null;
  case_revision_id: string | null;
}

export interface AlignmentQuestion {
  question_id: string;
  point_id: string;
  card_seq: number;
  row_index: number;
  answer_ref: string;
  /** A Coach-written answer (minimal-edit counterexample or generated example). */
  generated: boolean;
  question: string;
  answer_excerpt: string;
  verdict: { met: boolean | null; quote: string | null; reason: string | null };
  pick_reason: "marked_bad" | "judge_abstained" | "feedback_conflict" | "recent" | "counterexample" | "generated" | string;
  replacement: boolean;
  label: AlignmentLabel | null;
  label_via?: "row" | "rest_right" | null;
  labelled_at: string | null;
  /** Asked under wording the point no longer has: its verdict no longer counts. */
  stale?: boolean;
  /** Who shared this Turn (and gave its thumbs-down), read live; null for the Owner's own Turns. Owner view only. */
  shared_by?: string | null;
}

export interface AlignmentCount {
  met: number;
  cases: number;
  /** 0–1, only from three cases. */
  rate?: number;
}

export interface AlignmentPassing {
  passing: number;
  cases: number;
  rate?: number;
}

export interface AlignmentReport {
  /** Identifies this report; a card for an older report collapses instead of showing newer numbers. */
  report_id?: string;
  status: "measured" | "judge_changed" | "judge_unavailable" | "no_check";
  execution_id?: string;
  next_action?: string;
  target_points?: Array<{ point_id: string; text: string; before: AlignmentCount; after: AlignmentCount; independent_check: boolean }>;
  held_out?: { cases: number; before: AlignmentPassing; after: AlignmentPassing; regressions: number };
  regressions?: string[];
  to_read?: Array<{ case_key: string; excerpt: string }>;
}

export interface AlignmentLoop {
  kind: "agent_eval_alignment_loop";
  schema_version: 1;
  loop_id: string;
  agent_id: string;
  suite_id: string;
  state: "aligning" | "fixing" | "reported" | "abandoned" | string;
  /** Where the loop's words came from; for member feedback `owner_words` and `words_by` are null. */
  words_source?: "owner" | "member_feedback";
  owner_words: string | null;
  words_by: string | null;
  /** The member's comment, read live: null once the share is withdrawn, and for Owner words. */
  feedback_comment?: string | null;
  origin_turn_id: string | null;
  points: AlignmentPoint[];
  questions: AlignmentQuestion[];
  report: AlignmentReport | null;
  created_at?: string;
  updated_at?: string;
}

export interface AlignmentLabelsResult {
  outcomes: Record<string, AlignmentOutcome>;
  confirmed: Record<string, { suite_id: string; suite_revision_id: string; case_key: string; case_revision_id: string; calibration: "seeded" | "pending" }>;
  dropped_question_ids: string[];
  recheck_question_ids: string[];
  loop: AlignmentLoop;
}

export type PointReviewProblem = "vague" | "multiple" | "copied_value" | "duplicate" | "contradiction";

export interface PointReviewItem {
  id: string;
  problem: PointReviewProblem | null;
  related_point_id: string | null;
  reason: string | null;
  rewording: string | null;
  /** agent-eval-point-review@2: the two points a "multiple" point splits into. */
  split?: [string, string] | null;
}

export interface PointReview {
  status: "reviewed" | "unavailable";
  points: PointReviewItem[];
}

/** The server is still reviewing; send the same request again. */
interface PendingReview {
  status: "pending";
}

export interface SuiteReview {
  status: "reviewed" | "unavailable";
  cached?: boolean;
  gaps: Array<{ text: string; suggested_task: string | null }>;
  duplicate_cases: Array<[string, string]>;
}

export interface ReviewPointInput {
  id: string;
  kind: AgentEvalPointKind;
  text: string;
  review_dismissed_hash?: string | null;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, {
    credentials: "include",
    ...init,
    headers: { ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
  });
  if (!response.ok) throw await readApiError(response);
  return response.json() as Promise<T>;
}

const loopPath = (agentId: string, loopId: string) =>
  `/api/v1/agents/${encodeURIComponent(agentId)}/eval-alignments/${encodeURIComponent(loopId)}`;

export function getAgentEvalAlignment(agentId: string, loopId: string, signal?: AbortSignal) {
  return json<AlignmentLoop>(loopPath(agentId, loopId), { cache: "no-store", signal });
}

/** One request per card: the rows the Owner marked, plus `rest_right` for "The rest are right". */
export function labelAgentEvalAlignment(
  agentId: string,
  loopId: string,
  value: { labels: Array<{ question_id: string; label: AlignmentLabel }>; rest_right: boolean; card_seq: number },
) {
  return json<AlignmentLabelsResult>(`${loopPath(agentId, loopId)}/labels`, { method: "POST", body: JSON.stringify(value) });
}

/** Reword a point on its card; rewording back to `previous_text` is "Keep the original". */
export function rewordAgentEvalAlignmentPoint(agentId: string, loopId: string, pointId: string, text: string) {
  return json<AlignmentLoop>(`${loopPath(agentId, loopId)}/points/${encodeURIComponent(pointId)}`, {
    method: "PATCH",
    body: JSON.stringify({ text }),
  });
}

/** Re-pin the loop to the current evaluation model; a new check measures again. */
export function repinAgentEvalAlignment(agentId: string, loopId: string) {
  return json<AlignmentLoop & { next_action: "request_check" }>(`${loopPath(agentId, loopId)}/repin`, { method: "POST" });
}

export interface PointCalibrationExample {
  id: string;
  kind: "good" | "bad" | "judged_wrongly";
  label_source: "marked_good" | "coach_edit" | "judged_wrongly" | "owner_alignment" | null;
  answer_kind: "turn" | "attempt" | "coach" | string;
  question: string | null;
  expected_met: boolean;
  graded_met: boolean | null;
  /** Graded under the point's current text, prompt and evaluation model. */
  graded_current: boolean;
  created_at: string;
}

export interface PointCalibration {
  kind: "agent_eval_point_calibration";
  suite_id: string;
  point_id: string;
  status: { counts: boolean; reason: string | null; rewording: string | null };
  calibration: {
    state: "calibrated" | "failed";
    failure_reason: string | null;
    rewording: string | null;
    current_text: boolean;
    judge: { prompt_version: string; model_fingerprint: string };
    updated_at: string | null;
  } | null;
  judge: { prompt_version: string; model_fingerprint: string | null };
  /** The Team evaluation model as team model settings name it; null when unknown. */
  judge_model_name?: string | null;
  examples: PointCalibrationExample[];
}

/** 「查看校准」: a confirmed point's examples, judge and calibration status (Owner only). */
export function getAgentEvalPointCalibration(agentId: string, suiteId: string, pointId: string, signal?: AbortSignal) {
  return json<PointCalibration>(
    `/api/v1/agents/${encodeURIComponent(agentId)}/eval-suites/${encodeURIComponent(suiteId)}/points/${encodeURIComponent(pointId)}/calibration`,
    { signal },
  );
}

/** How often a pending review is asked for again, and for how long (the server's review deadline is 120 s). */
export const REVIEW_POLL_INTERVAL_MS = 2_000;
const REVIEW_POLL_LIMIT_MS = 130_000;

function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Sends the same review request until the server stops answering `pending`. A reasoning model can
 * take well over 30 s, so the server reviews in the background instead of holding the request open.
 */
async function pollReview<T extends { status: string }>(send: () => Promise<T>, signal?: AbortSignal): Promise<T | null> {
  const started = Date.now();
  for (;;) {
    const result = await send();
    if (result.status !== "pending") return result;
    if (Date.now() - started > REVIEW_POLL_LIMIT_MS) return null;
    await pause(REVIEW_POLL_INTERVAL_MS, signal);
  }
}

/** Coach's review of point text. Advice only: any failure reads as "nothing to say". */
export async function reviewAgentEvalPoints(
  agentId: string,
  value: { task: string; points: ReviewPointInput[]; other_points?: ReviewPointInput[] },
  signal?: AbortSignal,
): Promise<PointReview> {
  try {
    const result = await pollReview(
      () =>
        json<PointReview | PendingReview>(`/api/v1/agents/${encodeURIComponent(agentId)}/eval-points/review`, {
          method: "POST",
          body: JSON.stringify({ ...value, other_points: value.other_points ?? [] }),
          signal,
        }),
      signal,
    );
    return result?.status === "reviewed" && Array.isArray(result.points) ? result : { status: "unavailable", points: [] };
  } catch (cause) {
    if (signal?.aborted) throw cause;
    return { status: "unavailable", points: [] };
  }
}

/** Coverage gaps and duplicate cases for one suite revision. Advice only, cached per revision on the server. */
export async function reviewAgentEvalSuite(agentId: string, suiteId: string, suiteRevisionId: string, signal?: AbortSignal): Promise<SuiteReview> {
  try {
    const result = await pollReview(
      () =>
        json<SuiteReview | PendingReview>(
          `/api/v1/agents/${encodeURIComponent(agentId)}/eval-suites/${encodeURIComponent(suiteId)}/review`,
          { method: "POST", body: JSON.stringify({ suite_revision_id: suiteRevisionId }), signal },
        ),
      signal,
    );
    if (result?.status !== "reviewed") return { status: "unavailable", gaps: [], duplicate_cases: [] };
    return { ...result, gaps: Array.isArray(result.gaps) ? result.gaps : [], duplicate_cases: Array.isArray(result.duplicate_cases) ? result.duplicate_cases : [] };
  } catch (cause) {
    if (signal?.aborted) throw cause;
    return { status: "unavailable", gaps: [], duplicate_cases: [] };
  }
}

/** The server's point text hash (`sha256:` + hex of the UTF-8 text), stored as `review_dismissed_hash`. */
export async function pointTextHash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return `sha256:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

/** The rows of one question card, in order. */
export function cardRows(loop: AlignmentLoop, pointId: string, cardSeq: number): AlignmentQuestion[] {
  return loop.questions
    .filter((question) => question.point_id === pointId && question.card_seq === cardSeq)
    .sort((a, b) => a.row_index - b.row_index);
}

/** The newest card of the loop; older cards are settled history. */
export function latestCardSeq(loop: AlignmentLoop): number {
  return loop.questions.reduce((max, question) => Math.max(max, question.card_seq), 0);
}

/**
 * The labels body for a card: every row the Owner marked, then "The rest are right" for the untouched rows.
 * A row already labelled with the same answer is left out; the server is idempotent per question either way.
 */
export function cardLabelsBody(
  rows: AlignmentQuestion[],
  marks: Record<string, AlignmentLabel | null | undefined>,
  cardSeq: number,
): { labels: Array<{ question_id: string; label: AlignmentLabel }>; rest_right: boolean; card_seq: number } {
  const labels = rows
    .filter((row) => marks[row.question_id] && !(row.label === marks[row.question_id] && row.label_via === "row"))
    .map((row) => ({ question_id: row.question_id, label: marks[row.question_id] as AlignmentLabel }));
  return { labels, rest_right: true, card_seq: cardSeq };
}
