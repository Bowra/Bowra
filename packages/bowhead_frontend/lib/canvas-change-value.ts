/**
 * Human-readable change values for Canvas objects (Canvas Objects Spec C7). User-visible values are
 * never JSON, field keys, internal IDs or pointers: text stays text, numbers are formatted, lists
 * become value chips (added / removed / unchanged), switches read “On / Off”, enums use their display
 * name and empty values read “(empty)”.
 */

export interface ChangeListItem {
  label: string;
  change?: "added" | "removed";
}

export type ChangeValue =
  | { kind: "text"; text: string }
  | { kind: "number"; text: string }
  | { kind: "list"; items: ChangeListItem[] }
  | { kind: "toggle"; on: boolean }
  | { kind: "enum"; label: string }
  | { kind: "diff"; segments: DiffSegment[] }
  | { kind: "empty" };

export interface DiffSegment {
  text: string;
  op: "equal" | "insert" | "delete";
}

export type ChangeFieldKind = "text" | "number" | "list" | "toggle" | "enum";

export interface FormatChangeOptions {
  locale?: string;
  /** Display name for list items and enum values; return null for values that no longer resolve. */
  label?: (value: string) => string | null;
  /** Shown for list items whose display name cannot be resolved (never the raw reference). */
  unavailableLabel?: string;
  unit?: string;
}

function isEmpty(value: unknown): boolean {
  return value === null || value === undefined || (typeof value === "string" && value.trim() === "")
    || (Array.isArray(value) && value.length === 0);
}

function listLabels(value: unknown, options: FormatChangeOptions): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const raw = typeof item === "string" ? item
      : item && typeof item === "object" && "name" in item && typeof (item as { name: unknown }).name === "string" ? (item as { name: string }).name
        : item && typeof item === "object" && "skill_ref" in item ? String((item as { skill_ref?: { name?: unknown } }).skill_ref?.name ?? "")
          : "";
    const resolved = raw ? options.label ? options.label(raw) : raw : null;
    return resolved ?? options.unavailableLabel ?? "";
  }).filter(Boolean);
}

/** Format one stored value by field kind. */
export function formatChangeValue(kind: ChangeFieldKind, value: unknown, options: FormatChangeOptions = {}): ChangeValue {
  if (kind === "toggle") return typeof value === "boolean" ? { kind: "toggle", on: value }
    : value === "true" || value === "false" ? { kind: "toggle", on: value === "true" } : { kind: "empty" };
  if (isEmpty(value)) return { kind: "empty" };
  if (kind === "list") {
    const items = listLabels(value, options);
    return items.length ? { kind: "list", items: items.map((label) => ({ label })) } : { kind: "empty" };
  }
  if (kind === "number") {
    const number = typeof value === "number" ? value : Number(value);
    if (!Number.isFinite(number)) return { kind: "text", text: String(value) };
    const text = new Intl.NumberFormat(options.locale).format(number);
    return { kind: "number", text: options.unit ? `${text} ${options.unit}` : text };
  }
  if (kind === "enum") {
    const raw = String(value);
    return { kind: "enum", label: options.label?.(raw) ?? raw };
  }
  return { kind: "text", text: typeof value === "string" ? value : String(value) };
}

/**
 * A list comparison for the “after” column: unchanged items stay neutral, new items are marked added,
 * and removed items are appended with a strike-through.
 */
export function formatListChange(before: unknown, after: unknown, options: FormatChangeOptions = {}): { before: ChangeValue; after: ChangeValue } {
  const previous = listLabels(before, options);
  const next = listLabels(after, options);
  const beforeValue: ChangeValue = previous.length ? { kind: "list", items: previous.map((label) => ({ label })) } : { kind: "empty" };
  const items: ChangeListItem[] = [
    ...next.map((label) => (previous.includes(label) ? { label } : { label, change: "added" as const })),
    ...previous.filter((label) => !next.includes(label)).map((label) => ({ label, change: "removed" as const })),
  ];
  return { before: beforeValue, after: items.length ? { kind: "list", items } : { kind: "empty" } };
}

/**
 * Word-level prose diff for revision history: common prefix and suffix stay plain, the middle is shown as
 * a deletion followed by an insertion. Long rewrites therefore stay readable without a full LCS.
 */
export function diffText(before: string, after: string): DiffSegment[] {
  if (before === after) return [{ text: after, op: "equal" }];
  const a = Array.from(before);
  const b = Array.from(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const segments: DiffSegment[] = [];
  if (start > 0) segments.push({ text: a.slice(0, start).join(""), op: "equal" });
  if (endA > start) segments.push({ text: a.slice(start, endA).join(""), op: "delete" });
  if (endB > start) segments.push({ text: b.slice(start, endB).join(""), op: "insert" });
  if (endA < a.length) segments.push({ text: a.slice(endA).join(""), op: "equal" });
  return segments;
}

/** Trim long equal context around a diff so a section rewrite stays scannable. */
export function trimDiffContext(segments: DiffSegment[], context = 40): DiffSegment[] {
  return segments.map((segment, index) => {
    if (segment.op !== "equal") return segment;
    const chars = Array.from(segment.text);
    if (chars.length <= context * 2) return segment;
    if (index === 0) return { ...segment, text: `…${chars.slice(-context).join("")}` };
    if (index === segments.length - 1) return { ...segment, text: `${chars.slice(0, context).join("")}…` };
    return { ...segment, text: `${chars.slice(0, context).join("")}…${chars.slice(-context).join("")}` };
  });
}
