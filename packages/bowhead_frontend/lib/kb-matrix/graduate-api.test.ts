import { afterEach, describe, expect, it, vi } from "vitest";

import type { ExtractField } from "@/lib/kb-knowledge-tables-api";

import { MatrixApiError, fieldsToColumns, graduateMatrix, isColumnGroupKind, readinessOf } from "./graduate-api";

function field(over: Partial<ExtractField> & Pick<ExtractField, "name" | "type">): ExtractField {
  return { description: "", required: false, cross_verify: false, extraction_hint: "", ...over };
}

describe("readinessOf（对话框就绪度三桶）", () => {
  it("currency/number → 可聚合（附单位未声明提示）", () => {
    expect(readinessOf("currency")).toEqual({ k: "agg", unit: true });
    expect(readinessOf("number")).toEqual({ k: "agg", unit: true });
  });
  it("select/multi_select/boolean → 可分类汇总", () => {
    for (const t of ["select", "multi_select", "boolean"]) expect(readinessOf(t).k).toBe("cat");
  });
  it("string/date/question/未知 → 仅列举与筛选", () => {
    for (const t of ["string", "date", "question", "whatever"]) expect(readinessOf(t)).toEqual({ k: "list", unit: false });
  });
});

describe("isColumnGroupKind（套用分流）", () => {
  it("seed/template/graduated 是列组模板；manual/undefined 不是", () => {
    expect(isColumnGroupKind("seed")).toBe(true);
    expect(isColumnGroupKind("template")).toBe(true);
    expect(isColumnGroupKind("graduated")).toBe(true);
    expect(isColumnGroupKind("manual")).toBe(false);
    expect(isColumnGroupKind(undefined)).toBe(false);
  });
});

describe("fieldsToColumns（套用列预填 = 逆 build_graduation_fields）", () => {
  it("description 即原 question；options/hint/cross_verify 透传；空 description 回落列名", () => {
    const cols = fieldsToColumns([
      field({ name: "合同金额", type: "currency", description: "合同总金额是多少？" }),
      field({ name: "情感倾向", type: "select", description: "倾向？", options: ["正面", "负面"] }),
      field({ name: "竞业条款", type: "string", description: "", extraction_hint: "合同是否包含竞业限制条款？", cross_verify: true }),
    ]);
    expect(cols[0]).toEqual({ name: "合同金额", type: "currency", question: "合同总金额是多少？", extraction_hint: undefined, options: undefined, cross_verify: undefined });
    expect(cols[1].options).toEqual(["正面", "负面"]);
    expect(cols[2]).toMatchObject({ question: "竞业条款", extraction_hint: "合同是否包含竞业限制条款？", cross_verify: true });
  });
  it("空 options 数组不发（服务端 options_not_applicable 面）", () => {
    expect(fieldsToColumns([field({ name: "a", type: "string", options: [] })])[0].options).toBeUndefined();
  });
});

describe("graduateMatrix（typed error 契约）", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("409 doc_type_name_taken → MatrixApiError 携 suggestion", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: { error: { code: "doc_type_name_taken", suggestion: "合同审阅 · 2" } } }), { status: 409 })),
    );
    const err = await graduateMatrix("mx_1", { name: "合同审阅", mode: "snapshot" }).catch((e) => e);
    expect(err).toBeInstanceOf(MatrixApiError);
    expect(err.code).toBe("doc_type_name_taken");
    expect(err.status).toBe(409);
    expect(err.payload.suggestion).toBe("合同审阅 · 2");
  });

  it("2xx → GraduateResult 原样返回", async () => {
    const body = { doc_type_id: "dt_9", mode: "snapshot", records: 12, columns: 7, name: "n", reused: false };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
    await expect(graduateMatrix("mx_1", { name: "n", mode: "snapshot" })).resolves.toEqual(body);
  });
});
