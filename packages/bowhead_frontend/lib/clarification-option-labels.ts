/**
 * Clarification options: display labels and canonical returned values.
 *
 * Observed: an English user asked a question and Lead clarified in English, but options were datarc's canonical Chinese names
 * `["一级品牌名称英文", "品规", "产品组", "市场分类一级"]`, leaving people who cannot read Chinese
 * facing four Chinese buttons.
 *
 * ## Why options cannot simply be translated
 *
 * Option strings serve **two roles**: visible text, and **query identifiers** returned unchanged to the model
 * to insert into `bi_query`. datarc recognizes only canonical Chinese names, so translated strings cannot be queried.
 * Backend therefore sends `option_labels` alongside; separate the roles here: **render label, submit value**,
 * reusing `tool_context_select`'s existing `valueByLabel` seam.
 *
 * ## The only tightening
 *
 * `valueByLabel` is **one flat table for the whole card**; duplicate labels obscure which value to return.
 * Relabel only when **labels are globally unique across the card**; duplicates make the whole card render canonical names.
 * Prefer showing English users Chinese to returning B's value for A's selection.
 */

import type { ThetaClarificationQuestion } from "@/lib/types";

/** Questions with usable `option_labels` (present, same length); leave the others unchanged. */
function usableLabels(q: ThetaClarificationQuestion): string[] | null {
  const labels = q.option_labels;
  if (!labels || labels.length !== q.options.length) return null;
  return labels;
}

/** Display labels are unique across the card **and** do not collide with other options' canonical values.
 *
 * The latter constraint matters: resolve retains label fallback matching for old frontends
 * (old FE ignores value/label separation and returns labels). A display label equal to **another option's** canonical name
 * makes the fallback choose the wrong option. Prefer falling back to Chinese for the whole card to silently misrouting selections.
 */
function labelsAreGloballyUnique(questions: ThetaClarificationQuestion[]): boolean {
  const seen = new Set<string>();
  const allValues = new Set<string>();
  for (const q of questions) for (const v of q.options) allValues.add(v);
  for (const q of questions) {
    const labels = usableLabels(q) ?? q.options;
    for (const [i, l] of labels.entries()) {
      if (seen.has(l)) return false;
      // Relabeled options must not use another option's canonical value as their label (their own original value is not a collision).
      if (l !== q.options[i] && allValues.has(l)) return false;
      seen.add(l);
    }
  }
  return true;
}

/** Render usable display labels; otherwise return the same array reference unchanged (memo-friendly). */
export function relabelQuestions(questions: ThetaClarificationQuestion[]): ThetaClarificationQuestion[] {
  if (!questions.length || !labelsAreGloballyUnique(questions)) return questions;
  let changed = false;
  const out = questions.map((q) => {
    const labels = usableLabels(q);
    if (!labels) return q;
    changed = true;
    return { ...q, options: labels };
  });
  return changed ? out : questions;
}

/** Display label → canonical value. Return null when nothing is relabeled (callers submit exactly what is displayed). */
export function buildOptionValueByLabel(
  questions: ThetaClarificationQuestion[] | undefined,
): Record<string, string> | null {
  if (!questions?.length || !labelsAreGloballyUnique(questions)) return null;
  const map: Record<string, string> = {};
  for (const q of questions) {
    const labels = usableLabels(q);
    if (!labels) continue;
    labels.forEach((label, i) => {
      const value = q.options[i];
      if (value !== undefined && label !== value) map[label] = value;
    });
  }
  return Object.keys(map).length ? map : null;
}
