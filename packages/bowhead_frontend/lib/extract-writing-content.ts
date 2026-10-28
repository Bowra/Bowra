/**
 * extract-writing-content — project the accumulated tool-args JSON of a live
 * `lead_thinking_writing` step down to the human-facing content being written
 * (the code / SQL / query), dropping the surrounding JSON scaffold + sibling
 * fields.
 *
 * chat-solidify follow-up (2026-07, #5): the Lead streams a tool call's args as
 * `partial_args_json` DELTAS (subagent.py:2680) which the turn-store appends
 * verbatim into `step.partialContent`. The accumulated value is the whole args
 * object, e.g. `{"code": "<code>", "inputs": {"data": "art_1"}}` — so the <Code>
 * block rendered the code with a trailing `,"inputs":{…}` tail (dogfood 0.72.8).
 * We can't fix it at the backend emit site because each streamed chunk is a
 * fragment (no full buffer there); the FE holds the accumulated buffer, so we
 * extract the target field HERE, tolerant of the still-streaming (incomplete
 * JSON) case so live typing is preserved.
 */

/** Target string fields per tool, in priority order. */
function targetKeys(toolName: string | null | undefined): string[] {
  if (toolName === "python_sandbox") return ["code", "script"];
  // bi_query / kb_search / web_search — user is watching the query/SQL type.
  return ["query", "queries", "code", "script"];
}

/**
 * Tolerant extraction of the string value for `key` from a possibly-incomplete
 * JSON string. Returns the decoded value, or null if the key isn't present or
 * its value isn't a (started) string. Uses JSON.parse for escape decoding by
 * reconstructing a valid string literal — including the streaming case where
 * the closing quote hasn't arrived yet.
 */
function findStringValue(raw: string, key: string): string | null {
  const marker = `"${key}"`;
  const ki = raw.indexOf(marker);
  if (ki < 0) return null;
  let i = ki + marker.length;
  // Between the key and its value there may only be whitespace then a colon.
  while (i < raw.length && raw[i] !== ":") {
    if (!/\s/.test(raw[i]!)) return null;
    i++;
  }
  if (raw[i] !== ":") return null; // colon not streamed yet, or wrong shape
  i++;
  while (i < raw.length && /\s/.test(raw[i]!)) i++;
  if (i >= raw.length) return ""; // value hasn't started — show nothing yet
  if (raw[i] !== '"') return null; // not a string value (array/number) — caller falls through
  const start = i; // index of the opening quote
  i++;
  let closed = false;
  while (i < raw.length) {
    const c = raw[i];
    if (c === "\\") {
      i += 2; // skip the escaped char (\uXXXX's hex tail is scanned harmlessly)
      continue;
    }
    if (c === '"') {
      closed = true;
      break;
    }
    i++;
  }
  let literal: string;
  if (closed) {
    literal = raw.slice(start, i + 1); // complete "..." literal
  } else {
    // Unterminated (still streaming). Drop a dangling half-escape then close.
    let tail = raw.slice(start);
    const trailingSlashes = tail.match(/\\+$/);
    if (trailingSlashes && trailingSlashes[0].length % 2 === 1) {
      tail = tail.slice(0, -1);
    }
    literal = tail + '"';
  }
  try {
    const v = JSON.parse(literal);
    return typeof v === "string" ? v : null;
  } catch {
    return null;
  }
}

/**
 * Extract the code/query being written from the accumulated raw tool-args JSON.
 * Falls back to the raw string if nothing recognizable can be extracted (never
 * worse than the pre-fix behavior).
 */
export function extractWritingContent(
  raw: string,
  toolName?: string | null,
): string {
  if (!raw) return raw;
  const keys = targetKeys(toolName);
  // Fast path: complete JSON → clean extraction (drops sibling fields such as
  // python_sandbox's trailing `inputs`).
  try {
    const obj = JSON.parse(raw);
    if (obj !== null && typeof obj === "object" && !Array.isArray(obj)) {
      for (const k of keys) {
        const v = (obj as Record<string, unknown>)[k];
        if (typeof v === "string" && v.length > 0) return v;
        if (Array.isArray(v) && v.length > 0) {
          const strs = v.filter((x): x is string => typeof x === "string");
          if (strs.length > 0) return strs.join("\n");
        }
      }
    }
  } catch {
    /* still streaming — incomplete JSON; tolerant scan below */
  }
  // Streaming path: tolerant single-string extraction of the first target key.
  for (const k of keys) {
    const val = findStringValue(raw, k);
    if (val !== null) return val;
  }
  // Give up — show raw (no worse than before this fix).
  return raw;
}
