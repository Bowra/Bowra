/**
 * Document matrix step 1 (S4) review logic: value filter operators per column type, status buckets (low confidence
 * by the named threshold or an unfound quote), search, sort, verified progress, the column bulk-verify preview and the
 * URL query that keeps the view across back and reload.
 */

import { describe, expect, it } from "vitest";

import {
  EMPTY_VIEW,
  FILE_SORT,
  LOW_CONFIDENCE_BELOW,
  type MatrixView,
  cellMatchesStatus,
  columnVerifyPreview,
  isLowConfidence,
  matchesValueFilter,
  readMatrixView,
  valueFilterOps,
  verifiedProgress,
  viewRows,
  writeMatrixView,
} from "./review";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "./types";

const cell = (over: Partial<MatrixCellVM> = {}): MatrixCellVM => ({
  status: "done",
  value: "v",
  variants: [],
  edited: false,
  verified: false,
  confidence: 0.9,
  sourceExcerpt: "quote",
  chunkId: "chk_1",
  ...over,
});
const row = (fileId: string, cells: MatrixRowVM["cells"], over: Partial<MatrixRowVM> = {}): MatrixRowVM => ({
  fileId,
  fileName: `${fileId}.pdf`,
  missing: false,
  extractFailed: false,
  recordId: "",
  approved: false,
  cells,
  ...over,
});
const col = (columnId: string, type: string, extra: Partial<MatrixColumnVM> = {}): MatrixColumnVM => ({ columnId, name: columnId, type, ...extra });

describe("value filters", () => {
  const text = col("party", "question");
  const amount = col("amount", "currency");
  const due = col("due", "date");
  const flag = col("compete", "boolean");
  const pay = col("pay", "select", { options: ["预付", "月结"] });
  const tags = col("tags", "multi_select", { options: ["冷链", "运输"] });

  it("offers greater or less than only for numbers and dates, contains only for text and options", () => {
    expect(valueFilterOps("currency")).toEqual(["eq", "gt", "lt", "empty"]);
    expect(valueFilterOps("date")).toEqual(["eq", "gt", "lt", "empty"]);
    expect(valueFilterOps("boolean")).toEqual(["eq", "empty"]);
    expect(valueFilterOps("question")).toEqual(["eq", "contains", "empty"]);
    expect(valueFilterOps("multi_select")).toEqual(["eq", "contains", "empty"]);
  });

  it("compares text case- and width-insensitively", () => {
    const c = cell({ value: "Huadong Cold Chain" });
    expect(matchesValueFilter(c, text, { columnId: "party", op: "eq", value: " huadong cold chain " })).toBe(true);
    expect(matchesValueFilter(c, text, { columnId: "party", op: "contains", value: "ＣＯＬＤ" })).toBe(true);
    expect(matchesValueFilter(c, text, { columnId: "party", op: "contains", value: "north" })).toBe(false);
  });

  it("compares numbers as numbers, also when typed with separators", () => {
    const c = cell({ value: "1280000" });
    expect(matchesValueFilter(c, amount, { columnId: "amount", op: "gt", value: "1,000,000" })).toBe(true);
    expect(matchesValueFilter(c, amount, { columnId: "amount", op: "lt", value: "1000000" })).toBe(false);
    expect(matchesValueFilter(c, amount, { columnId: "amount", op: "eq", value: "1280000.00" })).toBe(true);
    expect(matchesValueFilter(cell({ value: "约 500 万" }), amount, { columnId: "amount", op: "gt", value: "1" })).toBe(false);
  });

  it("compares dates by day", () => {
    const c = cell({ value: "2026-02-28" });
    expect(matchesValueFilter(c, due, { columnId: "due", op: "gt", value: "2026-01-31" })).toBe(true);
    expect(matchesValueFilter(c, due, { columnId: "due", op: "lt", value: "2026-01-31" })).toBe(false);
    expect(matchesValueFilter(c, due, { columnId: "due", op: "eq", value: "2026-02-28" })).toBe(true);
  });

  it("matches booleans by token and options by name", () => {
    expect(matchesValueFilter(cell({ value: "true" }), flag, { columnId: "compete", op: "eq", value: "true" })).toBe(true);
    expect(matchesValueFilter(cell({ value: "false" }), flag, { columnId: "compete", op: "eq", value: "true" })).toBe(false);
    expect(matchesValueFilter(cell({ value: "预付" }), pay, { columnId: "pay", op: "eq", value: "预付" })).toBe(true);
    const both = cell({ value: "冷链、运输", variants: [{ is_primary: true, normalized: { choices: ["冷链", "运输"] } }] as never });
    expect(matchesValueFilter(both, tags, { columnId: "tags", op: "eq", value: "运输" })).toBe(true);
    expect(matchesValueFilter(both, tags, { columnId: "tags", op: "contains", value: "冷" })).toBe(true);
  });

  it("treats cells without a value as empty, and never matches them otherwise", () => {
    const empty = { columnId: "party", op: "empty" as const, value: "" };
    expect(matchesValueFilter(undefined, text, empty)).toBe(true);
    expect(matchesValueFilter(cell({ status: "failed", value: "" }), text, empty)).toBe(true);
    expect(matchesValueFilter(cell({ status: "running" }), text, empty)).toBe(true);
    expect(matchesValueFilter(cell({ value: "x" }), text, empty)).toBe(false);
    expect(matchesValueFilter(undefined, text, { columnId: "party", op: "contains", value: "" })).toBe(false);
  });
});

