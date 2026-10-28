/**
 * Source content blocks: turn a citation source's viewer-safe preview into the
 * most structured form it supports (table → markdown → preformatted text).
 *
 * Why this exists: the citation drawer used to feed every text preview to the
 * Markdown renderer. Sandbox stdout is not Markdown — a printed pandas
 * DataFrame or a `--- csv ---` dump has meaningful single line breaks, which
 * Markdown folds into one paragraph, so tables came out as a run-on line.
 * The line breaks are intact in the payload; the renderer dropped them.
 *
 * Order of preference:
 *   1. structured rows carried by the payload (never re-parse text when rows exist);
 *   2. a whole-text JSON array of flat records → table;
 *   3. Markdown, only with unambiguous Markdown syntax;
 *   4. CSV/TSV runs and whitespace-aligned DataFrame prints → tables, the rest
 *      stays as preformatted text that keeps every line break.
 *
 * Pure functions only: no DOM, no i18n.
 */

import type { ReportSection } from "@/lib/types";

export interface SourceTable {
  columns: string[];
  rows: Array<Record<string, string>>;
  /** Columns whose every non-missing cell is a plain number. */
  numericColumns: string[];
  /** Real row count behind the shown rows, when the payload says so. */
  totalRows?: number;
}

export type SourceBlock =
  | { kind: "table"; table: SourceTable; label?: string }
  | { kind: "markdown"; text: string }
  | { kind: "text"; text: string; label?: string };

const MAX_TABLE_ROWS = 500;
const NUMBER_RE = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$/;
const GROUPED_NUMBER_RE = /^[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$/;
const MISSING_TOKENS = new Set(["", "nan", "none", "null", "nat", "<na>"]);
const SECTION_MARKER_RE = /^\s*-{2,}\s*([^-\s][^-]*?)\s*-{2,}\s*$/;
const PANDAS_FOOTER_RE = /^\s*\[\d+ rows x \d+ columns\]\s*$/;

export function isMissingCell(value: string): boolean {
  return MISSING_TOKENS.has(value.trim().toLowerCase());
}

export function parseNumberCell(value: string): number | null {
  const trimmed = value.trim();
  if (NUMBER_RE.test(trimmed)) return Number(trimmed);
  if (GROUPED_NUMBER_RE.test(trimmed)) return Number(trimmed.replace(/,/g, ""));
  return null;
}

/** Group the available numeric representation without rounding, including tiny values.
 * Decimal shifts are textual so declared percentage scaling cannot introduce binary tails. */
export function formatExactSourceNumber(value: number | null, decimalShift = 0, locale?: string): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  const [mantissa, exponent = "0"] = Math.abs(value).toString().split("e");
  const [integer, fraction = ""] = mantissa!.split(".");
  const digits = integer! + fraction;
  const point = integer!.length + Number(exponent) + decimalShift;
  const whole = (point <= 0 ? "0" : digits.slice(0, point).padEnd(point, "0"))
    .replace(/^0+(?=\d)/, "");
  const tail = (point <= 0 ? "0".repeat(-point) + digits : digits.slice(point)).replace(/0+$/, "");
  const grouped = locale ? new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(BigInt(whole))
    : whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const separator = locale ? new Intl.NumberFormat(locale).formatToParts(1.5).find((part) => part.type === "decimal")!.value : ".";
  return `${value < 0 ? "-" : ""}${grouped}${tail ? `${separator}${tail}` : ""}`;
}

/** Display string for one parsed cell: numbers through `formatExactSourceNumber`, missing → "—". */
export function formatSourceCell(table: SourceTable, column: string, value: string): string {
  if (isMissingCell(value)) return "—";
  if (!table.numericColumns.includes(column)) return value;
  return formatExactSourceNumber(parseNumberCell(value));
}

function isNumberToken(value: string): boolean {
  return parseNumberCell(value) !== null;
}

/** Brackets that open or close without their partner mark list/tuple reprs, not table cells. */
function hasUnbalancedBrackets(value: string): boolean {
  let depth = 0;
  for (const ch of value) {
    if (ch === "[" || ch === "(" || ch === "{") depth += 1;
    else if (ch === "]" || ch === ")" || ch === "}") depth -= 1;
    if (depth < 0) return true;
  }
  return depth !== 0;
}

/** A column of bare four-digit integers is a year or an id: number formatting would print "2,026". */
const FOUR_DIGIT_INTEGER_RE = /^\d{4}$/;

