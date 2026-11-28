"use client";

/**
 * The Word editor's pages (docx inline edit design §8.2, §8.3; canvas W1, W2, W7, W9, W10): the
 * rendered pages stacked vertically, a hit area over each paragraph found on them, and the field
 * that edits a paragraph where it is drawn.
 *
 * Selecting a paragraph keeps its field focused but hidden, so typing and input methods land from
 * the first key; editing shows the same field over the paragraph. Enter finishes the paragraph,
 * Shift+Enter breaks the line inside it, Esc keeps the text and returns to the selection, Tab moves
 * on. An edit that fits the paragraph's last line is painted on the page; one that adds a line is
 * outlined with its number and its new text sits in a card in the right margin until the save
 * re-renders the pages.
 *
 * On phones (`touch`, canvas WP1) a tap opens the paragraph's sheet: every hit area is at least 44 px
 * each way, and where two grown areas would overlap the boundary is the midpoint between the two
 * paragraphs, so the paragraph nearest the tap wins.
 */

import { type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useRef } from "react";

import type { DocumentPendingEdit } from "@/lib/document-edit";
import type { DocumentLayout } from "@/lib/document-layout";
import type { LocatedLine } from "@/lib/document-locate";
import { LazyPdfPageCanvas, type PdfDocumentLike } from "../presentation-edit/pdf-page";
import { isComposing } from "../presentation-edit/keys";
import styles from "./document-editor.module.css";

export type DocumentEditing = { key: string; armed: boolean } | null;

type Rect = { left: number; top: number; width: number; height: number };

const PAD = 2;
const CARET_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]);
/** Fields being edited stay readable when the pages fit a narrow window (decision 2026-11-27). */
const MIN_FIELD_FONT = 13;

function linesOnPage(lines: ReadonlyArray<LocatedLine>, page: number): LocatedLine[] {
  return lines.filter((line) => line.page === page);
}

function rectOf(lines: ReadonlyArray<LocatedLine>, scale: number, extendRight = 0): Rect {
  const x0 = Math.min(...lines.map((line) => line.x0));
  const x1 = Math.max(...lines.map((line) => line.x1), extendRight);
  const top = Math.min(...lines.map((line) => line.top));
  const bottom = Math.max(...lines.map((line) => line.bottom));
  return { left: x0 * scale - PAD, top: top * scale - PAD, width: (x1 - x0) * scale + 2 * PAD, height: (bottom - top) * scale + 2 * PAD };
}

/** Touch targets on phones (canvas WP1). */
export const MIN_TOUCH = 44;

/**
 * Each rect grown to at least `MIN_TOUCH` each way; where two grown rects overlap, the boundary is the
 * midpoint of the gap between the original rects, so a tap goes to the nearest paragraph. Rects that
 * already overlap stay as they are. The result stays inside the page.
 */
export function touchRects(rects: ReadonlyArray<Rect>, bounds: { width: number; height: number }): Rect[] {
  const grown = rects.map((rect) => {
    const dx = Math.max(0, MIN_TOUCH - rect.width) / 2;
    const dy = Math.max(0, MIN_TOUCH - rect.height) / 2;
    return { l: rect.left - dx, t: rect.top - dy, r: rect.left + rect.width + dx, b: rect.top + rect.height + dy };
  });
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      const a = grown[i];
      const b = grown[j];
      if (a.r <= b.l || b.r <= a.l || a.b <= b.t || b.b <= a.t) continue;
      const ra = rects[i];
      const rb = rects[j];
      const aBottom = ra.top + ra.height;
      const bBottom = rb.top + rb.height;
      const aRight = ra.left + ra.width;
      const bRight = rb.left + rb.width;
      if (aBottom <= rb.top) {
        const mid = (aBottom + rb.top) / 2;
        a.b = Math.min(a.b, mid);
        b.t = Math.max(b.t, mid);
      } else if (bBottom <= ra.top) {
        const mid = (bBottom + ra.top) / 2;
        b.b = Math.min(b.b, mid);
        a.t = Math.max(a.t, mid);
      } else if (aRight <= rb.left) {
        const mid = (aRight + rb.left) / 2;
        a.r = Math.min(a.r, mid);
        b.l = Math.max(b.l, mid);
      } else if (bRight <= ra.left) {
        const mid = (bRight + ra.left) / 2;
        b.r = Math.min(b.r, mid);
        a.l = Math.max(a.l, mid);
      }
    }
  }
  return grown.map((box) => {
    const left = Math.max(0, box.l);
    const top = Math.max(0, box.t);
    return { left, top, width: Math.max(0, Math.min(bounds.width, box.r) - left), height: Math.max(0, Math.min(bounds.height, box.b) - top) };
  });
}

