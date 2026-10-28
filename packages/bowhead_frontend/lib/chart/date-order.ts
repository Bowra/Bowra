type Cell = string | number | null | undefined;

const DATE_COLLATOR = new Intl.Collator("en", {
  numeric: true,
  sensitivity: "base",
});

/**
 * Compare date-axis labels by their numeric parts instead of plain text.
 *
 * Datarc can return month buckets without zero padding (`2025-1`, `2025-11`).
 * A lexical comparison puts November before February; numeric collation keeps
 * ISO-like, Chinese year/month and quarter labels in chronological order while
 * preserving the original display value.
 */
export function compareDateAxisValues(left: Cell, right: Cell): number {
  if (left === right) return 0;
  if (left === null || left === undefined || left === "") return 1;
  if (right === null || right === undefined || right === "") return -1;
  return DATE_COLLATOR.compare(String(left), String(right));
}