describe("status filters", () => {
  it("counts a cell as low confidence below the named threshold or when its quote was not found", () => {
    expect(LOW_CONFIDENCE_BELOW).toBe(0.7);
    expect(isLowConfidence(cell({ confidence: 0.69 }))).toBe(true);
    expect(isLowConfidence(cell({ confidence: 0.7 }))).toBe(false);
    expect(isLowConfidence(cell({ sourceExcerpt: undefined }))).toBe(true);
    expect(isLowConfidence(cell({ chunkId: undefined }))).toBe(true);
    // A manual value and a cell without a value are not the AI's guess.
    expect(isLowConfidence(cell({ confidence: 0.2, edited: true }))).toBe(false);
    expect(isLowConfidence(cell({ confidence: 0.2, value: "" }))).toBe(false);
    expect(isLowConfidence(cell({ status: "failed", confidence: 0.2 }))).toBe(false);
  });

  it("puts every cell in the buckets it belongs to", () => {
    expect(cellMatchesStatus(cell(), "unverified")).toBe(true);
    expect(cellMatchesStatus(cell({ status: "needs_review" }), "unverified")).toBe(true);
    expect(cellMatchesStatus(cell({ verified: true }), "unverified")).toBe(false);
    expect(cellMatchesStatus(cell({ status: "failed" }), "unverified")).toBe(false);
    expect(cellMatchesStatus(undefined, "unverified")).toBe(false);
    expect(cellMatchesStatus(cell({ edited: true }), "edited")).toBe(true);
    expect(cellMatchesStatus(cell({ selfConsistencyAgree: 0.5 }), "cross_check")).toBe(true);
    expect(cellMatchesStatus(cell({ selfConsistencyAgree: 1 }), "cross_check")).toBe(false);
    expect(cellMatchesStatus(cell(), "cross_check")).toBe(false);
    expect(cellMatchesStatus(cell({ status: "failed" }), "failed")).toBe(true);
    expect(cellMatchesStatus(cell({ stale: true }), "stale")).toBe(true);
    expect(cellMatchesStatus(cell(), "stale")).toBe(false);
  });
});