/** The distance between two lines of the paragraph, or 1.5 times its size for a single line. */
function linePitch(lines: ReadonlyArray<LocatedLine>): number {
  if (lines.length > 1) return Math.max(lines[1].top - lines[0].top, lines[0].fontSize);
  return (lines[0]?.fontSize ?? 10) * 1.5;
}

export interface PageHit {
  key: string;
  label: string;
  editable: boolean;
  /** Pending edit number, when the paragraph has one. */
  number?: number;
}

export function DocumentPages({
  pdf, pageCount, pageSizes, scale, filename, layout, readOnlyLines, hits, pending, decisions, selectedKey, editing, gutter,
  fieldToken, interactive, pageLabel, fieldLabel, armedHint, gutterCopy, onSelect, onStart, onChange, onClose, onDisarm, onTabAway, onVisiblePage, pagesRef,
  before, textOf, touch = false, pageAfter,
}: {
  pdf: PdfDocumentLike | null;
  pageCount: number;
  /** Each page's size in points. */
  pageSizes: ReadonlyArray<{ width: number; height: number }>;
  /** CSS pixels per point. */
  scale: number;
  filename: string;
  layout: DocumentLayout | null;
  readOnlyLines: ReadonlyMap<string, LocatedLine[]>;
  /** The paragraphs and read-only items with hit areas, in reading order. */
  hits: ReadonlyArray<PageHit>;
  pending: ReadonlyArray<DocumentPendingEdit>;
  decisions: ReadonlyMap<string, "paint" | "pending">;
  selectedKey: string | null;
  editing: DocumentEditing;
  /** There is room for cards in the right margin. */
  gutter: boolean;
  fieldToken: string;
  /** False after a save, while the pages only show what was saved. */
  interactive: boolean;
  pageLabel: (page: number) => string;
  fieldLabel: (key: string) => string;
  armedHint: string;
  gutterCopy: { badge: (number: number) => string; changeTo: string; note: string };
  onSelect: (key: string, viaKeyboard: boolean) => void;
  onStart: () => void;
  onChange: (key: string, text: string) => void;
  onClose: () => void;
  onDisarm: () => void;
  onTabAway: (key: string) => void;
  onVisiblePage: (page: number) => void;
  pagesRef?: (element: HTMLDivElement | null) => void;
  /** Shown above the pages (the saved status, canvas W10). */
  before?: ReactNode;
  /** A paragraph's current text: its pending edit, else the file's text. */
  textOf: (key: string) => string;
  /** Phones: a tap opens the paragraph's sheet, with 44 px touch areas (canvas WP1). */
  touch?: boolean;
  /** Phones: the page's actions and changes under it (修改第 N 页, 引用这一页). */
  pageAfter?: (page: number) => ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const quietRef = useRef(false);
  const pendingByKey = new Map(pending.map((edit) => [edit.key, edit]));
  const linesFor = (key: string) => layout?.paragraphs.get(key)?.lines ?? readOnlyLines.get(key) ?? [];

  // The page most in view, for the toolbar, the outline and 本页内容.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof IntersectionObserver === "undefined") return;
    const ratios = new Map<number, number>();
    const observer = new IntersectionObserver((entries) => {
      for (const entry of entries) ratios.set(Number((entry.target as HTMLElement).dataset.page), entry.intersectionRatio);
      let best = 0;
      let bestRatio = 0;
      for (const [page, ratio] of ratios) if (ratio > bestRatio) { best = page; bestRatio = ratio; }
      if (best) onVisiblePage(best);
    }, { threshold: [0, 0.25, 0.5, 0.75, 1] });
    container.querySelectorAll<HTMLElement>("[data-page]").forEach((page) => observer.observe(page));
    return () => observer.disconnect();
  }, [onVisiblePage, pageCount, scale]);

  const focusHitQuietly = (key: string) => {
    const hit = containerRef.current?.querySelector<HTMLElement>(`[data-hit-key="${CSS.escape(key)}"][data-first]`);
    if (!hit) return;
    quietRef.current = true;
    hit.focus();
    quietRef.current = false;
  };

  const pages = Array.from({ length: pageCount }, (_, index) => index);
  const editingKey = editing?.key ?? null;
  const editingLines = editingKey ? linesFor(editingKey) : [];
  const editingPage = editingLines[0]?.page;
  return (
    <div ref={(element) => { containerRef.current = element; pagesRef?.(element); }} className={styles.pages} data-gutter={gutter || undefined} data-touch={touch || undefined}
      style={{ ["--page-width" as string]: `${(pageSizes[0]?.width ?? 595) * scale}px` }}>
      {before}
      {pages.map((index) => {
        const size = pageSizes[index] ?? pageSizes[0] ?? { width: 595, height: 842 };
        const width = Math.round(size.width * scale);
        const height = Math.round(size.height * scale);
        const marks: ReactNode[] = [];
        const touchTargets: Array<{ hit: PageHit; rect: Rect; first: boolean; selected: boolean; pending: boolean }> = [];
        for (const hit of hits) {
          const all = linesFor(hit.key);
          const here = linesOnPage(all, index);
          if (!here.length) continue;
          const first = all[0]?.page === index;
          const edit = pendingByKey.get(hit.key);
          const decision = edit ? decisions.get(hit.key) ?? "pending" : null;
          const layoutRoom = layout?.paragraphs.get(hit.key)?.room ?? 0;
          const last = here[here.length - 1];
          const rect = rectOf(here, scale, decision === "paint" ? last.x1 + layoutRoom : 0);
          const selected = selectedKey === hit.key;
          const armedHere = editing?.key === hit.key && editing.armed;
          if (touch) {
            // The outline is drawn where the paragraph is; the button gets its touch area below.
            marks.push(
              <span key={`mark-${hit.key}`} className={styles.hit} aria-hidden="true" style={{ ...rect, pointerEvents: "none" }}
                data-editable={(hit.editable && interactive) || undefined}
                data-pending={edit ? true : undefined}
                data-selected={selected || undefined} />,
            );
            touchTargets.push({ hit, rect, first, selected, pending: Boolean(edit) });
          } else marks.push(
            <button key={`hit-${hit.key}`} type="button" className={styles.hit}
              style={rect}
              data-hit-key={hit.key}
              data-first={first || undefined}
              data-editable={(hit.editable && interactive) || undefined}
              data-locked={!hit.editable || undefined}
              data-pending={edit ? true : undefined}
              data-selected={selected || undefined}
              tabIndex={!interactive || !first || armedHere ? -1 : 0}
              aria-hidden={!first || armedHere || undefined}
              aria-label={hit.label}
              aria-pressed={first ? selected : undefined}
              disabled={!interactive}
              onFocus={(event) => {
                if (quietRef.current || !first) return;
                let keyboard = false;
                try { keyboard = event.currentTarget.matches(":focus-visible"); } catch { keyboard = false; }
                if (keyboard && !selected) onSelect(hit.key, true);
              }}
              // While the selection's field holds focus, a press on the paragraph keeps it there, so the
              // second click edits instead of blurring the field and dropping the selection.
              onMouseDown={(event) => { if (armedHere) event.preventDefault(); }}
              onClick={() => {
                if (!selected) onSelect(hit.key, false);
                else if (hit.editable) onStart();
              }}
            />,
          );
          if (first && hit.number) {
            // On a phone the number sits in the left margin as it does on desktops.
            marks.push(<span key={`num-${hit.key}`} className={styles.number} aria-hidden="true" style={{ left: rect.left - 26, top: rect.top + 2 }}>{hit.number}</span>);
          }
          // The painted text shows except while the field itself is shown over the paragraph.
          if (edit && decision === "paint" && !(editing?.key === hit.key && !editing.armed)) {
            const fontSize = last.fontSize * scale;
            marks.push(
              <PaintedText key={`paint-${hit.key}`} rect={rect} fontSize={fontSize} lineHeight={linePitch(here) * scale}
                lines={here.length} text={edit.text.split(fieldToken).join("")} />,
            );
          }
          if (edit && decision === "pending" && first && gutter && hit.number) {
            marks.push(
              <span key={`line-${hit.key}`} className={styles.gutterLine} aria-hidden="true"
                style={{ left: rect.left + rect.width, top: rect.top + 12, width: Math.max(12, width + 24 - rect.left - rect.width) }} />,
              <div key={`card-${hit.key}`} className={styles.gutterCard} role="note" aria-label={gutterCopy.badge(hit.number)}
                style={{ left: width + 24, top: rect.top }}>
                <span className={styles.gutterHead}>
                  <span className="ui-badge ui-badge--sm ui-badge--warning">{gutterCopy.badge(hit.number)}</span>
                  <span>{gutterCopy.changeTo}</span>
                </span>
                <span>{edit.text.split(fieldToken).join("")}</span>
                <span className={styles.gutterNote}>{gutterCopy.note}</span>
              </div>,
            );
          }
        }
        if (touch) {
          const areas = touchRects(touchTargets.map((target) => target.rect), { width, height });
          touchTargets.forEach((target, position) => {
            marks.push(
              <button key={`tap-${target.hit.key}`} type="button" className={styles.touchHit} style={areas[position]}
                data-hit-key={target.hit.key}
                data-first={target.first || undefined}
                tabIndex={interactive && target.first ? 0 : -1}
                aria-hidden={!target.first || undefined}
                aria-label={target.hit.label}
                aria-haspopup="dialog"
                disabled={!interactive}
                onClick={() => onSelect(target.hit.key, false)} />,
            );
          });
        }
        const page = (
          <div key={index} className={styles.page} data-page={index + 1} style={{ width, height }}
            aria-label={pageAfter ? undefined : pageLabel(index + 1)} role={pageAfter ? undefined : "group"}>
            <LazyPdfPageCanvas pdf={pdf} pageNumber={index + 1} width={width} aspectRatio={size.width / size.height}
              label={`${filename} · ${pageLabel(index + 1)}`} className={styles.pageCanvas} />
            <div className={styles.overlay}>
              {marks}
              {editingKey && editingPage === index && interactive ? (
                <ParagraphField key={editingKey} lines={linesOnPage(editingLines, index)} scale={scale}
                  text={textOf(editingKey)}
                  armed={Boolean(editing?.armed)} label={fieldLabel(editingKey)} hint={armedHint}
                  onStart={onStart}
                  onChange={(text) => onChange(editingKey, text)}
                  onClose={onClose}
                  onDisarm={onDisarm}
                  onTabAway={() => { focusHitQuietly(editingKey); onTabAway(editingKey); }} />
              ) : null}
            </div>
          </div>
        );
        if (!pageAfter) return page;
        return (
          <section key={index} className={styles.phonePage} aria-label={pageLabel(index + 1)} style={{ width }}>
            {page}
            {pageAfter(index + 1)}
          </section>
        );
      })}
    </div>
  );
}

