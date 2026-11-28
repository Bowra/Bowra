/**
 * Finding a Word file's paragraphs on its rendered pages (docx inline edit design §8.2).
 *
 * Word files carry no geometry, so every position comes from the render: the PDF.js text items of
 * each page are joined into one character stream and the inventory's paragraphs are matched in
 * document order. Two rules from the spike make this reliable:
 *
 * - LibreOffice draws a line in font portions (CJK text first, then Latin quotes, digits and
 *   dashes), so stream order is not reading order. Consecutive items that share a line are
 *   re-sorted by x, character by character: one item often holds a whole line of CJK text with
 *   the quotes drawn into its gaps, so each character's x is estimated inside its item (wide
 *   characters take twice the width of narrow ones).
 * - Short repeated texts (table cells) cause false forward jumps. Paragraphs whose text occurs
 *   once are matched first and kept in increasing order (longest increasing subsequence); the
 *   others are matched in order between those anchors, in up to four pieces when a page break,
 *   a header or a footer cuts them.
 * - LibreOffice draws some text out of document order: a floating table after the paragraph that
 *   follows it, the rest of a cell on the next page after its neighbour cell. A paragraph the
 *   ordered pass missed takes the first unclaimed occurrence of its text from the start of the
 *   paragraph before it, on the pages between the paragraphs found before and after it.
 *
 * Whitespace, field tokens and compatibility forms (NFKC) are ignored on both sides. Positions are
 * in PDF points with the origin at the page's top left. Pure: no DOM, so it runs in tests and in a
 * worker alike.
 */

export type PdfTextItemLike = {
  str: string;
  /** PDF.js text matrix: [a, b, c, d, e, f]; e and f place the baseline start in PDF space (y up). */
  transform: ArrayLike<number>;
  width: number;
  height: number;
};

export type LocatePage = { width: number; height: number; items: ReadonlyArray<PdfTextItemLike | { type?: string }> };

export type LocateParagraph = { key: string; text: string };

/** One drawn line of a paragraph: its box on a page (0-based page index, points, top-left origin). */
export type LocatedLine = { page: number; x0: number; top: number; x1: number; bottom: number; fontSize: number };

export type LocatedParagraph = {
  key: string;
  /** exact: found whole; split: found in pieces (a page break, header or footer cut it); missing: not found. */
  status: "exact" | "split" | "missing" | "empty";
  lines: LocatedLine[];
};

const IGNORED = /[\s 　​￼]+/gu;
const MAX_PIECES = 4;
const PIECE_WINDOW = 400;
const MIN_PIECE = 4;
const MIN_ANCHOR_LENGTH = 6;

/** The text both sides are compared on. */
export function normalizeForLocate(text: string): string {
  return text.normalize("NFKC").replace(IGNORED, "");
}

type Glyph = { page: number; line: number; x0: number; x1: number; top: number; bottom: number; fontSize: number };

type Item = { page: number; str: string; x0: number; x1: number; top: number; bottom: number; fontSize: number };

function itemsOf(pages: ReadonlyArray<LocatePage>): Item[][] {
  return pages.map((page, index) => {
    const out: Item[] = [];
    for (const raw of page.items) {
      if (!("str" in raw) || typeof raw.str !== "string" || !raw.str) continue;
      const [a, b, c, d, e, f] = Array.from(raw.transform);
      // Rotated or skewed text is not body text of a Word page; it would only confuse the line rule.
      if (Math.abs(b) > 1e-3 || Math.abs(c) > 1e-3 || a <= 0 || d <= 0) continue;
      const fontSize = raw.height || d;
      const baseline = page.height - f;
      out.push({
        page: index,
        str: raw.str,
        x0: e,
        x1: e + Math.max(raw.width, 0),
        top: baseline - fontSize * 0.8,
        bottom: baseline + fontSize * 0.2,
        fontSize,
      });
    }
    return out;
  });
}

/** Consecutive items whose vertical extents overlap by half the smaller height share a line. */
function lineGroups(items: Item[]): Item[][] {
  const groups: Item[][] = [];
  let group: Item[] = [];
  for (const item of items) {
    const first = group[0];
    if (first) {
      const overlap = Math.min(first.bottom, item.bottom) - Math.max(first.top, item.top);
      const height = Math.min(first.bottom - first.top, item.bottom - item.top) || 1;
      if (overlap < 0.5 * height) {
        groups.push(group);
        group = [];
      }
    }
    group.push(item);
  }
  if (group.length) groups.push(group);
  return groups;
}

