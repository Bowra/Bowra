/**
 * kb-matrix M2—snapshot-api mapping + locateExcerpt approx-match unit tests
 * (typed fixtures lock DTO shapes, sharing the e2e-plan mechanism).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { locateExcerpt } from "@/components/knowledge/matrix/cite-popover";

import { MatrixApiError } from "./api-error";
import { buildCellFromWire, fetchMatrixDelta, fetchMatrixSnapshot, variantDisplayValue } from "./snapshot-api";

const WIRE_DONE = {
  status: "done",
  variants: [
    {
      raw: null,
      normalized: { amount: "286000", currency: null },
      is_primary: true,
      extracted_by: "tier1",
      confidence: 0.94,
      source_excerpt: "每年人民币贰拾捌万陆仟元整",
      chunk_id: "chk_1",
      page: 5,
    },
  ],
  fail_reason: null,
  edited: false,
  verified: true,
  stale: true,
  cached: true,
  prompt_version: 2,
  seq: 37,
};

describe("buildCellFromWire", () => {
  it("maps snake_case wire → MatrixCellVM (M2 additive fields)", () => {
    const cell = buildCellFromWire(WIRE_DONE as never);
    expect(cell.status).toBe("done");
    expect(cell.value).toBe("286000");
    expect(cell.verified).toBe(true);
    expect(cell.stale).toBe(true);
    expect(cell.cached).toBe(true);
    expect(cell.chunkId).toBe("chk_1");
    expect(cell.page).toBe(5);
    expect(cell.seq).toBe(37);
  });

  it("failed cell carries failReason and no value", () => {
    const cell = buildCellFromWire({
      status: "failed",
      variants: [],
      fail_reason: "llm_parse_failed",
      edited: false,
      verified: false,
      stale: false,
      cached: false,
      prompt_version: 1,
      seq: 2,
    } as never);
    expect(cell.status).toBe("failed");
    expect(cell.failReason).toBe("llm_parse_failed");
    expect(cell.value).toBe("");
  });

  it("keep_as_text cell (normalized=null, raw set) displays raw", () => {
    expect(
      variantDisplayValue({ raw: "面议（按季度结算）", normalized: null, is_primary: true }),
    ).toBe("面议（按季度结算）");
  });

  it("raw_string sentinel displays the original text", () => {
    expect(variantDisplayValue({ normalized: { raw_string: "约100元" }, is_primary: true })).toBe("约100元");
  });

  // ── M3 new shapes (§4.6) ──
  it("select choice displays the matched option text", () => {
    expect(variantDisplayValue({ normalized: { choice: "高风险" }, is_primary: true })).toBe("高风险");
  });

  it("multi_select choices join with 顿号", () => {
    expect(variantDisplayValue({ normalized: { choices: ["华东", "华南"] }, is_primary: true })).toBe("华东、华南");
  });

  it("boolean flag returns the semantic token (i18n map happens in rendering layers)", () => {
    expect(variantDisplayValue({ normalized: { flag: true }, is_primary: true })).toBe("true");
    expect(variantDisplayValue({ normalized: { flag: false }, is_primary: true })).toBe("false");
  });

  it("primary self_consistency_agree lands on MatrixCellVM (M3 §4.7)", () => {
    const cell = buildCellFromWire({
      ...WIRE_DONE,
      variants: [{ ...WIRE_DONE.variants[0], self_consistency_agree: 0.5 }],
    } as never);
    expect(cell.selfConsistencyAgree).toBe(0.5);
    // Missing key → undefined (side panel omits cv row).
    expect(buildCellFromWire(WIRE_DONE as never).selfConsistencyAgree).toBeUndefined();
  });
});

// ── M3 runs[] mapping + typed errors (§4.5/§4.8) ────────────────

const WIRE_RUN = {
  run_id: "run_a",
  status: "running",
  scope: { kind: "stale_column", column_id: "c1" },
  cells_total: 4,
  cells_done: 1,
  cells_failed: 0,
  skipped_edited: 1,
  error_code: null,
  eta_s: 30,
  initiated_by: "u_1",
  can_cancel: true,
};

const WIRE_FULL = {
  matrix: { matrix_id: "mx_1", name: "M", can_edit: true, seq: 5, structure_seq: 1, poll_ms: 1000, template_doc_type_id: null },
  columns: [
    {
      column_id: "c1",
      name: "级别",
      type: "select",
      question: "q",
      extraction_hint: "",
      prompt_version: 2,
      options: ["A", "B"],
      cross_verify: true,
    },
  ],
  rows: [{ file_id: "f1", file_name: "a.pdf", missing: false, file_version: 1 }],
  cells: {},
  runs: [WIRE_RUN],
  run: WIRE_RUN,
};

function stubFetch(body: unknown, ok = true, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok, status, json: async () => body }) as Response),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("snapshot runs[] mapping (M3)", () => {
  it("maps runs with scope + can_cancel; columns carry options/crossVerify", async () => {
    stubFetch(WIRE_FULL);
    const snap = await fetchMatrixSnapshot("mx_1");
    expect(snap.runs).toHaveLength(1);
    expect(snap.runs![0]).toMatchObject({
      runId: "run_a",
      status: "running",
      scopeKind: "stale_column",
      scopeColumnId: "c1",
      canCancel: true,
    });
    expect(snap.columns[0].options).toEqual(["A", "B"]);
    expect(snap.columns[0].crossVerify).toBe(true);
  });

  it("falls back to [run] when runs missing (镜像回滚窗口旧后端)", async () => {
    const { runs: _drop, ...legacy } = WIRE_FULL;
    stubFetch(legacy);
    const snap = await fetchMatrixSnapshot("mx_1");
    expect(snap.runs).toHaveLength(1);
    expect(snap.runs![0].runId).toBe("run_a");
    // Legacy wire lacking scope/can_cancel leaves fields empty (banner has no prefix/cancel button).
    stubFetch({ ...legacy, run: { ...WIRE_RUN, scope: undefined, can_cancel: undefined } });
    const snap2 = await fetchMatrixSnapshot("mx_1");
    expect(snap2.runs![0].scopeKind).toBeUndefined();
    expect(snap2.runs![0].canCancel).toBeUndefined();
  });

  it("delta carries runs[] (falls back to run)", async () => {
    stubFetch({ seq: 9, cells: {}, runs: [WIRE_RUN], run: WIRE_RUN });
    const res = await fetchMatrixDelta("mx_1", 5);
    expect(res.kind).toBe("delta");
    if (res.kind === "delta") expect(res.delta.runs.map((r) => r.runId)).toEqual(["run_a"]);
    stubFetch({ seq: 9, cells: {}, run: WIRE_RUN });
    const res2 = await fetchMatrixDelta("mx_1", 5);
    if (res2.kind === "delta") expect(res2.delta.runs).toHaveLength(1);
  });

  it("non-2xx throws MatrixApiError carrying status (FE-P2-11 死轮询根因修复)", async () => {
    stubFetch({ detail: "matrix not found" }, false, 404);
    const err = await fetchMatrixSnapshot("mx_gone").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(MatrixApiError);
    expect((err as MatrixApiError).status).toBe(404);
  });
});

describe("locateExcerpt approx-match (M2 第三级降级)", () => {
  const content =
    "第五条 服务费用 5.1 仓储服务费按每年人民币贰拾捌万陆仟元整（¥286,000）计收，含仓租、装卸及日常盘点费用。5.2 乙方于每月末开具发票，甲方收到发票后 30 日内支付。";

  it("exact substring still wins (level 2)", () => {
    const hit = locateExcerpt(content, "每年人民币贰拾捌万陆仟元整（¥286,000）计收");
    expect(hit).not.toBeNull();
    expect(hit!.hit).toContain("贰拾捌万陆仟元整");
  });

  it("lightly rewritten snippet lands via approx sliding window (level 3)", () => {
    // Light LLM rewrite: punctuation/individual characters differ from original, so exact necessarily misses.
    const rewritten = "每年 人民币贰拾捌万陆仟元整(¥286,000)计收，含仓租、装卸及平常盘点费用";
    const hit = locateExcerpt(content, rewritten);
    expect(hit).not.toBeNull();
    expect(hit!.hit).toContain("贰拾捌万陆仟元整");
  });

  it("unrelated text degrades to null (段落级降级小字 by caller)", () => {
    expect(locateExcerpt(content, "本合同自二〇二六年一月一日起生效，有效期三年，期满自动续期")).toBeNull();
  });

  it("short probes never approx-match (防误锚)", () => {
    expect(locateExcerpt(content, "金额为准")).toBeNull();
  });
});
