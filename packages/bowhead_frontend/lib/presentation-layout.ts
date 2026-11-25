/**
 * Where an editable element's text sits on the rendered page, so the deck editor can edit it in
 * place and paint pending text over the old render (pptx-inline-edit tech-design §13.3, spike §10).
 *
 * Everything here works in fractions of the page: `left`/`right` of its width, `top`/`bottom` of its
 * height, and a glyph's `size` (its font size) of the page height. Pixels and colours are the
 * caller's: it samples the rendered canvas and scales to the page it draws.
 */

import type { ElementBox, InventoryElement } from "./presentation-edit";

/** One run of text on the rendered page, as pdf.js reports it. `bottom` is the baseline. */
export interface PageGlyph {
  text: string;
  left: number;
  right: number;
  top: number;
  bottom: number;
  size: number;
  /** The PDF font name (PostScript), such as `PingFangSC-Semibold`; empty when unknown. */
  font: string;
}

/** pdf.js text content item with the fields read here. */
export interface PdfGlyphLike {
  str?: string;
  transform?: number[];
  width?: number;
  height?: number;
  fontName?: string;
}

/** Converts pdf.js text items of an unrotated page; `fonts` maps a pdf.js font id to its PDF name. */
export function pageGlyphs(items: ReadonlyArray<PdfGlyphLike>, view: ReadonlyArray<number>, fonts: Readonly<Record<string, string>> = {}): PageGlyph[] {
  const [x0, y0, x1, y1] = view;
  const pageWidth = x1 - x0 || 1;
  const pageHeight = y1 - y0 || 1;
  return items.flatMap((item): PageGlyph[] => {
    if (!item.str || !item.str.trim() || !item.transform || item.transform.length < 6) return [];
    const size = Math.hypot(item.transform[2], item.transform[3]) || item.height || 0;
    const left = (item.transform[4] - x0) / pageWidth;
    const bottom = 1 - (item.transform[5] - y0) / pageHeight;
    return [{
      text: item.str,
      left,
      right: left + (item.width ?? 0) / pageWidth,
      top: bottom - size / pageHeight,
      bottom,
      size: size / pageHeight,
      font: (item.fontName && fonts[item.fontName]) || "",
    }];
  });
}

export interface FontLook {
  /** CSS font-family list, the deck's family first. */
  family: string;
  /** The deck's family and its likely stand-ins, quoted, without the generic fallback. */
  faces: string[];
  weight: number;
  italic: boolean;
}

// Latin faces first: a browser takes each character from the first face that has it, so CJK
// characters still fall through to a CJK face.
const FALLBACK_FAMILY = "'Helvetica Neue', Arial, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif";

/** Faces a render substitutes, and the faces a viewer is likely to have with the same metrics. */
const FAMILY_ALIASES: Record<string, string[]> = {
  LiberationSans: ["Arial", "Helvetica"],
  LiberationSerif: ["Times New Roman", "Times"],
  LiberationMono: ["Courier New", "Courier"],
  Carlito: ["Calibri"],
  Caladea: ["Cambria"],
  NotoSansCJKsc: ["PingFang SC", "Microsoft YaHei"],
  NotoSerifCJKsc: ["Songti SC", "SimSun"],
  SimSong: ["Songti SC", "SimSun"],
  PingFangSC: ["PingFang SC"],
};

/** The CSS look of a PDF font name: `ABCDEF+PingFangSC-Semibold` is PingFang SC at 600. */
export function fontLook(pdfName: string): FontLook {
  const bare = pdfName.replace(/^[A-Z]{6}\+/, "");
  const dash = bare.indexOf("-");
  const family = dash > 0 ? bare.slice(0, dash) : bare;
  const style = dash > 0 ? bare.slice(dash + 1) : "";
  const semi = /semi|demi/i.test(style);
  const weight = /black|heavy/i.test(style) ? 900
    : /bold/i.test(style) && !semi ? 700
      : semi ? 600
        : /medium/i.test(style) ? 500
          : /light|thin/i.test(style) ? 300
            : /bold/i.test(family) ? 700 : 400;
  const spaced = family.replace(/([a-z])([A-Z]{2,})$/, "$1 $2");
  const names = family ? [spaced, family, ...(FAMILY_ALIASES[family] ?? [])] : [];
  const families = [...new Set(names)].map((name) => `"${name}"`);
  return { family: [...families, FALLBACK_FAMILY].join(", "), faces: families, weight, italic: /italic|oblique/i.test(style) };
}