const WIDE = /[\u1100-\u115f\u2e80-\u303e\u3041-\u33ff\u3400-\u4dbf\u4e00-\u9fff\ua000-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]|[\u{20000}-\u{3fffd}]/u;

/** The characters of one item with their estimated boxes, in the stream's normalized form. */
function glyphsOf(item: Item, line: number): Array<Glyph & { ch: string }> {
  const slots = [...item.str].map((ch) => ({ ch: normalizeForLocate(ch), weight: WIDE.test(ch) ? 2 : 1 }));
  const units = slots.reduce((sum, slot) => sum + slot.weight, 0) || 1;
  const step = (item.x1 - item.x0) / units;
  const out: Array<Glyph & { ch: string }> = [];
  let x = item.x0;
  for (const slot of slots) {
    const end = x + step * slot.weight;
    const parts = [...slot.ch];
    parts.forEach((ch, k) => {
      const part = (end - x) / parts.length;
      out.push({ ch, page: item.page, line, x0: x + part * k, x1: x + part * (k + 1), top: item.top, bottom: item.bottom, fontSize: item.fontSize });
    });
    x = end;
  }
  return out;
}

function buildStream(pages: ReadonlyArray<LocatePage>): { stream: string; glyphs: Glyph[] } {
  const glyphs: Glyph[] = [];
  let stream = "";
  let line = 0;
  for (const items of itemsOf(pages)) {
    for (const group of lineGroups(items)) {
      const lineGlyphs = group.flatMap((item) => glyphsOf(item, line));
      // Stable: characters at the same x keep their drawing order.
      lineGlyphs.sort((left, right) => (left.x0 + left.x1) / 2 - (right.x0 + right.x1) / 2);
      for (const { ch, ...glyph } of lineGlyphs) {
        glyphs.push(glyph);
        stream += ch;
      }
      line += 1;
    }
  }
  return { stream, glyphs };
}

/** Indices into `items` of the longest subsequence whose positions increase. */
function longestIncreasing(items: ReadonlyArray<{ position: number }>): number[] {
  const tails: number[] = [];
  const previous = new Array<number>(items.length).fill(-1);
  for (let k = 0; k < items.length; k += 1) {
    let low = 0;
    let high = tails.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (items[tails[mid]].position < items[k].position) low = mid + 1;
      else high = mid;
    }
    previous[k] = low > 0 ? tails[low - 1] : -1;
    tails[low] = k;
  }
  const out: number[] = [];
  for (let k = tails.length ? tails[tails.length - 1] : -1; k >= 0; k = previous[k]) out.push(k);
  return out.reverse();
}

function findBefore(stream: string, text: string, from: number, end: number): number {
  const found = stream.indexOf(text, from);
  return found >= 0 && found + text.length <= end ? found : -1;
}

