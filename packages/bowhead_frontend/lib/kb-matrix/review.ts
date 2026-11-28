/**
 * Document matrix step 1 (S4) review: in-table search, per-column value filters, status filters, sort and the
 * verified progress. Everything runs client-side over the snapshot (the 500 × 20 cap keeps it under 10,000 cells)
 * and is kept in the URL query (`?q=…&f=…&sort=…`), so back and reload restore it.
 *
 * Rows the caller cannot read (`missing`) hold no values or names: any active filter or search leaves them out,
 * and a sort puts them last.
 */

import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "./types";

/** Confidence below this counts as low (Box's confidence bands; grounding drops an unfound quote to or below it). */
export const LOW_CONFIDENCE_BELOW = 0.7;
/** Cross-check agreement at or below this means the two readings disagree (the side panel warns at the same point). */
export const CROSS_CHECK_DISAGREE_AT_MOST = 0.5;

export const STATUS_FILTERS = ["unverified", "edited", "low_confidence", "cross_check", "failed", "stale"] as const;
export type StatusFilter = (typeof STATUS_FILTERS)[number];

export const VALUE_FILTER_OPS = ["eq", "contains", "gt", "lt", "empty"] as const;
export type ValueFilterOp = (typeof VALUE_FILTER_OPS)[number];

export interface ValueFilter {
  columnId: string;
  op: ValueFilterOp;
  /** Unused for `empty`. Booleans compare the tokens "true" / "false". */
  value: string;
}

/** Sort key of the file-name column (column ids are `mxcol_*`). */
export const FILE_SORT = "@file";

export interface MatrixSort {
  /** A column id, or FILE_SORT. */
  key: string;
  dir: "asc" | "desc";
}

export interface MatrixView {
  q: string;
  status: StatusFilter | null;
  /** At most one per column. */
  values: ValueFilter[];
  sort: MatrixSort | null;
}

export const EMPTY_VIEW: MatrixView = { q: "", status: null, values: [], sort: null };

/** Longest search text kept in the URL. */
const MAX_QUERY = 200;

export type ValueKind = "number" | "date" | "boolean" | "choice" | "text";

export function valueKind(type: string): ValueKind {
  if (type === "number" || type === "currency") return "number";
  if (type === "date") return "date";
  if (type === "boolean") return "boolean";
  if (type === "select" || type === "multi_select") return "choice";
  return "text";
}

/** Operators a column offers: greater/less only for numbers and dates, contains only for text and options. */
export function valueFilterOps(type: string): ValueFilterOp[] {
  const kind = valueKind(type);
  if (kind === "number" || kind === "date") return ["eq", "gt", "lt", "empty"];
  if (kind === "boolean") return ["eq", "empty"];
  return ["eq", "contains", "empty"];
}

/** What filters and sort read: the value of a done or needs-review cell; "" for any other state. */
export function cellText(cell: MatrixCellVM | undefined): string {
  return cell && (cell.status === "done" || cell.status === "needs_review") ? cell.value : "";
}

function fold(text: string): string {
  return text.normalize("NFKC").trim().toLocaleLowerCase();
}

