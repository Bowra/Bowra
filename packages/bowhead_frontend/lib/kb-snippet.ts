/**
 * Deep-search snippet excerpts: center a window around the longest contiguous query match and highlight it.
 *
 * Previously searched only for the verbatim full query, which semantic queries almost never match; consequently always showed the beginning
 * (observed on 116: a table excerpt began with unrelated text while the matching row started at character 715).
 */
export interface SnippetWindow {
  before: string;
  match: string;
  after: string;
}

const MIN_MATCH = 2;
/** Source-card minimum highlight match: common two-character words (“销售”, “数据”) appear in almost any snippet; using them to position
 *  and highlight a window is essentially random. Require at least four characters (about two Chinese words) to truly match the body before the citation. */
const MIN_QUOTE_MATCH = 4;
const MAX_QUERY = 40;

export function snippetWindow(
  body: string,
  query: string,
  context = { before: 14, after: 18, fallback: 40 },
  minMatch = MIN_MATCH,
): SnippetWindow {
  const q = query.trim().slice(0, MAX_QUERY);
  const lowerBody = body.toLowerCase();
  const lowerQ = q.toLowerCase();
  for (let len = lowerQ.length; len >= minMatch; len--) {
    for (let start = 0; start + len <= lowerQ.length; start++) {
      const piece = lowerQ.slice(start, start + len);
      // Whitespace does not count: source-card minimum N characters uses length after trimming leading/trailing whitespace.
      const core = piece.trim();
      if (!core || (minMatch > MIN_MATCH && core.length < minMatch)) continue;
      const i = lowerBody.indexOf(piece);
      if (i >= 0) {
        const s = Math.max(0, i - context.before);
        return {
          before: body.slice(s, i),
          match: body.slice(i, i + len),
          after: body.slice(i + len, i + len + context.after),
        };
      }
    }
  }
  return { before: body.slice(0, context.fallback), match: "", after: "" };
}

export interface QuoteLine {
  before: string;
  match: string;
  after: string;
}

/** Original text for chat knowledge-base source cards: at most `maxLines` lines, centered near the line containing the longest contiguous match with `context`
 *  (body text before the citation); if a table excerpt (first line starts with `|`) omits the first line, add its header.
 *  End truncated content with “…”. Use the same match logic as deep-search excerpts (`snippetWindow`), but require at least
 *  `MIN_QUOTE_MATCH` characters; without a match, show the first few lines without highlighting. */
export function quoteLines(body: string, context: string, maxLines = 8): QuoteLine[] {
  const lines = body.split("\n");
  const w = context.trim() ? snippetWindow(body, context, { before: 0, after: 0, fallback: 0 }, MIN_QUOTE_MATCH) : null;
  let matchLine = -1;
  let matchCol = -1;
  if (w && w.match) {
    const at = body.toLowerCase().indexOf(w.match.toLowerCase());
    let offset = 0;
    for (let i = 0; i < lines.length; i++) {
      if (at < offset + lines[i].length + 1) {
        matchLine = i;
        matchCol = at - offset;
        break;
      }
      offset += lines[i].length + 1;
    }
  }
  const isTable = lines[0]?.trimStart().startsWith("|") ?? false;
  let header: number[] = [];
  let budget = maxLines;
  let start = 0;
  if (matchLine > 0) {
    if (isTable) {
      header = [0];
      if (/^\s*\|?\s*:?-{2,}/.test(lines[1] ?? "")) header.push(1);
    }
    budget = maxLines - header.length;
    start = Math.max(header.length, matchLine - Math.floor((budget - 1) / 2));
    start = Math.min(start, Math.max(header.length, lines.length - budget));
    if (start <= header.length) {
      header = [];
      budget = maxLines;
      start = 0;
    }
  }
  const picked = [...header, ...Array.from({ length: Math.min(budget, lines.length - start) }, (_, k) => start + k)];
  const out = picked.map((i): QuoteLine => {
    const line = lines[i];
    if (i === matchLine && w && w.match) {
      return {
        before: line.slice(0, matchCol),
        match: line.slice(matchCol, matchCol + w.match.length),
        after: line.slice(matchCol + w.match.length),
      };
    }
    return { before: line, match: "", after: "" };
  });
  if (picked.length && picked[picked.length - 1] < lines.length - 1) out.push({ before: "…", match: "", after: "" });
  return out;
}
