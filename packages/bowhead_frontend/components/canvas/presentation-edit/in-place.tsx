"use client";

/**
 * Editing on the slide itself (pptx-inline-edit tech-design §13.3): the page's text is read from
 * the render with its PDF fonts, pending text is painted over the old render in the look each
 * character had, and text boxes and table cells open their field where they are drawn.
 */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import type { ElementBox, InventoryElement, PendingEdit } from "@/lib/presentation-edit";
import {
  alignment,
  cellRect,
  fontLook,
  pageGlyphs,
  styledRuns,
  type PageGlyph,
  type ParagraphLayout,
  type PdfGlyphLike,
  type TableLayout,
  type TextLayout,
} from "@/lib/presentation-layout";
import type { PdfDocumentLike } from "./pdf-page";
import { isComposing } from "./keys";
import styles from "./presentation-editor.module.css";

export type Rgb = [number, number, number];

export interface PageLayout {
  page: number;
  glyphs: PageGlyph[];
  /** The render's colour at a point given in fractions of the page. */
  sample: (x: number, y: number) => Rgb;
}

/** A drawn stage page: its canvas and a stamp that changes with every draw. */
export interface DrawnPage {
  page: number;
  canvas: HTMLCanvasElement;
  stamp: number;
}

/** Reads the drawn page's text (with its PDF font names) and keeps its pixels for sampling colours. */
export function usePageLayout(pdf: PdfDocumentLike | null, drawn: DrawnPage | null): PageLayout | null {
  const [layout, setLayout] = useState<{ stamp: number; value: PageLayout } | null>(null);
  useEffect(() => {
    if (!pdf || !drawn) return;
    let cancelled = false;
    void (async () => {
      try {
        const page = await pdf.getPage(drawn.page);
        const content = await page.getTextContent();
        const items = content.items as PdfGlyphLike[];
        const fonts: Record<string, string> = {};
        for (const item of items) {
          if (!item.fontName || item.fontName in fonts) continue;
          try {
            const font = page.commonObjs?.get(item.fontName) as { name?: string } | undefined;
            fonts[item.fontName] = font?.name ?? "";
          } catch {
            fonts[item.fontName] = "";
          }
        }
        const { canvas } = drawn;
        const width = canvas.width;
        const height = canvas.height;
        let pixels: Uint8ClampedArray | null = null;
        try {
          pixels = canvas.getContext("2d")?.getImageData(0, 0, width, height).data ?? null;
        } catch {
          pixels = null;
        }
        if (cancelled) return;
        const sample = (x: number, y: number): Rgb => {
          if (!pixels || !width || !height) return [255, 255, 255];
          const px = Math.min(width - 1, Math.max(0, Math.round(x * width)));
          const py = Math.min(height - 1, Math.max(0, Math.round(y * height)));
          const at = (py * width + px) * 4;
          return [pixels[at], pixels[at + 1], pixels[at + 2]];
        };
        setLayout({ stamp: drawn.stamp, value: { page: drawn.page, glyphs: pageGlyphs(items, page.view, fonts), sample } });
      } catch {
        if (!cancelled) setLayout(null);
      }
    })();
    return () => { cancelled = true; };
  }, [pdf, drawn]);
  return layout && drawn && layout.stamp === drawn.stamp ? layout.value : null;
}

const distance = (a: Rgb, b: Rgb) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]);
const css = (color: Rgb) => `rgb(${color[0]}, ${color[1]}, ${color[2]})`;

function medianColor(colors: Rgb[]): Rgb {
  const channel = (index: 0 | 1 | 2) => {
    const values = colors.map((color) => color[index]).sort((a, b) => a - b);
    return values[Math.floor(values.length / 2)] ?? 255;
  };
  return [channel(0), channel(1), channel(2)];
}

