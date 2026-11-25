/**
 * In-place editing of a PowerPoint result (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md).
 *
 * The server lists what a deck lets a person change (the inventory) and saves a list of edits as the
 * next version. This module holds the wire types, the two requests, and the pure editing model the
 * editor uses: pending edits, chart number parsing and display, re-applying edits to a newer version
 * (design §6.7) and the text-overflow check against a rendered page (design §6.8).
 */

import { appFetch } from "@/lib/base-path";
import type { ErrorDiagnostics } from "@/lib/error-presentation";

// ── Wire types ──────────────────────────────────────────────────────────────────────

export type PresentationElementKind = "text" | "table" | "chart" | "picture" | "smartart" | "ole" | "other";

export interface InventoryParagraph {
  text: string;
  mixed_style: boolean;
  fields: number;
}

export interface InventoryCell {
  text: string;
  editable: boolean;
  reason: string | null;
  mixed_style?: boolean;
}

export interface InventoryChartSeries {
  name: string;
  values: Array<number | null>;
  format: string | null;
}

export interface InventoryChart {
  type: string;
  categories: string[];
  series: InventoryChartSeries[];
}

/** `box` is `[left, top, width, height]` as fractions of the slide size. */
export type ElementBox = [number, number, number, number];

export interface InventoryElement {
  key: string;
  shape_id: number;
  name: string;
  kind: PresentationElementKind;
  box: ElementBox | null;
  editable: boolean;
  reason: string | null;
  paragraphs?: InventoryParagraph[];
  rows?: InventoryCell[][];
  /** A table's column widths and row heights, as shares of its box (they sum to 1). */
  col_widths?: number[];
  row_heights?: number[];
  chart?: InventoryChart;
}

export interface InventorySlide {
  slide_id: number;
  index: number;
  /** The text of the slide's title placeholder, when it has one. */
  title?: string | null;
  inherited_text: boolean;
  elements: InventoryElement[];
}

export type PresentationEditMode = "version" | "new_file";

/** A file this person already made by editing an upload (design §6.4). */
export interface EditedCopy {
  artifact_id: string;
  filename: string;
  version_no: number;
}

export interface PresentationInventory {
  artifact_id: string;
  version_no: number;
  edit_mode: PresentationEditMode;
  slide_width: number;
  slide_height: number;
  field_token: string;
  slides: InventorySlide[];
  /** The file as a chat reference names it (attachment checks compare these). */
  file: { filename: string; mime: string; size: number } | null;
  /** Uploads only: the name the first save gives the new file. */
  new_filename: string | null;
  edited_copies: EditedCopy[];
}

export interface LatestVersionFacts {
  artifact_id: string;
  version_no: number;
  created_at: string;
  produced_by: "agent" | "user";
  request_excerpt: string | null;
}

export type WireEdit =
  | { kind: "text"; key: string; paragraph: number; text: string; expect: string }
  | { kind: "cell"; key: string; row: number; col: number; text: string; expect: string }
  | {
      kind: "chart";
      key: string;
      expect: { categories: string[]; series: Array<{ name: string; values: Array<number | null> }> };
      series: Array<{ name: string; values: Array<number | null> }>;
    };

export interface PresentationSaveResult {
  artifact_id: string;
  version_no: number;
  filename: string;
  new_file: boolean;
}

export interface RefusedEdit {
  index: number | null;
  code: string;
}

