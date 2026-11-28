/**
 * Where a Word file's paragraphs are on its rendered pages and whether an edit can be painted in
 * place (docx inline edit design §8.2, §8.3). Reads the PDF.js text of every page once, locates the
 * inventory's paragraphs (`document-locate.ts`) and measures each one's last-line room; an edit is
 * painted only when its added width fits that room, otherwise it is pending until the save
 * re-renders the pages.
 */

import {
  fitsOnLastLine,
  lastLineRoom,
  locateParagraphs,
  type LocatedLine,
  type LocatePage,
  type LocatedParagraph,
  type PdfTextItemLike,
} from "@/lib/document-locate";
import type { DocumentInventory, DocumentPendingEdit, DocumentReadOnlyParagraph } from "@/lib/document-edit";
import { normalizeForLocate } from "@/lib/document-locate";

/** The parts of a PDF.js document this module reads. */
export interface PdfDocumentForLayout {
  numPages: number;
  getPage: (page: number) => Promise<{
    view: number[];
    getTextContent: () => Promise<{ items: ReadonlyArray<{ str?: string; transform?: number[]; width?: number; height?: number }> }>;
  }>;
}

/** Each page's text items, in page points with the page's own origin. */
export async function readLocatePages(pdf: PdfDocumentForLayout, signal?: AbortSignal): Promise<LocatePage[]> {
  const pages: LocatePage[] = [];
  for (let number = 1; number <= pdf.numPages; number += 1) {
    if (signal?.aborted) throw new DOMException("aborted", "AbortError");
    const page = await pdf.getPage(number);
    const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = page.view;
    const content = await page.getTextContent();
    const items = content.items.flatMap((item): PdfTextItemLike[] => {
      if (!item.str || !item.transform || item.transform.length < 6) return [];
      const [a, b, c, d, e, f] = item.transform;
      return [{ str: item.str, transform: [a, b, c, d, e - x0, f - y0], width: item.width ?? 0, height: item.height ?? 0 }];
    });
    pages.push({ width: x1 - x0, height: y1 - y0, items });
  }
  return pages;
}

export interface ParagraphLayout {
  status: LocatedParagraph["status"];
  lines: LocatedLine[];
  /** Points the last line can grow; null when not found or split by a page break. */
  room: number | null;
}

export interface DocumentLayout {
  pages: Array<{ width: number; height: number }>;
  paragraphs: Map<string, ParagraphLayout>;
  /** Editable paragraphs not found on the pages: edited in 本页内容 (design §8.2). */
  missing: string[];
  /** Where headers, footers and notes are drawn, page by page (canvas W7). */
  readOnly: Map<string, LocatedLine[]>;
}

export function layoutDocument(
  pages: ReadonlyArray<LocatePage>,
  inventory: Pick<DocumentInventory, "paragraphs"> & { read_only_paragraphs?: ReadonlyArray<DocumentReadOnlyParagraph> },
): DocumentLayout {
  const located = locateParagraphs(pages, inventory.paragraphs);
  const room = lastLineRoom(located, inventory.paragraphs);
  const paragraphs = new Map<string, ParagraphLayout>();
  for (const result of located) paragraphs.set(result.key, { status: result.status, lines: result.lines, room: room.get(result.key) ?? null });
  const missing = inventory.paragraphs
    .filter((paragraph) => paragraph.editable && paragraphs.get(paragraph.key)?.status === "missing")
    .map((paragraph) => paragraph.key);
  const readOnly = locateReadOnly(pages, inventory.read_only_paragraphs ?? []);
  return { pages: pages.map(({ width, height }) => ({ width, height })), paragraphs, missing, readOnly };
}

/**
 * Headers, footers and notes on the pages (canvas W7): each is searched page by page, since a header
 * repeats on every page and a note sits on the page that cites it. Only text of two characters or more
 * is searched, so a lone page number is never placed.
 */
export function locateReadOnly(pages: ReadonlyArray<LocatePage>, items: ReadonlyArray<DocumentReadOnlyParagraph>): Map<string, LocatedLine[]> {
  const found = new Map<string, LocatedLine[]>();
  const searchable = items.filter((item) => normalizeForLocate(item.text).length >= 2);
  if (!searchable.length) return found;
  pages.forEach((page, index) => {
    const results = locateParagraphs([page], searchable.map((item) => ({ key: item.key, text: item.text })));
    for (const result of results) {
      if (result.status !== "exact") continue;
      const lines = result.lines.map((line) => ({ ...line, page: index }));
      found.set(result.key, [...(found.get(result.key) ?? []), ...lines]);
    }
  });
  return found;
}

/** The page a paragraph starts on (1-based), or null when it was not found. */
export function paragraphPage(layout: DocumentLayout, key: string): number | null {
  const line = layout.paragraphs.get(key)?.lines[0];
  return line ? line.page + 1 : null;
}

/** Text width in points: what the browser would draw at `fontSize`. */
export type TextMeasure = (text: string, fontSize: number) => number;

const WIDE = /[ᄀ-ᅟ⺀-〾ぁ-㏿㐀-䶿一-鿿ꀀ-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]|[\u{20000}-\u{3fffd}]/u;

/**
 * A measure from a canvas: CJK characters are one em, as Word lays them out; other text is
 * measured with the browser's sans-serif, which the half-em margin of `fitsOnLastLine` absorbs.
 */
export function canvasMeasure(): TextMeasure {
  const context = typeof document !== "undefined" ? document.createElement("canvas").getContext("2d") : null;
  return (text, fontSize) => {
    let width = 0;
    let narrow = "";
    for (const ch of text) {
      if (WIDE.test(ch)) width += fontSize;
      else narrow += ch;
    }
    if (!narrow) return width;
    if (!context) return width + narrow.length * fontSize * 0.55;
    context.font = `${fontSize}px sans-serif`;
    return width + context.measureText(narrow).width;
  };
}

/**
 * Painted on the page, or pending until the re-render (design §8.3, canvas W2). An edit is painted
 * only when its paragraph was found whole on one page, it adds no line break or tab, and the width
 * it adds fits the last line with half an em to spare.
 */
export function paintDecision(
  edit: DocumentPendingEdit,
  layout: ParagraphLayout | undefined,
  measure: TextMeasure,
  fieldToken: string,
): "paint" | "pending" {
  if (!layout || layout.status !== "exact" || layout.room === null || !layout.lines.length) return "pending";
  const breaks = (text: string) => (text.match(/\n/g) ?? []).length;
  const tabs = (text: string) => (text.match(/\t/g) ?? []).length;
  if (breaks(edit.text) > breaks(edit.expect) || tabs(edit.text) > tabs(edit.expect)) return "pending";
  const visible = (text: string) => text.split(fieldToken).join("").replace(/[\t\n]/g, "");
  const fontSize = layout.lines[layout.lines.length - 1].fontSize;
  const added = measure(visible(edit.text), fontSize) - measure(visible(edit.expect), fontSize);
  return fitsOnLastLine(layout.room, added, fontSize) ? "paint" : "pending";
}