function insideGlyph(x: number, y: number, glyphs: ReadonlyArray<PageGlyph>): boolean {
  return glyphs.some((glyph) => x >= glyph.left - 0.002 && x <= glyph.right + 0.002 && y >= glyph.top - glyph.size * 0.1 && y <= glyph.bottom + glyph.size * 0.3);
}

export interface Backdrop {
  rgb: Rgb;
  /** Three in four sampled points agree: the box sits on one flat colour. */
  solid: boolean;
}

/** The colour around a box, sampled on a ring `inset` inside it and away from text. */
export function backdrop(layout: PageLayout, rect: ElementBox, avoid: ReadonlyArray<PageGlyph>, inset?: number): Backdrop {
  const [left, top, width, height] = rect;
  const insetX = Math.min(inset ?? 0.004, width / 4);
  const insetY = Math.min(inset ?? 0.006, height / 4);
  const points: Array<[number, number]> = [];
  for (let i = 0; i < 6; i += 1) {
    const f = (i + 0.5) / 6;
    points.push([left + insetX, top + height * f], [left + width - insetX, top + height * f], [left + width * f, top + insetY], [left + width * f, top + height - insetY]);
  }
  const colors = points.filter(([x, y]) => !insideGlyph(x, y, avoid)).map(([x, y]) => layout.sample(x, y));
  if (colors.length < 4) return { rgb: [255, 255, 255], solid: false };
  const rgb = medianColor(colors);
  const near = colors.filter((color) => distance(color, rgb) <= 30).length / colors.length;
  return { rgb, solid: near >= 0.75 };
}

interface GlyphLook {
  family: string;
  weight: number;
  italic: boolean;
  color: string;
  /** A run's own background, such as a highlight. */
  background: string | null;
}

function glyphLook(layout: PageLayout, glyph: PageGlyph, ground: Rgb, latinFaces: ReadonlyArray<string> = []): GlyphLook {
  let ink: Rgb = [31, 42, 58];
  let farthest = -1;
  for (let i = 0; i < 10; i += 1) {
    for (let j = 0; j < 5; j += 1) {
      const color = layout.sample(glyph.left + (glyph.right - glyph.left) * (i + 0.5) / 10, glyph.top + (glyph.bottom - glyph.top) * (j + 0.5) / 5);
      const away = distance(color, ground);
      if (away > farthest) { farthest = away; ink = color; }
    }
  }
  const font = fontLook(glyph.font);
  // Latin text in a CJK run is drawn in the paragraph's Latin face, as the render does.
  const faces = [...new Set([...latinFaces, ...font.faces])];
  const family = latinFaces.length ? [...faces, font.family.slice(font.faces.join(", ").length + (font.faces.length ? 2 : 0))].filter(Boolean).join(", ") : font.family;
  return { family, weight: font.weight, italic: font.italic, color: css(ink), background: null };
}

/**
 * What is behind one character, such as a highlight: pdf.js merges runs of one font into one item,
 * so a highlight on part of an item is found per character, in the column the character occupies.
 */
let measurer: CanvasRenderingContext2D | null | undefined;

/** Where a character sits along its glyph's run, measured with the run's face (proportional text). */
function charSpan(glyph: PageGlyph, offset: number, family: string): [number, number] {
  const chars = [...glyph.text];
  if (measurer === undefined) measurer = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
  const span = glyph.right - glyph.left;
  if (!measurer) return [glyph.left + span * offset / chars.length, span / chars.length];
  measurer.font = `100px ${family}`;
  const total = measurer.measureText(glyph.text).width || 1;
  const before = measurer.measureText(chars.slice(0, offset).join("")).width;
  const own = measurer.measureText(chars[offset] ?? "").width;
  return [glyph.left + span * (before + own / 2) / total, span * own / total];
}

