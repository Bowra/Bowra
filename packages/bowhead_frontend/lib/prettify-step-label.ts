/**
 * prettify-step-label — coerce raw backend `input_preview` payloads into a
 * mono one-line label suitable for the Step Row. NEVER returns raw JSON.
 *
 * Phase 1.7-A (chat-redesign-2026-05-21): replaces the JSON-tolerant
 * `prettifyInputPreview` for the new beats-driven Step List Card surface.
 * The old helper stays for non-step callers (1.7-E will sweep).
 *
 * Behavior (tech-design §2.3 table):
 *   - null / undefined           → ""
 *   - string                     → first 60 chars, trimmed
 *   - obj.results: [...]         → "查询返回 N 行" (bi_query result-ish)
 *   - obj.columns + obj.rows     → "col1 · col2 · col3" (truncate at 3)
 *   - obj.ok=true, exit_code=0   → "运行完成" (python_sandbox)
 *   - obj.ok=false / exit_code≠0 → "运行出错" (python_sandbox)
 *   - obj.query: "..."           → "搜索: {query[:40]}" (kb_search / web_search)
 *   - object, unknown keys       → "" (NEVER raw JSON)
 *   - array                      → "{n} 项"
 *   - number / boolean           → String(value)
 */

import { staticTranslator } from "./locale-messages";

const STRING_MAX = 60;
const QUERY_MAX = 40;

/**
 * chat-ux-2026-08 batch A—unrecognized values now return empty string instead of `"[no-preview]"`.
 *
 * Old sentinel was developer jargon directly displayed via `step.label` (Chinese UI showing
 * `[no-preview]` is worse than no label). After fixing the truncation gap above, previews formerly leaking as raw JSON
 * all reach this branch—frequency only increases, so fix together.
 *
 * Empty string = no readable argument summary for this step, equivalent to `input_preview == null`;
 * renderer shows only tool name (Step.tsx label-empty check).
 */
const NO_LABEL = "";

// This helper runs outside the React tree (turn-store-dispatch), so no
// useTranslations — phrases come from messages `step_label_phrase` via staticTranslator.
interface LabelPhrases {
  items: (n: number) => string;
  query_rows: (n: number) => string;
  run_done: string;
  run_error: string;
  search: (q: string) => string;
}
function _phrases(): LabelPhrases {
  const t = staticTranslator("step_label_phrase");
  return {
    items: (n) => t("items", { n }),
    query_rows: (n) => t("query_rows", { n }),
    run_done: t("run_done"),
    run_error: t("run_error"),
    search: (q) => t("search", { q }),
  };
}

export function prettifyStepLabel(
  input_preview: unknown,
  tool_name?: string | null,
): string {
  if (input_preview === null || input_preview === undefined) return "";

  if (typeof input_preview === "string") {
    const t = input_preview.trim();
    // Defensive (chat-solidify follow-up 2026-07): a backend preview that is
    // itself raw JSON must NEVER render as a step label — backstops any future
    // tool whose `_prettify_input_preview` falls through to a json.dumps blob
    // (#4/#6 were live instances). Parse-gate rather than a bare startsWith so
    // human labels that merely begin with a bracket (subagent_spawn
    // "[compute] task") are preserved — those are not valid JSON.
    //
    // chat-ux-2026-08 batch A—close parse-gate gap: **truncated JSON cannot parse**,
    // leaking through catch as an ordinary string. Observed source on 116:
    //   `{"payload": {"delivery_surface": "chat", "exec_summary": "近3`
    // (backend cuts preview midway → JSON.parse throws → displayed verbatim).
    // Split criterion: **no human labels start with `{`**; suppress unconditionally, regardless of parsing;
    // `[` still uses parse gate, preserving actual labels such as "[compute] task".
    if (t.startsWith("{")) return NO_LABEL;
    if (t.startsWith("[")) {
      try {
        const parsed = JSON.parse(t);
        if (parsed !== null && typeof parsed === "object") return NO_LABEL;
      } catch {
        /* not valid JSON — a normal label like "[compute] task"; keep it */
      }
    }
    return t.slice(0, STRING_MAX);
  }

  if (typeof input_preview === "number" || typeof input_preview === "boolean") {
    return String(input_preview);
  }

  const ph = _phrases();

  if (Array.isArray(input_preview)) {
    return ph.items(input_preview.length);
  }

  if (typeof input_preview === "object") {
    const obj = input_preview as Record<string, unknown>;

    // bi_query result-ish: { results: [...] }
    if (Array.isArray(obj.results)) {
      return ph.query_rows(obj.results.length);
    }

    // python_sandbox result-ish: { ok: bool, exit_code: number }
    const hasOk = typeof obj.ok === "boolean";
    const hasExitCode = typeof obj.exit_code === "number";
    if (hasOk || hasExitCode) {
      const okFlag = hasOk ? obj.ok === true : obj.exit_code === 0;
      const exitOk = hasExitCode ? obj.exit_code === 0 : true;
      const passed = okFlag && exitOk;
      return passed ? ph.run_done : ph.run_error;
    }

    // table-ish: { columns: [...], rows: [...] }
    if (Array.isArray(obj.columns) && Array.isArray(obj.rows)) {
      const cols = (obj.columns as unknown[])
        .slice(0, 3)
        .map((c) => (typeof c === "string" ? c : JSON.stringify(c)));
      return cols.join(" · ");
    }

    // search-ish: { query: "..." } (kb_search / web_search)
    if (typeof obj.query === "string") {
      const q = obj.query.trim().slice(0, QUERY_MAX);
      return ph.search(q);
    }

    // No recognized keys — NEVER return raw JSON.
    return NO_LABEL;
  }

  // Defensive: any unexpected primitive type — coerce to string.
  void tool_name; // explicit no-op: tool_name reserved for future routing
  return NO_LABEL;
}