/** A refused request, with the server's stable code and the facts the editor shows. */
export class PresentationEditError extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly latest: LatestVersionFacts | null;
  readonly refused: RefusedEdit[];
  readonly reason: string | null;
  readonly diagnosticId: string | null;
  readonly requestId: string | null;

  constructor(code: string, options: {
    status?: number | null;
    latest?: LatestVersionFacts | null;
    refused?: RefusedEdit[];
    reason?: string | null;
    diagnosticId?: string | null;
    requestId?: string | null;
  } = {}) {
    super(code);
    this.name = "PresentationEditError";
    this.code = code;
    this.status = options.status ?? null;
    this.latest = options.latest ?? null;
    this.refused = options.refused ?? [];
    this.reason = options.reason ?? null;
    this.diagnosticId = options.diagnosticId ?? null;
    this.requestId = options.requestId ?? null;
  }

  /** Diagnostics for `ErrorNotice`: status, code and the server's `pe_…` reference, never the cause. */
  get diagnostics(): ErrorDiagnostics | undefined {
    if (this.status === null && !this.diagnosticId) return undefined;
    return {
      schemaVersion: 1,
      disclosure: "authenticated",
      ...(this.status !== null ? { status: this.status } : {}),
      code: this.code,
      ...(this.diagnosticId && /^[a-z][a-z0-9_]{0,63}$/.test(this.diagnosticId) ? { diagnosticCode: this.diagnosticId } : {}),
      ...(this.requestId && /^[0-9a-f]{32}$/.test(this.requestId) ? { requestId: this.requestId } : {}),
      ...(this.status === null ? { serverRequestIdUnavailable: true as const } : {}),
      observedAt: new Date().toISOString(),
    };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseLatest(value: unknown): LatestVersionFacts | null {
  if (!isRecord(value) || typeof value.artifact_id !== "string" || typeof value.version_no !== "number") return null;
  return {
    artifact_id: value.artifact_id,
    version_no: value.version_no,
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    produced_by: value.produced_by === "user" ? "user" : "agent",
    request_excerpt: typeof value.request_excerpt === "string" ? value.request_excerpt : null,
  };
}

async function errorFor(response: Response): Promise<PresentationEditError> {
  const requestId = response.headers.get("x-request-id");
  let error: Record<string, unknown> = {};
  try {
    const body = await response.json() as unknown;
    const detail = isRecord(body) ? body.detail : null;
    if (isRecord(detail) && isRecord(detail.error)) error = detail.error;
  } catch { /* A non-JSON refusal keeps only its status. */ }
  const code = typeof error.code === "string" && error.code ? error.code : `http_${response.status}`;
  const refused = Array.isArray(error.edits)
    ? error.edits.flatMap((item): RefusedEdit[] => isRecord(item) && typeof item.code === "string"
      ? [{ index: typeof item.index === "number" ? item.index : null, code: item.code }]
      : [])
    : [];
  return new PresentationEditError(code, {
    status: response.status,
    latest: parseLatest(error.latest),
    refused,
    reason: typeof error.reason === "string" ? error.reason : null,
    diagnosticId: typeof error.diagnostic_id === "string" ? error.diagnostic_id : null,
    requestId,
  });
}

async function post(path: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await appFetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new PresentationEditError("network");
  }
  if (!response.ok) throw await errorFor(response);
  try {
    return await response.json() as unknown;
  } catch {
    throw new PresentationEditError("response_invalid", { status: response.status });
  }
}