/** Colours sampled in the column one character occupies. */
function charSamples(layout: PageLayout, glyph: PageGlyph, offset: number, family: string): Rgb[] {
  const [x, width] = charSpan(glyph, offset, family);
  const colors: Rgb[] = [];
  for (let i = 0; i < 3; i += 1) {
    for (let j = 0; j < 6; j += 1) {
      colors.push(layout.sample(x + width * (i - 1) * 0.3, glyph.top + (glyph.bottom - glyph.top) * (j + 0.5) / 6));
    }
  }
  return colors;
}

function charBackground(samples: ReadonlyArray<Rgb>, ground: Rgb, ink: Rgb): Rgb | null {
  const buckets = new Map<string, { color: Rgb; count: number }>();
  for (const color of samples) {
    const key = color.map((channel) => channel >> 4).join(",");
    const entry = buckets.get(key);
    if (entry) entry.count += 1;
    else buckets.set(key, { color, count: 1 });
  }
  const most = [...buckets.values()].sort((a, b) => b.count - a.count)[0];
  // Strokes are thin: most of a character's column shows what is behind it.
  if (!most || most.count / samples.length < 0.3 || distance(most.color, ground) <= 60 || distance(most.color, ink) <= 60) return null;
  return most.color;
}

/**
 * A dense character (many strokes) hides most of a highlight behind it, so its own column may not
 * show the highlight as its main colour. Such a character takes a neighbour's highlight when that
 * colour still shows between its strokes.
 */
export function spreadBackgrounds(behind: ReadonlyArray<Rgb | null>, shows: (index: number, color: Rgb) => boolean): Array<Rgb | null> {
  const out = [...behind];
  let changed = true;
  while (changed) {
    changed = false;
    for (let i = 0; i < out.length; i += 1) {
      if (out[i]) continue;
      const neighbour = [out[i - 1], out[i + 1]].find((color): color is Rgb => Boolean(color) && shows(i, color as Rgb));
      if (neighbour) { out[i] = neighbour; changed = true; }
    }
  }
  return out;
}

function parseRgb(value: string): Rgb {
  const parts = value.match(/\d+/g)?.map(Number) ?? [31, 42, 58];
  return [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0];
}

function lookStyle(look: GlyphLook | undefined): CSSProperties {
  if (!look) return {};
  return {
    fontFamily: look.family,
    fontWeight: look.weight,
    fontStyle: look.italic ? "italic" : undefined,
    color: look.color,
    background: look.background ?? undefined,
  };
}

/** The look most of a paragraph's characters have: the field takes it while it is being edited. */
function majorityLook(paragraph: ParagraphLayout, looks: ReadonlyArray<GlyphLook>): GlyphLook | undefined {
  const weight = new Map<number, number>();
  paragraph.glyphs.forEach((glyph, index) => weight.set(index, (weight.get(index) ?? 0) + glyph.text.length));
  let best: number | undefined;
  let bestCount = -1;
  for (const [index, count] of weight) if (count > bestCount) { best = index; bestCount = count; }
  return best === undefined ? undefined : looks[best];
}

interface Geometry {
  left: number;
  top: number;
  width: number;
  minHeight: number;
  fontSize: number;
  lineHeight: number;
  align: "left" | "center" | "right";
}

function paragraphGeometry(element: InventoryElement, paragraph: ParagraphLayout, page: { width: number; height: number }): Geometry {
  const box = element.box ?? [paragraph.left, paragraph.top, paragraph.right - paragraph.left, paragraph.bottom - paragraph.top];
  const boxLeft = box[0];
  const boxRight = Math.max(box[0] + box[2], paragraph.right);
  const align = alignment(paragraph.glyphs, boxLeft, box[0] + box[2]);
  const fontSize = paragraph.size * page.height;
  const lineHeight = (paragraph.lineGap ?? paragraph.size * 1.2) * page.height;
  const left = (align === "left" ? paragraph.left : boxLeft) * page.width - 2;
  return {
    left,
    top: paragraph.top * page.height - (lineHeight - fontSize) / 2,
    width: boxRight * page.width - left + 2,
    minHeight: (paragraph.bottom - paragraph.top) * page.height + (lineHeight - fontSize) / 2,
    fontSize,
    lineHeight,
    align,
  };
}

