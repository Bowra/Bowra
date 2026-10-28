/**
 * Sidebar search result text (Header, Language & Paging spec 4b): the matched term is marked, and a hit inside the
 * user's own message shows about 12 characters on each side of the term.
 */

export interface MatchPart {
  text: string;
  match: boolean;
}

/** Split `text` into plain and matched parts (every case-insensitive occurrence of `query`). */
export function splitMatches(text: string, query: string): MatchPart[] {
  const needle = query.trim();
  if (!needle || !text) return text ? [{ text, match: false }] : [];
  const haystack = text.toLowerCase();
  const lowered = needle.toLowerCase();
  // Case folding that changes length (rare scripts) would misalign the offsets: show the text unmarked.
  if (haystack.length !== text.length || lowered.length !== needle.length) {
    return [{ text, match: false }];
  }
  const parts: MatchPart[] = [];
  let cursor = 0;
  for (;;) {
    const index = haystack.indexOf(lowered, cursor);
    if (index < 0) break;
    if (index > cursor)
      parts.push({ text: text.slice(cursor, index), match: false });
    parts.push({ text: text.slice(index, index + needle.length), match: true });
    cursor = index + needle.length;
  }
  if (cursor < text.length)
    parts.push({ text: text.slice(cursor), match: false });
  return parts;
}

/** Display width of one character: 2 for East Asian wide and fullwidth characters, 1 otherwise. */
export function displayWidth(character: string): number {
  return /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]|[\u{20000}-\u{3FFFD}]/u.test(
    character,
  )
    ? 2
    : 1;
}

/**
 * A one-line excerpt that keeps the matched term near the start, so a narrow sidebar row (248 desktop, 390 phone)
 * still shows it before the ellipsis: up to `beforeWidth` display columns of lead-in (about 5 CJK characters) and
 * up to `afterWidth` columns after the term (about 12 CJK characters). Wide characters count as 2 columns.
 */
export function contentExcerpt(
  snippet: string,
  query: string,
  beforeWidth = 10,
  afterWidth = 24,
): string {
  const text = snippet.replace(/\s+/g, " ").trim();
  const needle = query.trim();
  const index = needle ? text.toLowerCase().indexOf(needle.toLowerCase()) : -1;
  if (index < 0) return text;
  const characters = Array.from(text);
  // Code-point offsets so a cut never splits a surrogate pair.
  const termStart = Array.from(text.slice(0, index)).length;
  const termEnd =
    termStart + Array.from(text.slice(index, index + needle.length)).length;
  let start = termStart;
  for (let used = 0; start > 0; start -= 1) {
    const width = displayWidth(characters[start - 1] ?? "");
    if (used + width > beforeWidth) break;
    used += width;
  }
  let end = termEnd;
  for (let used = 0; end < characters.length; end += 1) {
    const width = displayWidth(characters[end] ?? "");
    if (used + width > afterWidth) break;
    used += width;
  }
  const leading = start > 0 || text.startsWith("…") ? "…" : "";
  const trailing = end < characters.length || text.endsWith("…") ? "…" : "";
  const body = characters.slice(start, end).join("").replace(/^…|…$/g, "");
  return `${leading}${body.trim()}${trailing}`;
}