function sessionPath(sessionId: string, tail: string): string {
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}/results/${tail}`;
}

/** A table's column widths or row heights as shares of its box. */
function isShares(value: unknown): value is number[] {
  return Array.isArray(value) && value.length > 0 && value.every((item) => typeof item === "number" && Number.isFinite(item) && item >= 0 && item <= 1);
}

function isBox(value: unknown): value is ElementBox {
  return Array.isArray(value) && value.length === 4 && value.every((item) => typeof item === "number" && Number.isFinite(item));
}

function parseInventory(value: unknown): PresentationInventory {
  if (
    !isRecord(value)
    || typeof value.artifact_id !== "string"
    || typeof value.version_no !== "number"
    || (value.edit_mode !== "version" && value.edit_mode !== "new_file")
    || !Array.isArray(value.slides)
  ) throw new PresentationEditError("response_invalid");
  const slides = value.slides.map((slide): InventorySlide => {
    if (!isRecord(slide) || typeof slide.slide_id !== "number" || typeof slide.index !== "number" || !Array.isArray(slide.elements)) {
      throw new PresentationEditError("response_invalid");
    }
    const elements = slide.elements.flatMap((element): InventoryElement[] => {
      if (!isRecord(element) || typeof element.key !== "string" || typeof element.kind !== "string") return [];
      return [{
        key: element.key,
        shape_id: typeof element.shape_id === "number" ? element.shape_id : 0,
        name: typeof element.name === "string" ? element.name : "",
        kind: element.kind as PresentationElementKind,
        box: isBox(element.box) ? element.box : null,
        editable: element.editable === true,
        reason: typeof element.reason === "string" ? element.reason : null,
        ...(Array.isArray(element.paragraphs) ? { paragraphs: element.paragraphs as InventoryParagraph[] } : {}),
        ...(Array.isArray(element.rows) ? { rows: element.rows as InventoryCell[][] } : {}),
        ...(isShares(element.col_widths) ? { col_widths: element.col_widths } : {}),
        ...(isShares(element.row_heights) ? { row_heights: element.row_heights } : {}),
        ...(isRecord(element.chart) ? { chart: element.chart as unknown as InventoryChart } : {}),
      }];
    });
    return {
      slide_id: slide.slide_id,
      index: slide.index,
      ...(typeof slide.title === "string" ? { title: slide.title } : {}),
      inherited_text: slide.inherited_text === true,
      elements,
    };
  });
  return {
    artifact_id: value.artifact_id,
    version_no: value.version_no,
    edit_mode: value.edit_mode,
    slide_width: typeof value.slide_width === "number" ? value.slide_width : 1,
    slide_height: typeof value.slide_height === "number" ? value.slide_height : 1,
    field_token: typeof value.field_token === "string" ? value.field_token : "\ufffc",
    slides,
    file: isRecord(value.file) && typeof value.file.filename === "string" && typeof value.file.mime === "string" && typeof value.file.size === "number"
      ? { filename: value.file.filename, mime: value.file.mime, size: value.file.size }
      : null,
    new_filename: typeof value.new_filename === "string" ? value.new_filename : null,
    edited_copies: Array.isArray(value.edited_copies)
      ? value.edited_copies.flatMap((item): EditedCopy[] => isRecord(item) && typeof item.artifact_id === "string" && typeof item.filename === "string"
        ? [{ artifact_id: item.artifact_id, filename: item.filename, version_no: typeof item.version_no === "number" ? item.version_no : 1 }]
        : [])
      : [],
  };
}

export async function fetchPresentationInventory({
  sessionId, artifactId, signal,
}: { sessionId: string; artifactId: string; signal?: AbortSignal }): Promise<PresentationInventory> {
  return parseInventory(await post(sessionPath(sessionId, "presentation-edit/inventory"), { artifact_id: artifactId }, signal));
}

export async function savePresentationEdits({
  sessionId, artifactId, edits, signal,
}: { sessionId: string; artifactId: string; edits: WireEdit[]; signal?: AbortSignal }): Promise<PresentationSaveResult> {
  const value = await post(sessionPath(sessionId, "presentation-edits"), { artifact_id: artifactId, edits }, signal);
  if (!isRecord(value) || typeof value.artifact_id !== "string" || typeof value.version_no !== "number" || typeof value.filename !== "string") {
    throw new PresentationEditError("response_invalid");
  }
  return { artifact_id: value.artifact_id, version_no: value.version_no, filename: value.filename, new_file: value.new_file === true };
}

// ── Pending edits ───────────────────────────────────────────────────────────────────

export type PendingEdit =
  | { kind: "text"; key: string; slideId: number; paragraph: number; expect: string; text: string }
  | { kind: "cell"; key: string; slideId: number; row: number; col: number; expect: string; text: string }
  | {
      kind: "chart";
      key: string;
      slideId: number;
      expect: InventoryChart;
      /** New values per series, in the inventory's series order. */
      values: Array<Array<number | null>>;
    };

export function pendingEditId(edit: Pick<PendingEdit, "kind" | "key"> & { paragraph?: number; row?: number; col?: number }): string {
  if (edit.kind === "text") return `text:${edit.key}:${edit.paragraph}`;
  if (edit.kind === "cell") return `cell:${edit.key}:${edit.row}:${edit.col}`;
  return `chart:${edit.key}`;
}

/** Pending edits in the order they were last changed; the last one is what ⌘Z undoes. */
export type PendingEdits = ReadonlyArray<PendingEdit>;

function chartValuesEqual(a: ReadonlyArray<ReadonlyArray<number | null>>, b: ReadonlyArray<ReadonlyArray<number | null>>): boolean {
  return a.length === b.length && a.every((row, index) => row.length === b[index].length
    && row.every((value, column) => value === b[index][column]
      || (value !== null && b[index][column] !== null && Math.abs(value - (b[index][column] as number)) <= 1e-12 * Math.max(1, Math.abs(value)))));
}

/** True when the edit would not change anything (the person typed the original back). */
export function isNoop(edit: PendingEdit): boolean {
  if (edit.kind === "chart") return chartValuesEqual(edit.values, edit.expect.series.map((series) => series.values));
  return edit.text === edit.expect;
}

/** Put `edit` at the end (most recent); drop it when it no longer changes anything. */
/** Adds or updates a change in place, so its number (its first edit's order) does not move. */
export function upsertPendingEdit(edits: PendingEdits, edit: PendingEdit): PendingEdit[] {
  const id = pendingEditId(edit);
  const index = edits.findIndex((item) => pendingEditId(item) === id);
  if (isNoop(edit)) return edits.filter((_, position) => position !== index);
  if (index < 0) return [...edits, edit];
  const next = [...edits];
  next[index] = edit;
  return next;
}

export function removePendingEdit(edits: PendingEdits, id: string): PendingEdit[] {
  return edits.filter((item) => pendingEditId(item) !== id);
}

export function toWireEdits(edits: PendingEdits): WireEdit[] {
  return edits.map((edit): WireEdit => {
    if (edit.kind === "text") return { kind: "text", key: edit.key, paragraph: edit.paragraph, text: edit.text, expect: edit.expect };
    if (edit.kind === "cell") return { kind: "cell", key: edit.key, row: edit.row, col: edit.col, text: edit.text, expect: edit.expect };
    return {
      kind: "chart",
      key: edit.key,
      expect: {
        categories: edit.expect.categories,
        series: edit.expect.series.map((series) => ({ name: series.name, values: series.values })),
      },
      series: edit.expect.series.map((series, index) => ({ name: series.name, values: edit.values[index] })),
    };
  });
}

/** Number of field tokens (slide numbers, dates) in a text. */
export function countFieldTokens(text: string, token: string): number {
  return token ? text.split(token).length - 1 : 0;
}

// ── Chart numbers ───────────────────────────────────────────────────────────────────

export interface NumberFormatShape {
  percent: boolean;
  decimals: number;
  grouping: boolean;
  general: boolean;
}

/** Reads the parts of an Excel number format the editor needs: percent, decimals, thousands grouping. */
export function numberFormatShape(format: string | null | undefined): NumberFormatShape {
  const section = (format ?? "").split(";")[0].replace(/"[^"]*"|\\.|\[[^\]]*\]/g, "");
  if (!section || /^general$/i.test(section.trim())) return { percent: false, decimals: 0, grouping: false, general: true };
  const decimalsMatch = section.match(/\.([0#?]+)/);
  return {
    percent: section.includes("%"),
    decimals: decimalsMatch ? decimalsMatch[1].length : 0,
    grouping: /[0#?],[0#?]/.test(section),
    general: false,
  };
}

/** A chart value as the deck shows it: grouping, decimals and percent from the series format. */
export function formatChartValue(value: number | null, format: string | null | undefined, locale: string): string {
  if (value === null || !Number.isFinite(value)) return "";
  const shape = numberFormatShape(format);
  if (shape.general) {
    return new Intl.NumberFormat(locale, { maximumFractionDigits: 10, useGrouping: false }).format(value);
  }
  const shown = shape.percent ? value * 100 : value;
  const text = new Intl.NumberFormat(locale, {
    minimumFractionDigits: shape.decimals,
    maximumFractionDigits: Math.max(shape.decimals, shape.percent ? 2 : 0),
    useGrouping: shape.grouping,
  }).format(shown);
  return shape.percent ? `${text}%` : text;
}

function separators(locale: string): { group: string; decimal: string } {
  const parts = new Intl.NumberFormat(locale).formatToParts(12345.6);
  return {
    group: parts.find((part) => part.type === "group")?.value ?? ",",
    decimal: parts.find((part) => part.type === "decimal")?.value ?? ".",
  };
}

/**
 * Parses what a person typed into a chart value. Empty means "no value". A percent series reads
 * 「91%」 and 「91」 alike as 0.91 (the field shows 91%). Returns `undefined` when it is not a number.
 */
export function parseChartValue(input: string, format: string | null | undefined, locale: string): number | null | undefined {
  let text = input.trim().replace(/[\s  ']/g, "");
  if (!text) return null;
  const shape = numberFormatShape(format);
  const hadPercent = text.endsWith("%");
  if (hadPercent) text = text.slice(0, -1);
  if (hadPercent && !shape.percent && !shape.general) return undefined;
  const { group, decimal } = separators(locale);
  // Accept both the locale's separators and the plain "1,250.5" form.
  let normalized = text;
  if (decimal === "," && /^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d+$/.test(text)) {
    normalized = text.replace(/\./g, "").replace(",", ".");
  } else if (/^-?\d{1,3}(,\d{3})+(\.\d+)?$/.test(text)) {
    normalized = text.replace(/,/g, "");
  } else if (group !== "," && group !== "." && text.includes(group)) {
    normalized = text.split(group).join("");
  }
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(normalized)) return undefined;
  const value = Number(normalized);
  if (!Number.isFinite(value)) return undefined;
  return shape.percent || hadPercent ? value / 100 : value;
}

/**
 * Where else on the slide the value shows as text: a number repeated in a text element does not
 * follow the chart. Matches the value as shown with and without thousands grouping, as a whole number.
 */
export function findRepeatedValue(value: number, format: string | null | undefined, texts: ReadonlyArray<{ key: string; text: string }>): string[] {
  const shape = numberFormatShape(format);
  const candidates = new Set<string>();
  for (const grouping of [true, false]) {
    const shown = shape.percent ? value * 100 : value;
    const decimals = shape.general ? undefined : shape.decimals;
    const text = new Intl.NumberFormat("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals ?? 6,
      useGrouping: grouping,
    }).format(shown);
    candidates.add(shape.percent ? `${text}%` : text);
  }
  const isDigit = (char: string | undefined) => char !== undefined && char >= "0" && char <= "9";
  // A neighbouring digit, or a separator followed by a digit, means the match is part of a longer number.
  const continues = (char: string | undefined, next: string | undefined) => isDigit(char) || ((char === "." || char === ",") && isDigit(next));
  const wholeNumberAt = (text: string, start: number, length: number) => (
    !continues(text[start - 1], text[start - 2]) && !continues(text[start + length], text[start + length + 1])
  );
  return texts.filter((item) => [...candidates].some((candidate) => {
    if (candidate.length < 2) return false;
    for (let start = item.text.indexOf(candidate); start >= 0; start = item.text.indexOf(candidate, start + 1)) {
      if (wholeNumberAt(item.text, start, candidate.length)) return true;
    }
    return false;
  })).map((item) => item.key);
}

// ── Re-applying after a refused save (design §6.7) ──────────────────────────────────

export interface ReapplyRow {
  edit: PendingEdit;
  /** The edit's target is unchanged in the newer version. */
  applicable: boolean;
  /** What the newer version holds when it differs. */
  latestText: string | null;
}

function elementByKey(inventory: PresentationInventory, key: string): InventoryElement | undefined {
  for (const slide of inventory.slides) {
    const found = slide.elements.find((element) => element.key === key);
    if (found) return found;
  }
  return undefined;
}

function sameChart(a: InventoryChart, b: InventoryChart): boolean {
  return a.categories.length === b.categories.length
    && a.categories.every((category, index) => category === b.categories[index])
    && a.series.length === b.series.length
    && a.series.every((series, index) => series.name === b.series[index].name)
    && chartValuesEqual(a.series.map((series) => series.values), b.series.map((series) => series.values));
}

/** Classifies each pending edit against the newer version's inventory. */
export function matchPendingEdits(edits: PendingEdits, latest: PresentationInventory): ReapplyRow[] {
  return edits.map((edit): ReapplyRow => {
    const element = elementByKey(latest, edit.key);
    if (!element || !element.editable) return { edit, applicable: false, latestText: null };
    if (edit.kind === "text") {
      const paragraph = element.paragraphs?.[edit.paragraph];
      if (!paragraph) return { edit, applicable: false, latestText: null };
      return { edit, applicable: paragraph.text === edit.expect, latestText: paragraph.text === edit.expect ? null : paragraph.text };
    }
    if (edit.kind === "cell") {
      const cell = element.rows?.[edit.row]?.[edit.col];
      if (!cell || !cell.editable) return { edit, applicable: false, latestText: cell?.text ?? null };
      return { edit, applicable: cell.text === edit.expect, latestText: cell.text === edit.expect ? null : cell.text };
    }
    if (!element.chart) return { edit, applicable: false, latestText: null };
    return { edit, applicable: sameChart(element.chart, edit.expect), latestText: null };
  });
}

// ── Text overflow after a save (design §6.8) ────────────────────────────────────────

/** A run of text on a rendered page, in fractions of the page with the origin at the top left. */
export interface PageTextItem {
  text: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}

const OVERFLOW_TOLERANCE = 0.012;

function compact(text: string): string {
  return text.replace(/\s+/g, "");
}

/** How far the element's text reaches past each side of its box, in fractions of the page (0 when inside). */
export interface TextOverflow {
  above: number;
  below: number;
  right: number;
}

/**
 * Measures how far text of the element lies outside its box on a rendered page. Runs belong to
 * the element when their text is part of the element's text and they start within the box's
 * columns and near its rows. Glyph boxes are font-size tall, so a large first line can reach a
 * little above its box without spilling; compare renders with `spillsFurther`.
 */
export function measureTextOverflow(items: ReadonlyArray<PageTextItem>, box: ElementBox, elementText: string, fieldToken = "\ufffc"): TextOverflow {
  const [left, top, width, height] = box;
  const right = left + width;
  const bottom = top + height;
  const overflow: TextOverflow = { above: 0, below: 0, right: 0 };
  const whole = compact(elementText.split(fieldToken).join(""));
  if (!whole) return overflow;
  for (const item of items) {
    const text = compact(item.text);
    if (!text || (text.length < 2 && whole.length >= 2) || !whole.includes(text)) continue;
    if (item.left < left - OVERFLOW_TOLERANCE || item.left > right + OVERFLOW_TOLERANCE) continue;
    if (item.bottom < top - height || item.top > bottom + 2 * height) continue;
    overflow.above = Math.max(overflow.above, top - item.top);
    overflow.below = Math.max(overflow.below, item.bottom - bottom);
    overflow.right = Math.max(overflow.right, item.right - right);
  }
  return overflow;
}

/** True when the edited text reaches past its box on a side where the earlier render did not. */
export function spillsFurther(before: TextOverflow, after: TextOverflow): boolean {
  return (["above", "below", "right"] as const).some((side) => (
    after[side] > OVERFLOW_TOLERANCE && after[side] > Math.max(0, before[side]) + OVERFLOW_TOLERANCE
  ));
}

/** pdf.js text content item, reduced to what the overflow check reads. */
export interface PdfTextLike {
  str?: string;
  transform?: number[];
  width?: number;
  height?: number;
}

/** Converts pdf.js text items of an unrotated page (`view` is the page's `[x0, y0, x1, y1]`). */
export function pageTextItems(items: ReadonlyArray<PdfTextLike>, view: ReadonlyArray<number>): PageTextItem[] {
  const [x0, y0, x1, y1] = view;
  const pageWidth = x1 - x0 || 1;
  const pageHeight = y1 - y0 || 1;
  return items.flatMap((item): PageTextItem[] => {
    if (!item.str || !item.transform || item.transform.length < 6) return [];
    const x = item.transform[4] - x0;
    const baseline = item.transform[5] - y0;
    const glyphHeight = item.height || Math.abs(item.transform[3]) || 0;
    return [{
      text: item.str,
      left: x / pageWidth,
      right: (x + (item.width ?? 0)) / pageWidth,
      top: 1 - (baseline + glyphHeight) / pageHeight,
      bottom: 1 - baseline / pageHeight,
    }];
  });
}