function compact(text: string): string {
  return text.replace(/\s+/g, "");
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function inBox(glyph: PageGlyph, box: ElementBox): boolean {
  const [left, top, width, height] = box;
  const cx = (glyph.left + glyph.right) / 2;
  const cy = (glyph.top + glyph.bottom) / 2;
  // Text may run below its box (an overflow the render shows); it does not start left of it.
  return cx >= left - 0.004 && cx <= left + width + 0.004 && cy >= top - 0.01 && cy <= top + height + Math.max(height, 0.02);
}

/** Groups glyphs into lines by baseline, top to bottom, each left to right. */
function lines(glyphs: ReadonlyArray<PageGlyph>): PageGlyph[][] {
  const out: PageGlyph[][] = [];
  for (const glyph of [...glyphs].sort((a, b) => a.bottom - b.bottom || a.left - b.left)) {
    const line = out[out.length - 1];
    if (line && Math.abs(glyph.bottom - line[0].bottom) < Math.max(glyph.size, line[0].size) * 0.4) line.push(glyph);
    else out.push([glyph]);
  }
  return out.map((line) => line.sort((a, b) => a.left - b.left));
}

export interface ParagraphLayout {
  /** Paragraph index in the element. */
  index: number;
  left: number;
  top: number;
  right: number;
  /** Below the last line's descenders. */
  bottom: number;
  /** Median glyph size. */
  size: number;
  /** Baseline to baseline, when the paragraph has more than one line. */
  lineGap: number | null;
  glyphs: PageGlyph[];
  /** For each character of the paragraph text, the glyph that drew it (null for spaces and fields). */
  charGlyph: Array<number | null>;
  /** For each character, its index in its glyph's text (null where `charGlyph` is). */
  charOffset: Array<number | null>;
}

export interface TextLayout {
  paragraphs: ParagraphLayout[];
  left: number;
  top: number;
  right: number;
  bottom: number;
}

function charMap(text: string, glyphs: ReadonlyArray<PageGlyph>, fieldToken: string): { glyph: Array<number | null>; offset: Array<number | null> } {
  const drawn: Array<{ char: string; glyph: number; offset: number }> = [];
  glyphs.forEach((glyph, index) => {
    [...glyph.text].forEach((char, offset) => { if (!/\s/.test(char)) drawn.push({ char, glyph: index, offset }); });
  });
  const out: Array<number | null> = [];
  const offsets: Array<number | null> = [];
  let at = 0;
  for (const char of text) {
    if (/\s/.test(char) || char === fieldToken) {
      out.push(null);
      offsets.push(null);
      continue;
    }
    let found = -1;
    for (let probe = at; probe < Math.min(drawn.length, at + 4); probe += 1) {
      if (drawn[probe].char === char) {
        found = probe;
        break;
      }
    }
    if (found < 0) {
      out.push(null);
      offsets.push(null);
      continue;
    }
    out.push(drawn[found].glyph);
    offsets.push(drawn[found].offset);
    at = found + 1;
  }
  return { glyph: out, offset: offsets };
}

/**
 * The element's own text on the render: glyphs inside its box whose text belongs to the element
 * (position alone takes a neighbour's text where boxes overlap), grouped into the element's
 * paragraphs in reading order. Null when none of its text is found.
 */
export function locateText(element: InventoryElement, glyphs: ReadonlyArray<PageGlyph>, fieldToken = "￼"): TextLayout | null {
  const box = element.box;
  const paragraphs = element.paragraphs ?? [];
  if (!box || !paragraphs.length) return null;
  const whole = compact(paragraphs.map((paragraph) => paragraph.text.split(fieldToken).join("")).join(""));
  if (!whole) return null;
  const own = glyphs.filter((glyph) => {
    const text = compact(glyph.text);
    return text && whole.includes(text) && inBox(glyph, box);
  });
  if (!own.length) return null;
  const ordered = lines(own);
  const out: ParagraphLayout[] = [];
  let lineAt = 0;
  paragraphs.forEach((paragraph, index) => {
    const target = compact(paragraph.text.split(fieldToken).join(""));
    if (!target || lineAt >= ordered.length) return;
    const taken: PageGlyph[][] = [];
    let text = "";
    while (lineAt < ordered.length) {
      const next = text + compact(ordered[lineAt].map((glyph) => glyph.text).join(""));
      if (!target.startsWith(next) && !(text === "" && target.includes(next))) break;
      taken.push(ordered[lineAt]);
      text = next;
      lineAt += 1;
      if (text.length >= target.length) break;
    }
    if (!taken.length) return;
    const flat = taken.flat();
    const baselines = taken.map((line) => Math.max(...line.map((glyph) => glyph.bottom)));
    const gaps = baselines.slice(1).map((value, i) => value - baselines[i]).filter((gap) => gap > 0);
    const size = median(flat.map((glyph) => glyph.size));
    const chars = charMap(paragraph.text, flat, fieldToken);
    out.push({
      index,
      left: Math.min(...flat.map((glyph) => glyph.left)),
      top: Math.min(...flat.map((glyph) => glyph.top)),
      right: Math.max(...flat.map((glyph) => glyph.right)),
      bottom: Math.max(...baselines) + size * 0.25,
      size,
      lineGap: gaps.length ? median(gaps) : null,
      glyphs: flat,
      charGlyph: chars.glyph,
      charOffset: chars.offset,
    });
  });
  if (!out.length) return null;
  return {
    paragraphs: out,
    left: Math.min(...out.map((item) => item.left)),
    top: Math.min(...out.map((item) => item.top)),
    right: Math.max(...out.map((item) => item.right)),
    bottom: Math.max(...out.map((item) => item.bottom)),
  };
}

export interface TableLayout {
  /** Column edges, left to right (columns + 1 values). */
  xs: number[];
  /** Row edges, top to bottom (rows + 1 values); rows grow where the render wrapped their text. */
  ys: number[];
  /** Glyphs drawn in each cell, `[row][col]`. */
  cells: PageGlyph[][][];
  /** Share of the cells with text whose text was found in its row. */
  located: number;
}

/**
 * Cell rectangles of a table on the render. Columns follow the file's grid; rows are placed from
 * the rendered text lines, assigned to rows in order, because rows grow when their text wraps
 * (29 of 44 tables in the spike corpus rendered taller than their file geometry).
 */
export function locateTable(element: InventoryElement, glyphs: ReadonlyArray<PageGlyph>): TableLayout | null {
  const box = element.box;
  const rows = element.rows;
  const colWidths = element.col_widths;
  const rowHeights = element.row_heights;
  if (!box || !rows?.length || !colWidths?.length || !rowHeights?.length) return null;
  const [left, top, width, height] = box;
  const xs = [left];
  for (const share of colWidths) xs.push(xs[xs.length - 1] + share * width);
  const colOf = (glyph: PageGlyph) => {
    const cx = (glyph.left + glyph.right) / 2;
    for (let c = 0; c < xs.length - 1; c += 1) if (cx >= xs[c] - 0.002 && cx <= xs[c + 1] + 0.002) return c;
    return -1;
  };
  const candidates = glyphs.filter((glyph) => colOf(glyph) >= 0 && glyph.top >= top - 0.01 && glyph.bottom <= top + height * 3 + 0.05);
  const score = (line: PageGlyph[], row: number) => line.reduce((sum, glyph) => {
    const text = compact(glyph.text);
    const cell = compact(rows[row]?.[colOf(glyph)]?.text ?? "");
    return sum + (text && cell.includes(text) ? text.length : 0);
  }, 0);
  const useful = lines(candidates).filter((line) => rows.some((_, row) => score(line, row) > 0));
  // Lines go to rows in order (a row never comes before the previous line's row): best total match.
  const rowOfLine: number[] = [];
  if (useful.length) {
    const negative = Number.NEGATIVE_INFINITY;
    const best = useful.map(() => rows.map(() => negative));
    const from = useful.map(() => rows.map(() => -1));
    rows.forEach((_, row) => { best[0][row] = score(useful[0], row); });
    for (let i = 1; i < useful.length; i += 1) {
      for (let row = 0; row < rows.length; row += 1) {
        let top2 = negative;
        let arg = -1;
        for (let prior = 0; prior <= row; prior += 1) if (best[i - 1][prior] > top2) { top2 = best[i - 1][prior]; arg = prior; }
        best[i][row] = top2 + score(useful[i], row);
        from[i][row] = arg;
      }
    }
    const last = best[useful.length - 1];
    let row = last.indexOf(Math.max(...last));
    for (let i = useful.length - 1; i >= 0; i -= 1) {
      rowOfLine[i] = row;
      row = from[i][row];
    }
  }
  const linesOf: PageGlyph[][][] = rows.map(() => []);
  useful.forEach((line, i) => linesOf[rowOfLine[i]].push(line));
  const ys = [top];
  for (let row = 0; row < rows.length; row += 1) {
    const own = linesOf[row].flat();
    const fileHeight = rowHeights[row] * height;
    if (!own.length) {
      ys.push(ys[row] + fileHeight);
      continue;
    }
    const textTop = Math.min(...own.map((glyph) => glyph.top));
    const textBottom = Math.max(...own.map((glyph) => glyph.bottom + glyph.size * 0.25));
    const padTop = Math.max(0.004, textTop - ys[row]);
    const next = linesOf[row + 1]?.flat() ?? [];
    const end = next.length
      ? (textBottom + Math.min(...next.map((glyph) => glyph.top))) / 2
      : Math.max(ys[row] + fileHeight, textBottom + padTop);
    ys.push(Math.max(end, ys[row] + 0.01));
  }
  const cells: PageGlyph[][][] = rows.map((row) => row.map(() => []));
  linesOf.forEach((rowLines, row) => rowLines.flat().forEach((glyph) => {
    const col = colOf(glyph);
    if (col >= 0 && cells[row][col]) cells[row][col].push(glyph);
  }));
  let filled = 0;
  let found = 0;
  rows.forEach((row, r) => row.forEach((cell, c) => {
    if (!compact(cell.text)) return;
    filled += 1;
    if (cells[r][c].length) found += 1;
  }));
  return { xs, ys, cells, located: filled ? found / filled : 1 };
}

/** The rectangle `[left, top, width, height]` of one cell. */
export function cellRect(layout: TableLayout, row: number, col: number): ElementBox {
  return [layout.xs[col], layout.ys[row], layout.xs[col + 1] - layout.xs[col], layout.ys[row + 1] - layout.ys[row]];
}

export interface StyledRun {
  text: string;
  /** The look this text takes (an index the caller chose per old character), or null when none is known. */
  glyph: number | null;
}

/**
 * New text split into runs that keep the look of the old text: unchanged characters keep the
 * glyph that drew them, replaced characters keep the look of what they replace, and inserted
 * characters take the look of the character before them, as the saved file will (run-keeping
 * writer, spike §3).
 */
export function styledRuns(oldText: string, newText: string, charGlyph: ReadonlyArray<number | null>): StyledRun[] {
  const before = [...oldText];
  const after = [...newText];
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix += 1;
  // Spaces and fields have no glyph of their own: they take the nearest one before (else after).
  const filled: Array<number | null> = [...charGlyph];
  let last: number | null = null;
  for (let i = 0; i < filled.length; i += 1) {
    if (filled[i] === null) filled[i] = last;
    else last = filled[i];
  }
  let next: number | null = null;
  for (let i = filled.length - 1; i >= 0; i -= 1) {
    if (filled[i] === null) filled[i] = next;
    else next = filled[i];
  }
  const styleAt = (index: number): number | null => filled[Math.min(Math.max(index, 0), filled.length - 1)] ?? null;
  // Replaced characters keep the look of what they replace; pure insertions take the one before.
  const replaced = before.length - prefix - suffix;
  const looks = after.map((_, i) => {
    if (i < prefix) return styleAt(i);
    if (i >= after.length - suffix) return styleAt(before.length - (after.length - i));
    if (replaced > 0) return styleAt(prefix + Math.min(i - prefix, replaced - 1));
    return styleAt(prefix > 0 ? prefix - 1 : prefix);
  });
  const runs: StyledRun[] = [];
  after.forEach((char, i) => {
    const run = runs[runs.length - 1];
    if (run && run.glyph === looks[i]) run.text += char;
    else runs.push({ text: char, glyph: looks[i] });
  });
  return runs;
}

/** Horizontal alignment of text in a box, judged from where the render placed it. */
export function alignment(glyphs: ReadonlyArray<PageGlyph>, left: number, right: number): "left" | "center" | "right" {
  if (!glyphs.length) return "left";
  const start = Math.min(...glyphs.map((glyph) => glyph.left)) - left;
  const end = right - Math.max(...glyphs.map((glyph) => glyph.right));
  const width = right - left || 1;
  if (Math.abs(start - end) < width * 0.08 && start > width * 0.1) return "center";
  if (end < width * 0.12 && start > end * 2 && start > width * 0.2) return "right";
  return "left";
}