function numericColumnsOf(columns: string[], rows: Array<Record<string, string>>): string[] {
  return columns.filter((column) => {
    let seen = 0;
    let fourDigit = 0;
    for (const row of rows) {
      const cell = (row[column] ?? "").trim();
      if (isMissingCell(cell)) continue;
      if (!isNumberToken(cell)) return false;
      seen += 1;
      if (FOUR_DIGIT_INTEGER_RE.test(cell)) fourDigit += 1;
    }
    return seen > 0 && fourDigit < seen;
  });
}

export function buildSourceTable(
  columns: string[],
  rows: Array<Record<string, unknown>>,
  totalRows?: number | null,
): SourceTable {
  const stringRows = rows.slice(0, MAX_TABLE_ROWS).map((row) => {
    const out: Record<string, string> = {};
    for (const column of columns) {
      const value = row[column];
      out[column] = value === null || value === undefined ? "" : String(value);
    }
    return out;
  });
  const table: SourceTable = {
    columns,
    rows: stringRows,
    numericColumns: numericColumnsOf(columns, stringRows),
  };
  const total = typeof totalRows === "number" ? totalRows : rows.length;
  if (total > stringRows.length) table.totalRows = total;
  return table;
}

/** Unambiguous Markdown syntax only; plain stdout must never be routed here. */
export function looksLikeMarkdown(text: string): boolean {
  if (/^#{1,6}\s+\S/m.test(text)) return true;
  if (/^\s*```/m.test(text)) return true;
  if (/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/m.test(text)) return true;
  if (/\*\*[^*\n]+\*\*/.test(text)) return true;
  if (/\[[^\]\n]+\]\([^)\s]+\)/.test(text)) return true;
  const listLines = text.split("\n").filter((line) => /^\s*(?:[-*+]|\d+\.)\s+\S/.test(line));
  return listLines.length >= 2;
}

function tableFromJson(text: string): SourceTable | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("[")) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  const columns: string[] = [];
  for (const item of parsed) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    for (const [key, value] of Object.entries(item as Record<string, unknown>)) {
      if (value !== null && typeof value === "object") return null;
      if (!columns.includes(key)) columns.push(key);
    }
  }
  return buildSourceTable(columns, parsed as Array<Record<string, unknown>>);
}

function splitDelimitedLine(line: string, delimiter: string): string[] {
  const cells: string[] = [];
  let current = "";
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        current += ch;
      }
    } else if (ch === '"' && current.trim() === "") {
      quoted = true;
      current = "";
    } else if (ch === delimiter) {
      cells.push(current.trim());
      current = "";
    } else {
      current += ch;
    }
  }
  cells.push(current.trim());
  return cells;
}

interface TableRun {
  table: SourceTable;
  end: number;
}

/** pandas `to_csv()` writes the RangeIndex as an unnamed first column; it is noise for readers. */
function dropUnnamedIntegerIndex(header: string[], body: string[][]): { header: string[]; body: string[][] } {
  if (header[0] !== "" || header.length < 2) return { header, body };
  if (!body.every((cells) => /^\d+$/.test(cells[0] ?? ""))) return { header, body };
  return { header: header.slice(1), body: body.map((cells) => cells.slice(1)) };
}

function toTable(header: string[], body: string[][]): SourceTable {
  const columns = header.map((name, index) => name || `#${index + 1}`);
  const rows = body.map((cells) => Object.fromEntries(columns.map((column, index) => [column, cells[index] ?? ""])));
  return buildSourceTable(columns, rows);
}

function readDelimited(lines: string[], start: number, forced: boolean): TableRun | null {
  const first = lines[start] ?? "";
  const delimiter = first.includes("\t") ? "\t" : first.includes(",") ? "," : null;
  if (!delimiter) return null;
  const header = splitDelimitedLine(first, delimiter);
  if (header.length < 2) return null;
  if (header.some((cell) => isNumberToken(cell) || hasUnbalancedBrackets(cell))) return null;
  if (header.filter((cell) => cell !== "").length < header.length - 1) return null;
  const body: string[][] = [];
  let end = start + 1;
  while (end < lines.length && body.length < MAX_TABLE_ROWS) {
    const line = lines[end] ?? "";
    if (line.trim() === "") break;
    const cells = splitDelimitedLine(line, delimiter);
    if (cells.length !== header.length || cells.some(hasUnbalancedBrackets)) break;
    body.push(cells);
    end += 1;
  }
  if (body.length < (forced ? 1 : 2)) return null;
  const shaped = dropUnnamedIntegerIndex(header, body);
  const table = toTable(shaped.header, shaped.body);
  if (!forced && table.numericColumns.length === 0) return null;
  return { table, end };
}

function whitespaceTokens(line: string): string[] {
  return line.trim().split(/\s+/).filter(Boolean);
}

/** A whitespace-aligned DataFrame print (`print(df)` / `df.to_string()`), with or without its index. */
function readAligned(lines: string[], start: number): TableRun | null {
  const header = whitespaceTokens(lines[start] ?? "");
  if (header.length < 2) return null;
  if (header.some((cell) => isNumberToken(cell) || hasUnbalancedBrackets(cell))) return null;
  const body: string[][] = [];
  let width: number | null = null;
  let end = start + 1;
  while (end < lines.length && body.length < MAX_TABLE_ROWS) {
    const line = lines[end] ?? "";
    if (line.trim() === "" || PANDAS_FOOTER_RE.test(line)) break;
    const cells = whitespaceTokens(line);
    if (cells.length !== header.length && cells.length !== header.length + 1) break;
    if (width !== null && cells.length !== width) break;
    if (cells.some(hasUnbalancedBrackets)) break;
    width = cells.length;
    body.push(cells);
    end += 1;
  }
  if (body.length < 2 || width === null) return null;
  let shapedHeader = header;
  let shapedBody = body;
  if (width === header.length + 1) {
    // pandas prints the index as an unlabeled leading column. A default RangeIndex is noise;
    // any other index carries the row label, so it stays as a column without a header.
    if (body.every((cells) => /^\d+$/.test(cells[0] ?? ""))) {
      shapedBody = body.map((cells) => cells.slice(1));
    } else {
      shapedHeader = ["", ...header];
    }
  }
  const table = toTable(shapedHeader, shapedBody);
  if (table.numericColumns.length === 0) return null;
  // Allow the trailing `[N rows x M columns]` line to be consumed with the table.
  if (PANDAS_FOOTER_RE.test(lines[end] ?? "")) end += 1;
  return { table, end };
}

function isDelimitedLabel(label: string | undefined): boolean {
  return !!label && /\b(?:csv|tsv)\b/i.test(label);
}

/** Split free text into tables and preformatted text blocks. */
export function sourceTextBlocks(text: string): SourceBlock[] {
  const normalized = text.replace(/\r\n?/g, "\n");
  if (!normalized.trim()) return [];
  const jsonTable = tableFromJson(normalized);
  if (jsonTable) return [{ kind: "table", table: jsonTable }];
  if (looksLikeMarkdown(normalized)) return [{ kind: "markdown", text: normalized }];

  const lines = normalized.split("\n");
  const blocks: SourceBlock[] = [];
  let pending: string[] = [];
  let label: string | undefined;

  const flushText = () => {
    while (pending.length && !pending[0]!.trim()) pending.shift();
    while (pending.length && !pending[pending.length - 1]!.trim()) pending.pop();
    if (pending.length) {
      blocks.push(label ? { kind: "text", text: pending.join("\n"), label } : { kind: "text", text: pending.join("\n") });
      label = undefined;
    }
    pending = [];
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? "";
    const marker = SECTION_MARKER_RE.exec(line);
    if (marker) {
      flushText();
      label = marker[1]!.trim();
      i += 1;
      continue;
    }
    if (line.trim()) {
      const run = readDelimited(lines, i, isDelimitedLabel(label) && pending.length === 0) ?? readAligned(lines, i);
      if (run) {
        flushText();
        blocks.push(label ? { kind: "table", table: run.table, label } : { kind: "table", table: run.table });
        label = undefined;
        i = run.end;
        continue;
      }
    }
    pending.push(line);
    i += 1;
  }
  flushText();
  return blocks;
}

export type CodeRunSection = Extract<ReportSection, { kind: "code_block" }>;

/** The code block whose outputs include this source, linked by viewer alias (`output_aliases`).
 *  Old canvases carry no aliases and simply have no linked run. Superseded runs are skipped. */
export function codeRunForSource(sections: readonly ReportSection[], alias: string | undefined): CodeRunSection | null {
  if (!alias) return null;
  for (const section of sections) {
    if (section.kind !== "code_block") continue;
    if ((section as { superseded?: boolean }).superseded) continue;
    if (section.output_aliases?.includes(alias)) return section;
  }
  return null;
}
