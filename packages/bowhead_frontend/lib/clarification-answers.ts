/**
 * Clarification answers → returned values: the **single projection point** (2026-09-04 HITL audit §4.1).
 *
 * Submitting a card produces two things: structured `answers` (read by backend `_project_answer_value`)
 * and legacy flat `chosen_values` strings. This projection previously lived only inside `ClarificationCard`,
 * leaving callers no way to **replace display labels with canonical values** before submission: single-select
 * could replace them in its handler (`TurnContainer` looks up `valueByLabel`), but multi-select had to send
 * the card's assembled strings unchanged. Once `option_labels` was wired, multi-select returned translated labels as canonical model values.
 *
 * Moving the projection here shares it between both paths: the card still assembles `values`, while callers
 * needing replacements use `canonicalizeAnswers` then reproject with the same function, avoiding drift from the card.
 */

import type { ClarificationAnswer } from "@/lib/types";

/** Sentinel string for skipping one question. The backend counterpart is in `routes/clarifications.py:61`; any change must update both. */
export const QUESTION_SKIP_VALUE = "__bowhead_question_skip__";

/**
 * Replace display labels with canonical values.
 *
 * Replace only answers with `source === "candidate"`: `custom` is user-entered free text,
 * and looking it up in the replacement map would alter the user's words; `skip` has no answer field.
 * When `valueByLabel` is empty (no replacements), return the same reference so callers can treat it as what you see is what you get.
 */
export function canonicalizeAnswers(
  answers: ClarificationAnswer[],
  valueByLabel: Record<string, string> | null | undefined,
): ClarificationAnswer[] {
  if (!valueByLabel) return answers;
  let changed = false;
  const out = answers.map((answer) => {
    if (answer.source !== "candidate") return answer;
    const canonical = valueByLabel[answer.answer];
    if (canonical === undefined || canonical === answer.answer) return answer;
    changed = true;
    return { ...answer, answer: canonical };
  });
  return changed ? out : answers;
}

/**
 * Legacy `chosen_values` projection: `"{question}: {answer}"` (multiple questions) or the bare answer (one question).
 *
 * Skipped questions always include the question prefix; backend `_split_legacy_question_value` needs it
 * to split the sentinel back into `(question, skip)`.
 */
export function projectAnswerValues(
  answers: ClarificationAnswer[],
  { multiQuestion }: { multiQuestion: boolean },
): string[] {
  return answers.map((answer) => {
    const projected =
      answer.source === "skip" ? QUESTION_SKIP_VALUE : answer.answer;
    return multiQuestion || answer.source === "skip"
      ? `${answer.question}: ${projected}`
      : projected;
  });
}
