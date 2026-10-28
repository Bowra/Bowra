import { describe, expect, it } from "vitest";

import { prettifyInputPreview } from "./format-input-preview";

describe("prettifyInputPreview", () => {
  it("returns undefined for null / empty", () => {
    expect(prettifyInputPreview(undefined)).toBeUndefined();
    expect(prettifyInputPreview(null)).toBeUndefined();
    expect(prettifyInputPreview("")).toBeUndefined();
    expect(prettifyInputPreview("   ")).toBeUndefined();
  });

  it("returns plain string unchanged (already-clean label)", () => {
    expect(prettifyInputPreview("sales.xlsx")).toBe("sales.xlsx");
    expect(prettifyInputPreview("2026年5月各品牌营业额排名")).toBe(
      "2026年5月各品牌营业额排名",
    );
  });

  it("bi_query queries[] → first query + count of remaining", () => {
    expect(
      prettifyInputPreview('{"queries":["2026年5月 各品牌 营业额 排名"],"limit":50}'),
    ).toBe("2026年5月 各品牌 营业额 排名");
    expect(
      prettifyInputPreview('{"queries":["q1","q2","q3"],"limit":10}'),
    ).toBe("q1 +2 more");
  });

  it("single query / q / text → that field", () => {
    expect(prettifyInputPreview('{"query":"近一周营业额"}')).toBe("近一周营业额");
    expect(prettifyInputPreview('{"q":"GMV trend"}')).toBe("GMV trend");
    expect(prettifyInputPreview('{"text":"RFM 客户分类算法"}')).toBe("RFM 客户分类算法");
  });

  it("subagent_spawn { profile, task } → [profile] task", () => {
    expect(
      prettifyInputPreview('{"profile":"compute","task":"基于销售数据做趋势分析"}'),
    ).toBe("[compute] 基于销售数据做趋势分析");
  });

  it("python_sandbox { name, script } → name", () => {
    expect(
      prettifyInputPreview('{"name":"python_sandbox","script":"import pandas..."}'),
    ).toBe("python_sandbox");
  });

  it("parse_file { filename } → filename", () => {
    expect(prettifyInputPreview('{"filename":"sales.xlsx"}')).toBe("sales.xlsx");
  });

  it("generic object → first string field", () => {
    expect(prettifyInputPreview('{"unknown_kind":"some content here"}')).toBe(
      "some content here",
    );
  });

  it("malformed JSON → falls back to raw (truncated if too long)", () => {
    expect(prettifyInputPreview('{"broken: json')).toBe('{"broken: json');
  });

  it("array → first element", () => {
    expect(prettifyInputPreview('["query1","query2"]')).toBe("query1");
  });

  it("truncates very long strings at MAX_LEN", () => {
    const long = "x".repeat(200);
    const out = prettifyInputPreview(long);
    expect(out?.length).toBeLessThanOrEqual(120);
    expect(out?.endsWith("…")).toBe(true);
  });

  // Phase 1.7 dogfood patch: backend `skill_invoke` emits arguments_preview
  // with raw newlines/control chars inside string values (invalid JSON).
  // Regex extraction fallback must keep step labels human-readable instead
  // of dumping 120-char raw JSON blob.
  describe("invalid-JSON regex fallback (Phase 1.7 dogfood)", () => {
    it("skill_invoke with raw newlines extracts skill name from `name` + `script`", () => {
      const raw =
        '{"name": "python_sandbox", "script": "runner.py", "input": {"code": "import pandas as pd\nimport json\n# 2025\nrev_2025 = 16534928991.197714"}}';
      // JSON.parse fails on raw `\n` inside string — regex fallback hits.
      expect(prettifyInputPreview(raw)).toBe("python_sandbox");
    });

    it("canvas section payload with raw newlines extracts title", () => {
      const raw =
        '{"payload": {"title": "2026年 GMV 同比分析: Q1 持平、Q2 微降", "exec_summary": "H1 整体下滑\n17.3%"}}';
      expect(prettifyInputPreview(raw)).toBe(
        "2026年 GMV 同比分析: Q1 持平、Q2 微降",
      );
    });

    it("exec_summary fallback when no name/title", () => {
      const raw =
        '{"exec_summary": "分析 2026 H1 GMV 同比 2025\n下滑 17.3%", "other": "stuff"}';
      expect(prettifyInputPreview(raw)).toBe(
        "分析 2026 H1 GMV 同比 2025",
      );
    });

    it("falls back to first quoted string value when no specific shape", () => {
      const raw =
        '{"random_key": "some readable label\nwith newline"}';
      expect(prettifyInputPreview(raw)).toBe("some readable label");
    });

    it("truly unparseable JSON gracefully truncates raw (last-resort)", () => {
      const raw = '{this is "totally" not json at all\n\n@#$%}';
      const out = prettifyInputPreview(raw);
      // Should be truncated raw, not crash.
      expect(out).toBeTruthy();
      expect(out!.length).toBeLessThanOrEqual(120);
    });
  });
});
