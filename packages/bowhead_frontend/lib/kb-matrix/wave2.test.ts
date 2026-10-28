/**
 * kb-matrix M3 wave-2 — pure-function unit tests:
 *  - cellChoiceList (multi_select chip arrays: normalized.choices is authoritative + “、” join fallback)
 *  - rowsWithFailedCell / failedCellCount (View Failed filtered row set + count)
 *  - checkNewOption (client select/multi_select option validation, mirroring the server)
 */

import { describe, expect, it } from "vitest";

import { OPTIONS_MAX, checkNewOption } from "./options";
import type { MatrixCellVM, MatrixRowVM } from "./types";
import { cellChoiceList, failedCellCount, rowsWithFailedCell } from "./types";
import { choiceInitial } from "../../components/knowledge/matrix/side-panel";

function mkCell(status: MatrixCellVM["status"], value: string, variants: unknown[] = []): MatrixCellVM {
  return {
    status,
    value,
    variants: variants as MatrixCellVM["variants"],
    edited: false,
    verified: false,
  };
}

function mkRow(fileId: string, cells: Record<string, MatrixCellVM | undefined>): MatrixRowVM {
  return { fileId, fileName: `${fileId}.pdf`, missing: false, extractFailed: false, recordId: "", approved: false, cells };
}

describe("cellChoiceList (multi_select chip 数组)", () => {
  it("reads primary variant normalized.choices (权威)", () => {
    const cell = mkCell("done", "华东、华南", [
      { is_primary: true, normalized: { choices: ["华东", "华南"] } },
    ]);
    expect(cellChoiceList(cell)).toEqual(["华东", "华南"]);
  });

  it("falls back to primary variant when is_primary flag absent", () => {
    const cell = mkCell("done", "A、B", [{ normalized: { choices: ["A", "B", "C"] } }]);
    expect(cellChoiceList(cell)).toEqual(["A", "B", "C"]);
  });

  it("splits the 顿号-joined display value when no variant choices (patch/delta 精简形状)", () => {
    expect(cellChoiceList(mkCell("done", "星舰 Pro、星舰 Air"))).toEqual(["星舰 Pro", "星舰 Air"]);
  });

  it("empty value → empty list", () => {
    expect(cellChoiceList(mkCell("done", ""))).toEqual([]);
  });
});

describe("rowsWithFailedCell / failedCellCount (过滤视图行集)", () => {
  const rows: MatrixRowVM[] = [
    mkRow("f1", { a: mkCell("done", "x"), b: mkCell("failed", "") }),
    mkRow("f2", { a: mkCell("done", "y"), b: mkCell("done", "z") }),
    mkRow("f3", { a: mkCell("failed", ""), b: mkCell("failed", "") }),
    mkRow("f4", { a: undefined, b: mkCell("running", "") }),
  ];

  it("subset = only rows containing ≥1 failed cell, 原序保持", () => {
    expect(rowsWithFailedCell(rows).map((r) => r.fileId)).toEqual(["f1", "f3"]);
  });

  it("failedCellCount counts every failed cell across rows", () => {
    expect(failedCellCount(rows)).toBe(3);
  });

  it("no failures → empty subset + zero count", () => {
    const clean = [mkRow("f1", { a: mkCell("done", "x") })];
    expect(rowsWithFailedCell(clean)).toEqual([]);
    expect(failedCellCount(clean)).toBe(0);
  });
});

describe("checkNewOption (选项客户端校验)", () => {
  it("accepts a fresh trimmed option", () => {
    expect(checkNewOption("华东", ["华南"])).toEqual({ ok: true });
    expect(checkNewOption("  华东  ", [])).toEqual({ ok: true });
  });

  it("empty / whitespace-only silently ignored (ok:false, no error)", () => {
    expect(checkNewOption("   ", ["华南"])).toEqual({ ok: false });
  });

  it("option > 64 chars → too_long", () => {
    expect(checkNewOption("x".repeat(65), [])).toEqual({ ok: false, error: "too_long" });
    expect(checkNewOption("x".repeat(64), [])).toEqual({ ok: true });
  });

  it("duplicate → dup with the existing index", () => {
    expect(checkNewOption("华南", ["华东", "华南", "华北"])).toEqual({ ok: false, error: "dup", dupIndex: 1 });
  });

  it("at 100-option cap → at_limit", () => {
    const full = Array.from({ length: OPTIONS_MAX }, (_, i) => `opt${i}`);
    expect(checkNewOption("new", full)).toEqual({ ok: false, error: "at_limit" });
  });

  it("dup wins over at_limit when the new value already exists at cap", () => {
    const full = Array.from({ length: OPTIONS_MAX }, (_, i) => `opt${i}`);
    expect(checkNewOption("opt3", full)).toEqual({ ok: false, error: "dup", dupIndex: 3 });
  });
});

describe("choiceInitial (侧板编辑初值；wave-2 review F1)", () => {
  const col = (type: string, options?: string[]) =>
    ({ columnId: "c", name: "涉及产品", type, options }) as unknown as import("./types").MatrixColumnVM;

  it("done multi_select：choices⊆options → 原样(no-op)", () => {
    const cell = mkCell("done", "星舰 Pro、星舰 Air", [
      { is_primary: true, normalized: { choices: ["星舰 Pro", "星舰 Air"] } },
    ]);
    expect(choiceInitial(col("multi_select", ["星舰 Pro", "星舰 Air", "星舰 mini"]), cell)).toEqual(["星舰 Pro", "星舰 Air"]);
  });

  it("needs_review multi_select：剥非法片段(可恢复的关键)", () => {
    // Review cell primary is raw_string without choices → cellChoiceList falls back to splitting original text → includes invalid “星舰 X”
    const cell = mkCell("needs_review", "星舰 Pro、星舰 X", [{ is_primary: true, normalized: { raw_string: "星舰 Pro、星舰 X" } }]);
    expect(choiceInitial(col("multi_select", ["星舰 Pro", "星舰 Air", "星舰 mini"]), cell)).toEqual(["星舰 Pro"]);
  });

  it("boolean token → 是/否/null（非本地化 label）", () => {
    expect(choiceInitial(col("boolean"), mkCell("done", "true"))).toBe("是");
    expect(choiceInitial(col("boolean"), mkCell("done", "false"))).toBe("否");
    expect(choiceInitial(col("boolean"), mkCell("done", ""))).toBeNull();
  });

  it("select → 选项串 / 空→null", () => {
    expect(choiceInitial(col("select", ["正面", "负面"]), mkCell("done", "正面"))).toBe("正面");
    expect(choiceInitial(col("select", ["正面", "负面"]), mkCell("done", ""))).toBeNull();
  });
});
