import type { ThetaClarificationQuestion } from "./types";

/**
 * Parse a raw SSE/mock `questions` array into validated
 * `ThetaClarificationQuestion[]`. Drops malformed entries (non-object,
 * blank question) and trims values. Empty options are valid so users can
 * answer with free text or skip; returns `undefined` when nothing valid
 * remains so callers can fall back to the legacy `candidates` shape.
 *
 * Shared by `use-turn-runner` (SSE + mock dispatch) and `turn-store-dispatch`
 * — keep the single definition here so the two paths can't drift.
 *
 * ⚠ This constructs **new objects** with a field whitelist. Omitted new backend question fields effectively
 * never arrived: `option_labels` (2026-09-01 display-label/canonical-value separation) previously made the whole path
 * vacuous this way. Pure-function tests fed `option_labels` directly to `relabelQuestions`, bypassing
 * this layer; all tests passed while production never relabeled anything (2026-09-04 HITL audit §4).
 */
export function parseThetaQuestions(
  raw: unknown,
): ThetaClarificationQuestion[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const questions = raw.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    if (typeof record.question !== "string" || !record.question.trim()) {
      return [];
    }
    const options = Array.isArray(record.options)
      ? record.options
        .filter((v): v is string => typeof v === "string" && v.trim().length > 0)
        .map((v) => v.trim())
      : [];
    // Display labels matter only when **positionally aligned** with options (downstream pairs by index).
    // If the filter above dropped any option, indices are already misaligned; also require that options
    // were never filtered. If either requirement fails, discard all labels and render options instead: canonical names
    // are preferable to mispaired labels.
    const optionsIntact = Array.isArray(record.options)
      && record.options.length === options.length;
    const rawLabels = record.option_labels;
    const labels = optionsIntact
      && Array.isArray(rawLabels)
      && rawLabels.length === options.length
      && rawLabels.every((v) => typeof v === "string" && v.trim().length > 0)
      ? (rawLabels as string[]).map((v) => v.trim())
      : undefined;
    return [{
      question: record.question.trim(),
      options,
      type: typeof record.type === "string" ? record.type : "single_select",
      ...(labels ? { option_labels: labels } : {}),
    }];
  });
  return questions.length > 0 ? questions : undefined;
}