function geometryStyle(geometry: Geometry): CSSProperties {
  return {
    left: geometry.left,
    top: geometry.top,
    width: geometry.width,
    minHeight: geometry.minHeight,
    fontSize: geometry.fontSize,
    lineHeight: `${geometry.lineHeight}px`,
    textAlign: geometry.align,
  };
}

export type InPlaceTarget =
  // `armed`: the element is selected and its field holds focus, hidden, so that typing and input
  // methods land in it from the first key; editing shows the same field (design §13.4).
  | { kind: "text"; key: string; paragraph: number; typed?: string; armed?: boolean }
  | { kind: "cell"; key: string; row: number; col: number; typed?: string; armed?: boolean }
  | { kind: "chart"; key: string };

export type CellMove = "up" | "down" | "next" | "previous";

/** What a field does while its element is selected but not being edited (design §13.4). */
interface ArmedProps {
  armed?: boolean;
  /** Typing, pasting, a composition or Enter on the hidden field: show it. */
  onStart: () => void;
  /** The hidden field lost focus: remove it, the selection stays. */
  onDisarm: () => void;
  /** Tab on the field: continue from the element's place among the hits. */
  onTabAway: () => void;
  /** The hidden field's description: how to start editing. */
  describedBy?: string;
}

const CARET_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]);

/**
 * Keys on a hidden field: Enter or Space starts editing, caret keys start editing and move the
 * caret (so a screen reader can read the text), Tab leaves, and deleting keys do nothing.
 */
function armedKeyDown(event: KeyboardEvent<HTMLElement>, armed: ArmedProps): boolean {
  if (!armed.armed) return false;
  if (isComposing(event) || event.metaKey || event.ctrlKey || event.altKey) return true;
  // Enter, or Space as on a button, starts editing without adding a line break or a space.
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    armed.onStart();
  } else if (event.key === "Tab") {
    armed.onTabAway();
  } else if (CARET_KEYS.has(event.key)) {
    armed.onStart();
  } else if (event.key === "Backspace" || event.key === "Delete") {
    event.preventDefault();
  }
  return true;
}

