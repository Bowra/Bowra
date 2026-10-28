/**
 * Pretty-print backend `input_preview` JSON strings into human-readable
 * step row labels. Backend emits `input_preview` as a raw JSON-serialised
 * arguments blob (e.g. `{"queries":["营业额"],"limit":50}`) which is
 * faithful but ugly inline. Phase 1.4 FE-side fallback — backend
 * `feedback_bi_sprint_discovery_gates` / Phase 1.4 cleanup will move the
 * prettify upstream so all surfaces (FE + traces + admin) see the same
 * cleaned shape.
 *
 * The function takes the raw string. If it parses to an object with a
 * known field, the field value is returned (with reasonable formatting).
 * Otherwise the raw string is returned unchanged so we never drop the
 * user-visible information.
 */

const MAX_LEN = 120;

export function prettifyInputPreview(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined;
  const trimmed = raw.trim();
  if (!trimmed) return undefined;

  // Not JSON → return as-is (already a string label from backend).
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) {
    return truncate(trimmed);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    // Phase 1.7 dogfood patch: backend `skill_invoke` arguments_preview
    // can contain raw newlines/control chars inside string values (invalid
    // JSON). When JSON.parse fails, try regex extraction of known shape
    // hints before falling back to raw truncation — keeps step labels
    // human-readable instead of dumping 120-char JSON blob.
    const regexLabel = extractLabelByRegex(trimmed);
    if (regexLabel) return regexLabel;
    return truncate(trimmed);
  }

  if (parsed && typeof parsed === "object") {
    const obj = parsed as Record<string, unknown>;

    // bi_query: { queries: [...], limit?: N }
    if (Array.isArray(obj.queries) && obj.queries.length > 0) {
      const first = obj.queries[0];
      const rest = obj.queries.length - 1;
      const head = typeof first === "string" ? first : JSON.stringify(first);
      const suffix = rest > 0 ? ` +${rest} more` : "";
      return truncate(`${head}${suffix}`);
    }

    // bi_query single query: { query: "..." } or web_search: { q: "..." }
    if (typeof obj.query === "string") return truncate(obj.query);
    if (typeof obj.q === "string") return truncate(obj.q);

    // kb_search: { text: "..." }
    if (typeof obj.text === "string") return truncate(obj.text);

    // subagent_spawn: { profile: "compute", task: "..." }
    if (typeof obj.task === "string") {
      const prof = typeof obj.profile === "string" ? `[${obj.profile}] ` : "";
      return truncate(`${prof}${obj.task}`);
    }

    // python_sandbox: { name: "...", script: "..." }
    if (typeof obj.name === "string" && obj.script !== undefined) {
      return truncate(obj.name);
    }

    // parse_file: { filename: "..." } or { file_id: "..." }
    if (typeof obj.filename === "string") return truncate(obj.filename);
    if (typeof obj.file_id === "string") return truncate(obj.file_id);

    // Generic — first string-valued field
    for (const key of Object.keys(obj)) {
      const v = obj[key];
      if (typeof v === "string" && v.length > 0) return truncate(v);
    }
  }

  if (Array.isArray(parsed) && parsed.length > 0) {
    const first = parsed[0];
    return truncate(typeof first === "string" ? first : JSON.stringify(first));
  }

  return truncate(trimmed);
}

function truncate(s: string): string {
  if (s.length <= MAX_LEN) return s;
  return s.slice(0, MAX_LEN - 1) + "…";
}

/**
 * Phase 1.7 dogfood fallback: when JSON.parse fails on a string that LOOKS
 * like JSON (starts with `{`), try regex extraction of the most common
 * shape hints (skill_invoke / python_sandbox / canvas payload / etc.).
 * Returns a humanized label or null if no pattern matches.
 *
 * Triggered by backend bug: arguments_preview can contain raw newlines or
 * control chars inside string values, breaking strict JSON parse.
 */
function extractLabelByRegex(raw: string): string | null {
  // Don't require trailing `"` after capture group: backend invalid-JSON
  // can have raw newlines inside string values, so the closing quote may
  // be far away or absent. Stop capture at first newline OR closing quote.
  // Pattern: `"key"\s*:\s*"([^"\n]+?)(?=["\n])`  (lookahead for boundary)

  // skill_invoke / python_sandbox: {"name": "python_sandbox", ...}
  const nameMatch = raw.match(/"name"\s*:\s*"([^"\n]+?)(?=["\n])/);
  if (nameMatch && nameMatch[1]) {
    // If we also see a "script" or "skill" key, it's confirmed skill_invoke shape
    if (/"(?:script|skill|exec_kind|profile)"\s*:/.test(raw)) {
      return truncate(nameMatch[1]);
    }
  }
  // canvas section render: {"payload": {"title": "...", ...}}
  const titleMatch = raw.match(/"title"\s*:\s*"([^"\n]+?)(?=["\n])/);
  if (titleMatch && titleMatch[1] && /"payload"\s*:\s*\{/.test(raw)) {
    return truncate(titleMatch[1]);
  }
  // exec_summary fallback (LLM-thinking step writing analysis)
  const summaryMatch = raw.match(/"exec_summary"\s*:\s*"([^"\n]+?)(?=["\n])/);
  if (summaryMatch && summaryMatch[1]) {
    return truncate(summaryMatch[1]);
  }
  // Generic first quoted string value (any "key": "value" pattern)
  const firstStringValue = raw.match(/"[a-zA-Z_]+"\s*:\s*"([^"\n]{3,}?)(?=["\n])/);
  if (firstStringValue && firstStringValue[1]) {
    return truncate(firstStringValue[1]);
  }
  return null;
}