function parseNumber(text: string): number | null {
  const cleaned = text.normalize("NFKC").replace(/[\s,¥$€£]/g, "");
  if (!cleaned) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

/** "YYYY-MM-DD" for an ISO-like or parseable date, else null. */
function parseDate(text: string): string | null {
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(text.trim());
  if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}`;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? new Date(ms).toISOString().slice(0, 10) : null;
}

function choices(cell: MatrixCellVM | undefined): string[] {
  const primary = cell?.variants.find((v) => v.is_primary) ?? cell?.variants[0];
  const n = primary?.normalized as { choices?: unknown } | null | undefined;
  if (n && Array.isArray(n.choices)) return n.choices.map(String);
  const text = cellText(cell);
  return text ? text.split("、") : [];
}

export function matchesValueFilter(cell: MatrixCellVM | undefined, column: MatrixColumnVM, filter: ValueFilter): boolean {
  const text = cellText(cell).trim();
  if (filter.op === "empty") return text === "";
  if (text === "") return false;
  const needle = filter.value.trim();
  const kind = valueKind(column.type);
  if (kind === "number" || kind === "date") {
    if (filter.op === "contains") return fold(text).includes(fold(needle));
    const a = kind === "number" ? parseNumber(text) : parseDate(text);
    const b = kind === "number" ? parseNumber(needle) : parseDate(needle);
    if (a === null || b === null) return false;
    if (filter.op === "eq") return a === b;
    return filter.op === "gt" ? a > b : a < b;
  }
  if (kind === "boolean") return filter.op === "eq" && text === needle;
  if (column.type === "multi_select" && filter.op === "eq") return choices(cell).some((c) => fold(c) === fold(needle));
  if (filter.op === "contains") return fold(text).includes(fold(needle));
  return filter.op === "eq" && fold(text) === fold(needle);
}

/** Low confidence: an AI value below LOW_CONFIDENCE_BELOW, or one whose source quote was not found in the document. */
export function isLowConfidence(cell: MatrixCellVM | undefined): boolean {
  if (!cell || cell.edited || !cellText(cell)) return false;
  if (cell.confidence != null && cell.confidence < LOW_CONFIDENCE_BELOW) return true;
  // Grounding clears a quote it cannot find; a quote matched to no chunk was not found either.
  return !cell.sourceExcerpt || !cell.chunkId;
}

export function cellMatchesStatus(cell: MatrixCellVM | undefined, status: StatusFilter): boolean {
  if (!cell) return false;
  switch (status) {
    case "unverified":
      return (cell.status === "done" || cell.status === "needs_review") && !cell.verified;
    case "edited":
      return cell.edited;
    case "low_confidence":
      return isLowConfidence(cell);
    case "cross_check":
      return cell.selfConsistencyAgree != null && cell.selfConsistencyAgree <= CROSS_CHECK_DISAGREE_AT_MOST;
    case "failed":
      return cell.status === "failed";
    case "stale":
      return !!cell.stale;
  }
}

/** Search reads the file name and every value as shown (booleans in the viewer's words). */
export interface SearchLabels {
  boolTrue: string;
  boolFalse: string;
}

function rowMatchesSearch(row: MatrixRowVM, columns: MatrixColumnVM[], q: string, labels?: SearchLabels): boolean {
  if (fold(row.fileName).includes(q)) return true;
  return columns.some((c) => {
    const text = cellText(row.cells[c.columnId]);
    if (!text) return false;
    const shown = c.type === "boolean" && labels ? (text === "true" ? labels.boolTrue : text === "false" ? labels.boolFalse : text) : text;
    return fold(shown).includes(q);
  });
}

export function viewIsFiltered(view: MatrixView): boolean {
  return !!view.q.trim() || view.status !== null || view.values.length > 0;
}

/** The rows the grid shows, filtered and sorted; filters on columns that no longer exist are ignored. */
export function viewRows(
  rows: MatrixRowVM[],
  columns: MatrixColumnVM[],
  view: MatrixView,
  opts: { labels?: SearchLabels; locale?: string } = {},
): MatrixRowVM[] {
  const byId = new Map(columns.map((c) => [c.columnId, c]));
  const values = view.values.filter((f) => byId.has(f.columnId));
  const q = fold(view.q);
  const status = view.status;
  let out = rows;
  if (q || status || values.length) {
    out = rows.filter(
      (row) =>
        !row.missing &&
        (!q || rowMatchesSearch(row, columns, q, opts.labels)) &&
        (!status || columns.some((c) => cellMatchesStatus(row.cells[c.columnId], status))) &&
        values.every((f) => matchesValueFilter(row.cells[f.columnId], byId.get(f.columnId) as MatrixColumnVM, f)),
    );
  }
  const sort = view.sort;
  if (sort && (sort.key === FILE_SORT || byId.has(sort.key))) out = sortRows(out, sort, byId.get(sort.key), opts.locale);
  return out;
}

type SortValue = { empty: true } | { empty: false; n?: number; s?: string };

function sortValue(row: MatrixRowVM, sort: MatrixSort, column: MatrixColumnVM | undefined): SortValue {
  if (row.missing) return { empty: true };
  if (sort.key === FILE_SORT || !column) return row.fileName ? { empty: false, s: row.fileName } : { empty: true };
  const text = cellText(row.cells[column.columnId]).trim();
  if (!text) return { empty: true };
  const kind = valueKind(column.type);
  if (kind === "number") {
    const n = parseNumber(text);
    return n === null ? { empty: false, s: text } : { empty: false, n };
  }
  if (kind === "date") return { empty: false, s: parseDate(text) ?? text };
  return { empty: false, s: text };
}

/** Stable sort; empty values (and unreadable rows) stay last in both directions. Numbers come before text. */
export function sortRows(rows: MatrixRowVM[], sort: MatrixSort, column: MatrixColumnVM | undefined, locale?: string): MatrixRowVM[] {
  const collator = new Intl.Collator(locale, { numeric: true, sensitivity: "base" });
  const sign = sort.dir === "asc" ? 1 : -1;
  const keyed = rows.map((row, index) => ({ row, index, v: sortValue(row, sort, column) }));
  keyed.sort((a, b) => {
    if (a.v.empty || b.v.empty) return a.v.empty === b.v.empty ? a.index - b.index : a.v.empty ? 1 : -1;
    let diff: number;
    if (a.v.n !== undefined && b.v.n !== undefined) diff = a.v.n - b.v.n;
    else if (a.v.n !== undefined) diff = -1;
    else if (b.v.n !== undefined) diff = 1;
    else diff = collator.compare(a.v.s ?? "", b.v.s ?? "");
    return diff !== 0 ? sign * diff : a.index - b.index;
  });
  return keyed.map((k) => k.row);
}

/** "已核 x / y": verified cells over every cell of the rows the caller can read (the whole matrix, not the filtered view). */
export function verifiedProgress(rows: MatrixRowVM[], columns: MatrixColumnVM[]): { verified: number; total: number } {
  let verified = 0;
  let total = 0;
  for (const row of rows) {
    if (row.missing) continue;
    total += columns.length;
    for (const c of columns) if (row.cells[c.columnId]?.verified) verified++;
  }
  return { verified, total };
}

/** What "整列标记已核" would do, mirroring the server: done and current cells change, edited ones included. */
export function columnVerifyPreview(
  rows: MatrixRowVM[],
  column: MatrixColumnVM,
): { eligible: number; notDone: number; stale: number; already: number; unavailable: number } {
  const out = { eligible: 0, notDone: 0, stale: 0, already: 0, unavailable: 0 };
  for (const row of rows) {
    const cell = row.cells[column.columnId];
    if (row.missing) out.unavailable++;
    else if (!cell || cell.status !== "done") out.notDone++;
    else if (cell.stale) out.stale++;
    else if (cell.verified) out.already++;
    else out.eligible++;
  }
  return out;
}

// ─── URL query ───────────────────────────────────────────────

const STATUS_PARAM = "status";

function isStatus(value: string): value is StatusFilter {
  return (STATUS_FILTERS as readonly string[]).includes(value);
}

function isOp(value: string): value is ValueFilterOp {
  return (VALUE_FILTER_OPS as readonly string[]).includes(value);
}

function splitOnce(text: string): [string, string] | null {
  const at = text.indexOf(":");
  return at < 0 ? null : [text.slice(0, at), text.slice(at + 1)];
}

function decode(text: string): string | null {
  try {
    return decodeURIComponent(text);
  } catch {
    return null;
  }
}

/**
 * `q=<text>`, `f=status:<bucket>`, `f=<op>:<column id, URI-encoded>[:<value>]` (one per column, later wins) and
 * `sort=<asc|desc>:<column id or @file>`. Unknown or malformed parts are ignored.
 */
export function readMatrixView(search: string): MatrixView {
  const params = new URLSearchParams(search);
  const view: MatrixView = { q: (params.get("q") ?? "").slice(0, MAX_QUERY), status: null, values: [], sort: null };
  const byColumn = new Map<string, ValueFilter>();
  for (const raw of params.getAll("f")) {
    const head = splitOnce(raw);
    if (!head) continue;
    const [kind, rest] = head;
    if (kind === STATUS_PARAM) {
      if (isStatus(rest)) view.status = rest;
      continue;
    }
    if (!isOp(kind)) continue;
    const parts = splitOnce(rest) ?? [rest, ""];
    const columnId = decode(parts[0]);
    if (!columnId) continue;
    if (kind !== "empty" && !parts[1].trim()) continue;
    byColumn.set(columnId, { columnId, op: kind, value: kind === "empty" ? "" : parts[1] });
  }
  view.values = [...byColumn.values()];
  const sort = splitOnce(params.get("sort") ?? "");
  if (sort && (sort[0] === "asc" || sort[0] === "desc") && sort[1]) view.sort = { key: sort[1], dir: sort[0] };
  return view;
}

/** The query with this view written over `q`, `f` and `sort`; other parameters (tab, view) are kept. */
export function writeMatrixView(search: string, view: MatrixView): URLSearchParams {
  const params = new URLSearchParams(search);
  params.delete("q");
  params.delete("f");
  params.delete("sort");
  const q = view.q.trim().slice(0, MAX_QUERY);
  if (q) params.set("q", q);
  if (view.status) params.append("f", `${STATUS_PARAM}:${view.status}`);
  for (const f of view.values) {
    const column = encodeURIComponent(f.columnId);
    params.append("f", f.op === "empty" ? `empty:${column}` : `${f.op}:${column}:${f.value}`);
  }
  if (view.sort) params.set("sort", `${view.sort.dir}:${view.sort.key}`);
  return params;
}