/** On opening: focus the field, add the character that opened it, and put the caret at the end. */
function useOpenField(ref: React.RefObject<HTMLTextAreaElement | HTMLInputElement | null>, typed: string | undefined, onChange: (value: string) => void) {
  const typedRef = useRef(typed);
  useLayoutEffect(() => {
    const field = ref.current;
    if (!field) return;
    field.focus();
    // The caret goes to the end at once: an input method can start composing before the next frame.
    field.setSelectionRange(field.value.length, field.value.length);
    const extra = typedRef.current;
    typedRef.current = undefined;
    if (extra) onChange(field.value + extra);
    const frame = requestAnimationFrame(() => {
      const end = field.value.length;
      field.setSelectionRange(end, end);
    });
    return () => cancelAnimationFrame(frame);
    // Runs once, when the field opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}

function TextField({ element, paragraph, value, typed, look, ground, geometry, label, onChange, onClose, ...armed }: ArmedProps & {
  element: InventoryElement;
  paragraph: ParagraphLayout;
  value: string;
  typed?: string;
  look: GlyphLook | undefined;
  ground: Backdrop;
  geometry: Geometry;
  label: string;
  onChange: (text: string) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const closedRef = useRef(false);
  useOpenField(ref, typed, onChange);
  const close = () => {
    if (closedRef.current) return;
    closedRef.current = true;
    onClose();
  };
  // Esc hides the field again and keeps it focused; the next edit closes it anew.
  useLayoutEffect(() => { if (armed.armed) closedRef.current = false; }, [armed.armed]);
  useLayoutEffect(() => {
    const field = ref.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.max(geometry.minHeight, field.scrollHeight)}px`;
  }, [geometry.minHeight, value]);
  return (
    <textarea
      ref={ref}
      className={styles.inPlaceField}
      data-key={element.key}
      data-paragraph={paragraph.index}
      data-armed={armed.armed || undefined}
      aria-label={label}
      aria-describedby={armed.armed ? armed.describedBy : undefined}
      rows={1}
      value={value}
      style={{ ...geometryStyle(geometry), ...lookStyle(look), background: css(ground.rgb), color: look?.color }}
      onChange={(event) => onChange(event.target.value)}
      onBlur={() => { if (armed.armed) armed.onDisarm(); else close(); }}
      onCompositionStart={() => { if (armed.armed) armed.onStart(); }}
      onBeforeInput={() => { if (armed.armed) armed.onStart(); }}
      onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
        if (armedKeyDown(event, armed) || isComposing(event)) return;
        // Tab leaves from the element's place among the hits, as it does from the selection.
        if (event.key === "Tab") {
          armed.onTabAway();
          return;
        }
        if (event.key === "Escape" || (event.key === "Enter" && (event.metaKey || event.ctrlKey))) {
          // Esc keeps what was typed and returns to the selection, as in slide tools.
          event.preventDefault();
          event.stopPropagation();
          close();
        }
      }}
    />
  );
}

function CellField({ value, typed, look, ground, rect, fontSize, align, padLeft, label, onChange, onMove, onClose, ...armed }: ArmedProps & {
  value: string;
  typed?: string;
  look: GlyphLook | undefined;
  ground: Backdrop;
  rect: ElementBox;
  fontSize: number;
  align: "left" | "center" | "right";
  padLeft: number;
  label: string;
  onChange: (text: string) => void;
  onMove: (move: CellMove) => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const movedRef = useRef(false);
  useOpenField(ref, typed, onChange);
  useLayoutEffect(() => { if (armed.armed) movedRef.current = false; }, [armed.armed]);
  return (
    <input
      ref={ref}
      className={styles.inPlaceField}
      aria-label={label}
      aria-describedby={armed.armed ? armed.describedBy : undefined}
      data-armed={armed.armed || undefined}
      value={value}
      style={{
        left: rect[0], top: rect[1], width: rect[2], height: rect[3],
        fontSize, textAlign: align, paddingLeft: align === "left" ? padLeft : 4, paddingRight: 4,
        ...lookStyle(look), background: css(ground.rgb), color: look?.color,
      }}
      onChange={(event) => onChange(event.target.value)}
      onBlur={() => {
        if (armed.armed) armed.onDisarm();
        else if (!movedRef.current) onClose();
      }}
      onCompositionStart={() => { if (armed.armed) armed.onStart(); }}
      onBeforeInput={() => { if (armed.armed) armed.onStart(); }}
      onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
        if (armedKeyDown(event, armed) || isComposing(event)) return;
        const input = event.currentTarget;
        const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
        const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
        let move: CellMove | null = null;
        if (event.key === "Enter") move = event.shiftKey ? "up" : "down";
        else if (event.key === "Tab") move = event.shiftKey ? "previous" : "next";
        else if (event.key === "ArrowUp") move = "up";
        else if (event.key === "ArrowDown") move = "down";
        else if (event.key === "ArrowLeft" && atStart) move = "previous";
        else if (event.key === "ArrowRight" && atEnd) move = "next";
        if (move) {
          event.preventDefault();
          movedRef.current = true;
          onMove(move);
          return;
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          movedRef.current = true;
          onClose();
        }
      }}
    />
  );
}

export interface InPlaceLayerProps {
  elements: ReadonlyArray<InventoryElement>;
  layout: PageLayout;
  texts: ReadonlyMap<string, TextLayout>;
  tables: ReadonlyMap<string, TableLayout>;
  pending: ReadonlyArray<PendingEdit>;
  page: { width: number; height: number };
  editing: InPlaceTarget | null;
  fieldToken: string;
  labelFor: (element: InventoryElement, target: InPlaceTarget) => string;
  onText: (element: InventoryElement, paragraph: number, text: string) => void;
  onCell: (element: InventoryElement, row: number, col: number, text: string) => void;
  onMove: (element: InventoryElement, row: number, col: number, move: CellMove) => void;
  onClose: () => void;
  onStart: () => void;
  onDisarm: () => void;
  onTabAway: () => void;
  /** Read with a selection's hidden field: how to start editing it. */
  armedHint: string;
}

/** Pending text painted over the old render, and the open field. */
export function InPlaceLayer({
  elements, layout, texts, tables, pending, page, editing, labelFor, onText, onCell, onMove, onClose, onStart, onDisarm, onTabAway, armedHint,
}: InPlaceLayerProps) {
  const hintId = useId();
  const armedProps = { armed: editing?.kind !== "chart" && Boolean(editing?.armed), onStart, onDisarm, onTabAway, describedBy: hintId };
  const byKey = useMemo(() => new Map(elements.map((element) => [element.key, element])), [elements]);
  // Looks are read from the render once per element and page draw.
  const textLooks = useMemo(() => {
    const out = new Map<string, { ground: Backdrop; looks: GlyphLook[][]; styles: Array<{ table: GlyphLook[]; chars: Array<number | null> }> }>();
    for (const [key, text] of texts) {
      const element = byKey.get(key);
      if (!element?.box) continue;
      // Sample a band just outside the text, away from any text on the page: inside the text's own
      // extent nearly every point falls on a glyph.
      const size = Math.max(...text.paragraphs.map((paragraph) => paragraph.size));
      const margin = Math.max(0.008, size * 0.35);
      const rect: ElementBox = [text.left - margin, text.top - margin, text.right - text.left + margin * 2, text.bottom - text.top + margin * 2];
      const ground = backdrop(layout, rect, layout.glyphs, margin / 2);
      const looks = text.paragraphs.map((paragraph) => {
        const latin = paragraph.glyphs.find((glyph) => /[A-Za-z0-9]/.test(glyph.text) && glyph.font);
        const latinFaces = latin ? fontLook(latin.font).faces : [];
        return paragraph.glyphs.map((glyph) => glyphLook(layout, glyph, ground.rgb, latinFaces));
      });
      // Each old character's look: its glyph's, plus what is behind that character.
      const styles = text.paragraphs.map((paragraph, p) => {
        const table: GlyphLook[] = [];
        const index = new Map<string, number>();
        const samples = paragraph.charGlyph.map((glyph, i) => {
          const offset = paragraph.charOffset[i];
          return glyph === null || offset === null ? null : charSamples(layout, paragraph.glyphs[glyph], offset, looks[p][glyph].family);
        });
        const behinds = spreadBackgrounds(
          paragraph.charGlyph.map((glyph, i) => {
            const own = samples[i];
            return glyph === null || !own ? null : charBackground(own, ground.rgb, parseRgb(looks[p][glyph].color));
          }),
          (i, color) => (samples[i]?.filter((sample) => distance(sample, color) <= 40).length ?? 0) >= 2,
        );
        const chars = paragraph.charGlyph.map((glyph, i) => {
          if (glyph === null) return null;
          const base = looks[p][glyph];
          const behind = behinds[i];
          const look = behind ? { ...base, background: css(behind) } : base;
          const id = `${glyph}|${look.background ?? ""}`;
          if (!index.has(id)) { index.set(id, table.length); table.push(look); }
          return index.get(id) ?? null;
        });
        return { table, chars };
      });
      out.set(key, { ground, looks, styles });
    }
    return out;
  }, [byKey, layout, texts]);
  const painted: React.ReactNode[] = [];
  for (const edit of pending) {
    const element = byKey.get(edit.key);
    if (!element) continue;
    if (edit.kind === "text") {
      // A hidden field (a selection) leaves the painted text in view.
      if (editing?.kind === "text" && !editing.armed && editing.key === edit.key && editing.paragraph === edit.paragraph) continue;
      const text = texts.get(edit.key);
      const paragraphIndex = text?.paragraphs.findIndex((item) => item.index === edit.paragraph) ?? -1;
      const looked = textLooks.get(edit.key);
      if (!text || paragraphIndex < 0 || !looked) continue;
      const paragraph = text.paragraphs[paragraphIndex];
      const looks = looked.looks[paragraphIndex];
      const style = looked.styles[paragraphIndex];
      const geometry = paragraphGeometry(element, paragraph, page);
      painted.push(
        <div key={`t:${edit.key}:${edit.paragraph}`} className={styles.painted} data-painted="text" data-draft={!looked.ground.solid || undefined} aria-hidden="true"
          style={{ ...geometryStyle(geometry), background: css(looked.ground.rgb) }}>
          {styledRuns(edit.expect, edit.text, style.chars).map((run, index) => (
            <span key={index} style={lookStyle(run.glyph === null ? majorityLook(paragraph, looks) : style.table[run.glyph])}>{run.text}</span>
          ))}
        </div>,
      );
    } else if (edit.kind === "cell") {
      if (editing?.kind === "cell" && !editing.armed && editing.key === edit.key && editing.row === edit.row && editing.col === edit.col) continue;
      const table = tables.get(edit.key);
      if (!table) continue;
      const cell = cellLook(layout, table, edit.row, edit.col, page);
      painted.push(
        <div key={`c:${edit.key}:${edit.row}:${edit.col}`} className={styles.painted} data-painted="cell" data-cell aria-hidden="true"
          style={{
            left: cell.rect[0], top: cell.rect[1], width: cell.rect[2], height: cell.rect[3], fontSize: cell.fontSize,
            lineHeight: `${cell.rect[3]}px`, textAlign: cell.align, paddingLeft: cell.align === "left" ? cell.padLeft : 4, paddingRight: 4,
            ...lookStyle(cell.look), background: css(cell.ground.rgb),
          }}>
          {edit.text}
        </div>,
      );
    }
  }

  let field: React.ReactNode = null;
  if (editing?.kind === "text") {
    const element = byKey.get(editing.key);
    const text = texts.get(editing.key);
    const paragraphIndex = text?.paragraphs.findIndex((item) => item.index === editing.paragraph) ?? -1;
    const looked = textLooks.get(editing.key);
    if (element && text && looked && paragraphIndex >= 0) {
      const paragraph = text.paragraphs[paragraphIndex];
      const edit = pending.find((item) => item.kind === "text" && item.key === editing.key && item.paragraph === editing.paragraph);
      const value = edit && edit.kind === "text" ? edit.text : element.paragraphs?.[editing.paragraph]?.text ?? "";
      field = (
        <TextField key={`${editing.key}:${editing.paragraph}`} element={element} paragraph={paragraph} value={value} typed={editing.typed}
          look={majorityLook(paragraph, looked.looks[paragraphIndex])} ground={looked.ground} geometry={paragraphGeometry(element, paragraph, page)}
          label={labelFor(element, editing)} onChange={(next) => onText(element, editing.paragraph, next)} onClose={onClose} {...armedProps} />
      );
    }
  } else if (editing?.kind === "cell") {
    const element = byKey.get(editing.key);
    const table = tables.get(editing.key);
    if (element && table) {
      const cell = cellLook(layout, table, editing.row, editing.col, page);
      const edit = pending.find((item) => item.kind === "cell" && item.key === editing.key && item.row === editing.row && item.col === editing.col);
      const value = edit && edit.kind === "cell" ? edit.text : element.rows?.[editing.row]?.[editing.col]?.text ?? "";
      // A small cell's field grows to readable text while it is open; the painted value keeps the rendered size.
      field = (
        <CellField key={`${editing.key}:${editing.row}:${editing.col}`} value={value} typed={editing.typed} look={cell.look} ground={cell.ground}
          rect={cell.rect} fontSize={Math.max(cell.fontSize, 13)} align={cell.align} padLeft={cell.padLeft} label={labelFor(element, editing)}
          onChange={(next) => onCell(element, editing.row, editing.col, next)}
          onMove={(move) => onMove(element, editing.row, editing.col, move)} onClose={onClose} {...armedProps} />
      );
    }
  }

  return (
    <>
      <div className={styles.paintedLayer}>{painted}</div>
      {field ? <div className={styles.fieldLayer}>{field}<span id={hintId} className="sr-only">{armedHint}</span></div> : null}
    </>
  );
}

interface CellLook {
  rect: ElementBox;
  ground: Backdrop;
  look: GlyphLook | undefined;
  fontSize: number;
  align: "left" | "center" | "right";
  padLeft: number;
}

/** Pixel rectangle and look of one cell, from its glyphs on the render (or its row's, when empty). */
function cellLook(layout: PageLayout, table: TableLayout, row: number, col: number, page: { width: number; height: number }): CellLook {
  const [left, top, width, height] = cellRect(table, row, col);
  const glyphs = table.cells[row]?.[col] ?? [];
  const reference = glyphs[0] ?? table.cells[row]?.flat()[0] ?? table.cells.flat(2)[0];
  const ground = backdrop(layout, [left, top, width, height], glyphs);
  const look = reference ? glyphLook(layout, reference, ground.rgb) : undefined;
  return {
    rect: [left * page.width, top * page.height, width * page.width, height * page.height],
    ground,
    look,
    fontSize: (reference?.size ?? 0.025) * page.height,
    align: alignment(glyphs, left, left + width),
    padLeft: glyphs.length ? Math.max(2, (Math.min(...glyphs.map((glyph) => glyph.left)) - left) * page.width) : 6,
  };
}

/** A chart's values beside the chart: chart values are not text on the page (design §13.3). */
export function ChartPopover({ label, left, top, width, title, meta, hint, doneLabel, onClose, onLeave, children }: {
  label: string;
  left: number;
  top: number;
  width: number;
  title: string;
  meta: string;
  hint: string;
  doneLabel: string;
  onClose: () => void;
  /** Focus moved out of the popover (Tab past 「完成」): close it without taking focus back. */
  onLeave: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [placedTop, setPlacedTop] = useState(top);
  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
  }, []);
  // Beside the chart, but kept inside the page: a chart low on the slide opens its data higher up,
  // and a tall one scrolls (its height is capped by the page). Re-placed when errors add lines.
  useLayoutEffect(() => {
    const node = ref.current;
    const parent = node?.offsetParent as HTMLElement | null | undefined;
    if (!node || !parent) return;
    const place = () => setPlacedTop(Math.max(8, Math.min(top, parent.clientHeight - node.offsetHeight - 8)));
    place();
    if (typeof ResizeObserver !== "function") return;
    const observer = new ResizeObserver(place);
    observer.observe(node);
    return () => observer.disconnect();
  }, [top]);
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (ref.current && event.target instanceof Node && !ref.current.contains(event.target)) onClose();
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [onClose]);
  return (
    <div ref={ref} role="dialog" aria-label={label} className={styles.chartPopover} style={{ left, top: placedTop, width }}
      onBlur={(event) => {
        const next = event.relatedTarget;
        if (next instanceof Node && !event.currentTarget.contains(next)) onLeave();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }}>
      <div className={styles.popoverHead}>
        <strong>{title}</strong>
        <span>{meta}</span>
      </div>
      {children}
      <p className={styles.panelHint}>{hint}</p>
      <div className={styles.popoverActions}>
        <Button size="sm" variant="primary" onClick={onClose}>{doneLabel}</Button>
      </div>
    </div>
  );
}