describe("viewRows", () => {
  const columns = [col("amount", "currency"), col("note", "question"), col("compete", "boolean")];
  const rows = [
    row("a", { amount: cell({ value: "1280000", verified: true }), note: cell({ value: "Cold chain", edited: true }), compete: cell({ value: "true" }) }),
    row("b", { amount: cell({ value: "860000", confidence: 0.5 }), note: cell({ status: "failed", value: "" }) }),
    row("c", { amount: cell({ value: "245000" }), compete: cell({ value: "false" }) }),
    row("gone", { amount: cell({ status: "failed", value: "", failReason: "source_unavailable" }) }, { missing: true, fileName: "" }),
  ];
  const ids = (view: Partial<MatrixView>, locale = "en-US") =>
    viewRows(rows, columns, { ...EMPTY_VIEW, ...view }, { labels: { boolTrue: "Yes", boolFalse: "No" }, locale }).map((r) => r.fileId);

  it("keeps every row, in matrix order, without a view", () => {
    expect(ids({})).toEqual(["a", "b", "c", "gone"]);
  });

  it("filters by status and leaves unreadable rows out", () => {
    expect(ids({ status: "failed" })).toEqual(["b"]);
    expect(ids({ status: "low_confidence" })).toEqual(["b"]);
    expect(ids({ status: "edited" })).toEqual(["a"]);
    expect(ids({ status: "unverified" })).toEqual(["a", "b", "c"]);
  });

  it("searches file names and values as shown, booleans in the viewer's words", () => {
    expect(ids({ q: "cold" })).toEqual(["a"]);
    expect(ids({ q: "c.pdf" })).toEqual(["c"]);
    expect(ids({ q: "no" })).toEqual(["c"]);
  });

  it("combines value filters with AND and ignores filters on removed columns", () => {
    expect(ids({ values: [{ columnId: "amount", op: "gt", value: "500000" }] })).toEqual(["a", "b"]);
    expect(
      ids({
        values: [
          { columnId: "amount", op: "gt", value: "500000" },
          { columnId: "compete", op: "eq", value: "true" },
        ],
      }),
    ).toEqual(["a"]);
    expect(ids({ values: [{ columnId: "deleted", op: "empty", value: "" }] })).toEqual(["a", "b", "c", "gone"]);
  });

  it("sorts numbers numerically, keeps empty values and unreadable rows last, and is stable", () => {
    expect(ids({ sort: { key: "amount", dir: "asc" } })).toEqual(["c", "b", "a", "gone"]);
    expect(ids({ sort: { key: "amount", dir: "desc" } })).toEqual(["a", "b", "c", "gone"]);
    expect(ids({ sort: { key: "compete", dir: "asc" } })).toEqual(["c", "a", "b", "gone"]);
    expect(ids({ sort: { key: "compete", dir: "desc" } })).toEqual(["a", "c", "b", "gone"]);
    expect(ids({ sort: { key: FILE_SORT, dir: "desc" } })).toEqual(["c", "b", "a", "gone"]);
    expect(ids({ sort: { key: "deleted", dir: "asc" } })).toEqual(["a", "b", "c", "gone"]);
  });

  it("sorts dates by day and text with the locale's collation", () => {
    const dated = [row("x", { d: cell({ value: "2027-04-09" }) }), row("y", { d: cell({ value: "2025-12-01" }) }), row("z", { d: undefined })];
    expect(viewRows(dated, [col("d", "date")], { ...EMPTY_VIEW, sort: { key: "d", dir: "asc" } }).map((r) => r.fileId)).toEqual(["y", "x", "z"]);
    const named = [row("1", { t: cell({ value: "item 10" }) }), row("2", { t: cell({ value: "item 9" }) })];
    expect(viewRows(named, [col("t", "string")], { ...EMPTY_VIEW, sort: { key: "t", dir: "asc" } }).map((r) => r.fileId)).toEqual(["2", "1"]);
  });
});

describe("progress and the column bulk-verify preview", () => {
  const columns = [col("a", "question"), col("b", "question")];
  const rows = [
    row("1", { a: cell({ verified: true }), b: cell({ stale: true }) }),
    row("2", { a: cell({ edited: true }), b: cell({ status: "failed" }) }),
    row("3", { a: undefined, b: cell({ verified: true, status: "failed" }) }),
    row("4", { a: cell(), b: cell({ verified: true }) }, { missing: true }),
  ];

  it("counts verified cells over every cell of the readable rows", () => {
    expect(verifiedProgress(rows, columns)).toEqual({ verified: 2, total: 6 });
  });

  it("previews what marking a column verified does, as the server decides", () => {
    expect(columnVerifyPreview(rows, columns[0])).toEqual({ eligible: 1, notDone: 1, stale: 0, already: 1, unavailable: 1 });
    expect(columnVerifyPreview(rows, columns[1])).toEqual({ eligible: 0, notDone: 2, stale: 1, already: 0, unavailable: 1 });
  });
});

describe("URL query", () => {
  it("round-trips search, status, value filters and sort, keeping other parameters", () => {
    const view: MatrixView = {
      q: "冷链",
      status: "unverified",
      values: [
        { columnId: "mxcol_pay", op: "eq", value: "预付: 30%" },
        { columnId: "odd:id", op: "empty", value: "" },
      ],
      sort: { key: "mxcol_due", dir: "asc" },
    };
    const params = writeMatrixView("?tab=matrix&view=mx_1&q=old", view);
    expect(params.get("tab")).toBe("matrix");
    expect(params.get("view")).toBe("mx_1");
    expect(params.getAll("f")).toEqual(["status:unverified", "eq:mxcol_pay:预付: 30%", "empty:odd%3Aid"]);
    expect(readMatrixView(`?${params.toString()}`)).toEqual(view);
  });

  it("clears its parameters for an empty view", () => {
    expect(writeMatrixView("?tab=matrix&view=mx_1&q=x&f=status:failed&sort=asc:@file", EMPTY_VIEW).toString()).toBe("tab=matrix&view=mx_1");
  });

  it("ignores malformed and unknown parts", () => {
    const view = readMatrixView("?f=status:nope&f=near:mxcol_a:1&f=eq:mxcol_b:&f=gt&f=eq:%E0%A4%A:x&sort=sideways:mxcol_a&f=gt:mxcol_c:1&f=lt:mxcol_c:5");
    expect(view).toEqual({ q: "", status: null, values: [{ columnId: "mxcol_c", op: "lt", value: "5" }], sort: null });
    expect(readMatrixView("?sort=desc:@file").sort).toEqual({ key: FILE_SORT, dir: "desc" });
  });
});