/** The pieces [start, end) of `text` in order after `from`, or null. */
function piecewise(stream: string, text: string, from: number, end: number): Array<[number, number]> | null {
  const pieces: Array<[number, number]> = [];
  let position = from;
  let rest = text;
  while (rest && pieces.length < MAX_PIECES) {
    // The longest prefix of the rest found in the window (a found prefix's own prefixes are found
    // no later, so the search is monotone). Pieces are at least four characters unless less remains.
    const window = pieces.length === 0 ? end : Math.min(end, position + PIECE_WINDOW);
    const at = (size: number) => findBefore(stream, rest.slice(0, size), position, window + size);
    let low = Math.min(MIN_PIECE, rest.length);
    let found = at(low);
    if (found < 0) return null;
    let high = rest.length;
    let size = low;
    while (low <= high) {
      const mid = (low + high) >> 1;
      const hit = at(mid);
      if (hit >= 0) {
        size = mid;
        found = hit;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    pieces.push([found, found + size]);
    position = found + size;
    rest = rest.slice(size);
  }
  return rest ? null : pieces;
}

function linesOf(glyphs: Glyph[], ranges: Array<[number, number]>): LocatedLine[] {
  const lines = new Map<number, LocatedLine>();
  for (const [start, end] of ranges) {
    for (let k = start; k < end; k += 1) {
      const glyph = glyphs[k];
      const line = lines.get(glyph.line);
      if (!line) {
        lines.set(glyph.line, { page: glyph.page, x0: glyph.x0, top: glyph.top, x1: glyph.x1, bottom: glyph.bottom, fontSize: glyph.fontSize });
      } else {
        line.x0 = Math.min(line.x0, glyph.x0);
        line.x1 = Math.max(line.x1, glyph.x1);
        line.top = Math.min(line.top, glyph.top);
        line.bottom = Math.max(line.bottom, glyph.bottom);
        line.fontSize = Math.max(line.fontSize, glyph.fontSize);
      }
    }
  }
  return [...lines.entries()].sort(([left], [right]) => left - right).map(([, line]) => line);
}

const RANGES = Symbol("ranges");

function claim(result: LocatedParagraph, glyphs: Glyph[], ranges: Array<[number, number]>, status: "exact" | "split"): void {
  result.status = status;
  result.lines = linesOf(glyphs, ranges);
  Object.defineProperty(result, RANGES, { value: ranges, enumerable: false });
}

export function locateParagraphs(pages: ReadonlyArray<LocatePage>, paragraphs: ReadonlyArray<LocateParagraph>): LocatedParagraph[] {
  const { stream, glyphs } = buildStream(pages);
  const texts = paragraphs.map((paragraph) => normalizeForLocate(paragraph.text));
  const flow = texts.map((text, index) => ({ index, text })).filter((entry) => entry.text);

  const candidates: Array<{ flow: number; position: number }> = [];
  flow.forEach((entry, k) => {
    if (entry.text.length < MIN_ANCHOR_LENGTH) return;
    const first = stream.indexOf(entry.text);
    if (first >= 0 && stream.indexOf(entry.text, first + 1) < 0) candidates.push({ flow: k, position: first });
  });
  const anchors = new Map<number, number>();
  for (const k of longestIncreasing(candidates)) anchors.set(candidates[k].flow, candidates[k].position);
  const nextAnchor = new Array<number>(flow.length + 1).fill(stream.length);
  for (let k = flow.length - 1; k >= 0; k -= 1) nextAnchor[k] = anchors.get(k) ?? nextAnchor[k + 1];

  const out: LocatedParagraph[] = paragraphs.map((paragraph) => ({ key: paragraph.key, status: "empty", lines: [] }));
  let cursor = 0;
  flow.forEach((entry, k) => {
    const result = out[entry.index];
    const anchored = anchors.get(k);
    if (anchored !== undefined) {
      claim(result, glyphs, [[anchored, anchored + entry.text.length]], "exact");
      cursor = anchored + entry.text.length;
      return;
    }
    const end = nextAnchor[k + 1];
    const found = findBefore(stream, entry.text, cursor, end);
    if (found >= 0) {
      claim(result, glyphs, [[found, found + entry.text.length]], "exact");
      cursor = found + entry.text.length;
      return;
    }
    const pieces = piecewise(stream, entry.text, cursor, end);
    if (pieces) {
      claim(result, glyphs, pieces, "split");
      cursor = pieces[pieces.length - 1][1];
      return;
    }
    result.status = "missing";
  });
  recoverOutOfOrder(stream, glyphs, flow, out);
  return out;
}

type Claim = { ranges: Array<[number, number]> };

function recoverOutOfOrder(
  stream: string,
  glyphs: Glyph[],
  flow: ReadonlyArray<{ index: number; text: string }>,
  out: LocatedParagraph[],
): void {
  const claims = new Map<number, Claim>();
  const used: Array<[number, number]> = [];
  flow.forEach((entry) => {
    const ranges = rangesOf(out[entry.index]);
    if (ranges) {
      claims.set(entry.index, { ranges });
      used.push(...ranges);
    }
  });
  if (claims.size === flow.length) return;
  const free = (start: number, end: number) => used.every(([left, right]) => end <= left || start >= right);
  flow.forEach((entry, k) => {
    const result = out[entry.index];
    if (result.status !== "missing") return;
    const before = flow.slice(0, k).reverse().find((item) => claims.has(item.index));
    const after = flow.slice(k + 1).find((item) => claims.has(item.index));
    const from = before ? claims.get(before.index)!.ranges[0][0] : 0;
    const firstPage = before ? glyphs[claims.get(before.index)!.ranges[0][0]].page : 0;
    const lastPage = after ? glyphs[claims.get(after.index)!.ranges.at(-1)![1] - 1].page : Number.POSITIVE_INFINITY;
    for (let found = stream.indexOf(entry.text, from); found >= 0; found = stream.indexOf(entry.text, found + 1)) {
      const end = found + entry.text.length;
      const page = glyphs[found].page;
      if (page > lastPage) break;
      if (page < firstPage || !free(found, end)) continue;
      claim(result, glyphs, [[found, end]], "exact");
      claims.set(entry.index, { ranges: [[found, end]] });
      used.push([found, end]);
      break;
    }
  });
}

/** The stream ranges a located paragraph claimed, kept on the result for the recovery pass. */
function rangesOf(result: LocatedParagraph): Array<[number, number]> | null {
  return (result as LocatedParagraph & { [RANGES]?: Array<[number, number]> })[RANGES] ?? null;
}

export type RoomParagraph = {
  key: string;
  kind: "body" | "cell";
  text: string;
  /** The cell of a table paragraph, as the inventory gives it. */
  table?: { key: string; col: number; col_span: number } | null;
};

/** Spaces, tabs and line breaks the locator cannot see; at the end of a paragraph they fill the line. */
const TRAILING_SPACE = /[\s\u00a0\u3000]$/u;

/**
 * How far, in points, each located paragraph's last line can grow before the paragraph needs
 * another line (docx inline edit design §8.3), from the render alone. The right edge a line can
 * reach is where full lines end:
 *
 * - a paragraph of several lines: its own full lines;
 * - a single-line body paragraph: the full body lines on its page;
 * - a single-line cell paragraph: the full lines of the multi-line paragraphs in its table column
 *   (a column keeps its width down the table).
 *
 * Centred text gets half its real room this way and right-aligned text none, which errs on the
 * side of showing the edit as pending. A paragraph ending in spaces, tabs or a line break has room
 * 0: their width is not in the located box, and text after a line break starts a new line. With
 * no known edge the room is 0, so only an edit that does not widen the line is painted. Null for a
 * paragraph that was not found or that a page break splits.
 */
export function lastLineRoom(located: ReadonlyArray<LocatedParagraph>, paragraphs: ReadonlyArray<RoomParagraph>): Map<string, number | null> {
  const byKey = new Map(paragraphs.map((paragraph) => [paragraph.key, paragraph]));
  const edgeKey = (result: LocatedParagraph): string | null => {
    const paragraph = byKey.get(result.key);
    if (!paragraph || !result.lines.length) return null;
    if (paragraph.table) return `cell:${paragraph.table.key}/${paragraph.table.col}/${paragraph.table.col_span}`;
    return paragraph.kind === "body" ? `page:${result.lines[0].page}` : null;
  };
  const edges = new Map<string, number>();
  for (const result of located) {
    const key = edgeKey(result);
    if (!key || result.lines.length < 2) continue;
    for (const line of result.lines.slice(0, -1)) edges.set(key, Math.max(edges.get(key) ?? 0, line.x1));
  }
  const out = new Map<string, number | null>();
  for (const result of located) {
    const lines = result.lines;
    const last = lines.at(-1);
    if (!last || lines.some((line) => line.page !== last.page)) {
      out.set(result.key, null);
      continue;
    }
    if (TRAILING_SPACE.test(byKey.get(result.key)?.text ?? "")) {
      out.set(result.key, 0);
      continue;
    }
    const own = lines.length > 1 ? Math.max(...lines.slice(0, -1).map((line) => line.x1)) : undefined;
    const key = edgeKey(result);
    const edge = own ?? (key ? edges.get(key) : undefined);
    out.set(result.key, edge !== undefined ? Math.max(0, edge - last.x1) : 0);
  }
  return out;
}

/**
 * Half an em kept free on the last line. Measured on the spike corpus (11 documents, 4,601 edits
 * that each appended 2, 6 or 12 CJK characters, re-rendered with LibreOffice): with this margin no
 * edit painted in place had in fact needed another line; without it, 3 had.
 */
export const LAST_LINE_SAFETY_EM = 0.5;

/** Whether an edit that widens the last line by `addedWidth` points can be painted in place. */
export function fitsOnLastLine(room: number | null | undefined, addedWidth: number, fontSize: number): boolean {
  if (addedWidth <= 0) return room !== null && room !== undefined;
  if (room === null || room === undefined) return false;
  return addedWidth <= room - LAST_LINE_SAFETY_EM * fontSize;
}
