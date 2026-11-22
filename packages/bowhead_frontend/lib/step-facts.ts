/**
 * step-facts — word a step event's `facts` in the current interface language (turn visibility FR3/FR4).
 *
 * The backend (`bowhead_core/step_facts.py`) sends facts instead of sentences, so a step row never shows
 * a MIME type, byte count, enum code or parser message, and reads in Chinese, English, German or French.
 * Events without facts (earlier Turns) keep their stored `input_preview` / `output_preview` text: every
 * function here returns null for anything it does not recognise, and the caller falls back.
 *
 * Runs outside the React tree (turn-store-dispatch), so phrases come from `staticTranslator`.
 */
import { getCurrentLocale } from "./locale";
import { staticTranslator } from "./locale-messages";

export type StepFacts =
  | { v: 1; kind: "attachment_read"; filename?: string; size_bytes?: number; chars_read?: number; truncated?: boolean }
  | { v: 1; kind: "canvas_added"; section_kind: string }
  | { v: 1; kind: "canvas_not_added"; reason: string }
  | { v: 1; kind: "code_outputs"; count: number; types: string[] }
  | { v: 1; kind: "result_lookup"; result_kind?: string; name?: string }
  | { v: 1; kind: "arguments_retry" };

const KINDS = new Set<StepFacts["kind"]>([
  "attachment_read",
  "canvas_added",
  "canvas_not_added",
  "code_outputs",
  "result_lookup",
  "arguments_retry",
]);

const isText = (value: unknown): value is string => typeof value === "string" && value.trim().length > 0;
const isCount = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;

/** A facts object this version understands, or null (unknown versions and kinds fall back to stored text). */
export function readStepFacts(value: unknown): StepFacts | null {
  if (!value || typeof value !== "object") return null;
  const facts = value as Record<string, unknown>;
  if (facts.v !== 1 || !KINDS.has(facts.kind as StepFacts["kind"])) return null;
  switch (facts.kind) {
    case "canvas_added":
      return isText(facts.section_kind) ? (facts as StepFacts) : null;
    case "canvas_not_added":
      return isText(facts.reason) ? (facts as StepFacts) : null;
    case "code_outputs":
      return isCount(facts.count) && Array.isArray(facts.types) && facts.types.every(isText) ? (facts as StepFacts) : null;
    default:
      return facts as StepFacts;
  }
}

function nameOf(namespace: string, key: string): string | null {
  const t = staticTranslator(namespace);
  return t.hasMessage(key) ? t(key) : null;
}

/** A Canvas section or chart kind by its interface name; the code itself when no name exists, as before. */
function sectionName(kind: string): string {
  return nameOf("step_facts.section", kind) ?? nameOf("chart_block.kind", kind) ?? kind;
}

function formatCount(n: number): string {
  return new Intl.NumberFormat(getCurrentLocale()).format(n);
}

/** Header replacing the stored one (the attachment row's "解析 x.csv" is Chinese in every language). */
export function stepFactsHeader(facts: StepFacts | null): string | null {
  if (facts?.kind === "attachment_read") return staticTranslator("step_facts")("attachment_header");
  return null;
}

/** Label for a step that has started. */
export function stepFactsLabel(facts: StepFacts | null): string | null {
  if (!facts) return null;
  const t = staticTranslator("step_facts");
  switch (facts.kind) {
    case "attachment_read":
      return isText(facts.filename) ? facts.filename : null;
    case "result_lookup":
      if (isText(facts.name)) return facts.name;
      return facts.result_kind ? (nameOf("step_facts.result_kind", facts.result_kind) ?? "") : "";
    case "arguments_retry":
      return t("arguments_retry");
    default:
      return null;
  }
}

/** Summary for a step that has finished. */
export function stepFactsSummary(facts: StepFacts | null): string | null {
  if (!facts) return null;
  const t = staticTranslator("step_facts");
  switch (facts.kind) {
    case "attachment_read":
      if (!isCount(facts.chars_read)) return null;
      return facts.truncated ? t("attachment_truncated", { n: formatCount(facts.chars_read) }) : t("attachment_complete");
    case "canvas_added":
      return t("canvas_added", { name: sectionName(facts.section_kind) });
    case "canvas_not_added":
      return t("canvas_not_added", { reason: nameOf("step_facts.not_added_reason", facts.reason) ?? facts.reason });
    case "code_outputs":
      if (facts.count === 0) return t("code_done");
      if (facts.types.length === 0) return t("code_outputs", { count: facts.count });
      return t("code_outputs_typed", {
        count: facts.count,
        types: facts.types.map((type) => type.toUpperCase()).join(t("type_separator")),
      });
    default:
      return null;
  }
}