/**
 * New text painted over the paragraph's old lines. The browser's font is not the document's, so the
 * text is kept inside the paragraph's lines: one line is narrowed to its width, several lines get a
 * smaller size until they fit; the save's new render shows the real layout (canvas GPanelKeys note).
 */
function PaintedText({ rect, fontSize, lineHeight, lines, text }: {
  rect: Rect;
  fontSize: number;
  lineHeight: number;
  lines: number;
  text: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const single = lines <= 1;
  useLayoutEffect(() => {
    const box = ref.current;
    if (!box) return;
    const inner = box.firstElementChild as HTMLElement | null;
    if (!inner) return;
    inner.style.transform = "";
    box.style.fontSize = `${fontSize}px`;
    if (single) {
      const ratio = box.clientWidth / Math.max(1, inner.scrollWidth);
      if (ratio < 1) inner.style.transform = `scaleX(${Math.max(0.6, ratio)})`;
      return;
    }
    let size = fontSize;
    for (let step = 0; step < 8 && box.scrollHeight > box.clientHeight + 1; step += 1) {
      size *= 0.94;
      box.style.fontSize = `${size}px`;
    }
  }, [fontSize, single, text]);
  return (
    <div ref={ref} className={styles.painted} aria-hidden="true" data-painted="paragraph"
      style={{ ...rect, fontSize, lineHeight: `${lineHeight}px`, whiteSpace: single ? "pre" : "pre-wrap", overflow: "hidden" }}>
      <span style={{ display: "inline-block", transformOrigin: "left center" }}>{text}</span>
    </div>
  );
}

/** The editor's field over a paragraph: hidden while armed, shown while editing (design §8.3, canvas W1). */
function ParagraphField({ lines, scale, text, armed, label, hint, onStart, onChange, onClose, onDisarm, onTabAway }: {
  lines: ReadonlyArray<LocatedLine>;
  scale: number;
  /** The paragraph's current text: its pending edit, else the file's text. */
  text: string;
  armed: boolean;
  label: string;
  hint: string;
  onStart: () => void;
  onChange: (text: string) => void;
  onClose: () => void;
  onDisarm: () => void;
  onTabAway: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const hintId = useRef(`doc-field-hint-${Math.random().toString(36).slice(2)}`).current;
  const fontSize = Math.max(MIN_FIELD_FONT, (lines[lines.length - 1]?.fontSize ?? 10) * scale);
  const rect = rectOf(lines, scale);
  const value = text;
  // Focus on opening, caret at the end: an input method can start composing before the next frame.
  useLayoutEffect(() => {
    const field = ref.current;
    if (!field) return;
    field.focus({ preventScroll: true });
    field.setSelectionRange(field.value.length, field.value.length);
  }, []);
  useLayoutEffect(() => {
    const field = ref.current;
    if (!field || armed) return;
    field.style.height = "auto";
    field.style.height = `${Math.max(rect.height, field.scrollHeight)}px`;
  }, [armed, rect.height, value]);
  return (
    <>
      <textarea
        ref={ref}
        className={styles.field}
        data-armed={armed || undefined}
        aria-label={label}
        aria-describedby={armed ? hintId : undefined}
        rows={1}
        value={value}
        style={{ left: rect.left, top: rect.top, width: Math.max(rect.width, 120), minHeight: rect.height, fontSize, lineHeight: `${Math.max(fontSize * 1.35, linePitch(lines) * scale)}px` }}
        onChange={(event) => onChange(event.target.value)}
        onBlur={() => (armed ? onDisarm() : onClose())}
        onCompositionStart={() => { if (armed) onStart(); }}
        onBeforeInput={() => { if (armed) onStart(); }}
        onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
          if (isComposing(event)) return;
          if (armed) {
            if (event.metaKey || event.ctrlKey || event.altKey) return;
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              onStart();
            } else if (event.key === "Tab") {
              onTabAway();
            } else if (CARET_KEYS.has(event.key)) {
              onStart();
            } else if (event.key === "Backspace" || event.key === "Delete") {
              event.preventDefault();
            }
            return;
          }
          if (event.key === "Tab") {
            onTabAway();
            return;
          }
          // Enter finishes the paragraph; Shift+Enter breaks the line inside it (canvas W1).
          if ((event.key === "Enter" && !event.shiftKey) || event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onClose();
          }
        }}
      />
      <span id={hintId} className="sr-only">{hint}</span>
    </>
  );
}
