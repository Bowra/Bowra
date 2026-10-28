/**
 * tool-catalog audit-fill test (Phase 1.7-A).
 *
 * Asserts every backend tool name from `tool_registry.py` + `subagent.py`
 * step_started emit sites resolves via getToolMeta. Keeps the catalog in
 * sync with the backend so new tools don't silently render with the raw
 * `tool_name` fallback in production.
 */

import { describe, expect, it } from "vitest";

import { TOOL_CATALOG_KEYS, getToolMeta } from "./tool-catalog";

// Mirror of tech-design §2.4 enumeration — keep in sync with backend
// `bowhead_core/tool_registry.py` + `bowhead_core/subagent.py`
// step_started emit sites.
const BACKEND_TOOL_NAMES: ReadonlyArray<string> = [
  // pre-1.7-A baseline
  "parse_file",
  "bi_query",
  "web_search",
  "kb_search",
  "subagent_spawn",
  "lead_thinking_writing_python",
  "lead_thinking_writing_sql",
  "python_sandbox",
  // 1.7-A audit-fill: BI meta + extended query
  "bi_list_measures",
  "bi_list_dimensions",
  "bi_schema",
  "bi_fetch_rows",
  "bi_attribute",
  "bi_breakdown",
  "bi_get_card",
  "bi_get_cards",
  "bi_explain",
  "bi_plan_query",
  "bi_fetch_account",
  // 1.7-A audit-fill: KB tools
  "kb_list_tables",
  "kb_query_records",
  "kb_get_record",
  // 1.7-A audit-fill: canvas writers
  "add_chart",
  "canvas_add_kpi_row",
  "canvas_add_table",
  "canvas_add_data_table",
  "canvas_add_insight",
  "canvas_add_recommendations",
  // 1.7-A audit-fill: BI async LLM
  "bi_call_llm_async",
  "bi_get_llm_result",
];

describe("tool-catalog audit-fill (Phase 1.7-A)", () => {
  it.each(BACKEND_TOOL_NAMES)("getToolMeta('%s') resolves with non-null meta", (name) => {
    const meta = getToolMeta(name);
    expect(meta).not.toBeNull();
    expect(meta?.name).toBeTruthy();
    expect(meta?.icon).toBeTruthy();
    expect(meta?.cat).toMatch(/^(parse|query|agent|write|run)$/);
  });

  it("unknown tool names return null (renderer falls back to raw name)", () => {
    expect(getToolMeta("nonexistent_tool")).toBeNull();
    expect(getToolMeta("")).toBeNull();
    expect(getToolMeta(null)).toBeNull();
    expect(getToolMeta(undefined)).toBeNull();
  });

  it("every catalog entry has a Chinese display name (non-Latin char)", () => {
    for (const key of TOOL_CATALOG_KEYS) {
      const meta = getToolMeta(key)!;
      // Catch entries where someone forgot to localize.
      const hasChinese = /[一-龥]/.test(meta.name);
      expect(hasChinese, `tool='${key}' name='${meta.name}' missing Chinese chars`).toBe(true);
    }
  });

  it("canvas write tools have cat=write", () => {
    // ⚠ Do not filter only by the `canvas_add_` prefix: renamed `add_chart` / `add_image` lack it
    // (docs/design/工具改名迁移.md); prefix-only filtering would **silently omit both** from checking.
    const canvasTools = [...TOOL_CATALOG_KEYS].filter(
      (k) => k.startsWith("canvas_add_") || k === "add_chart" || k === "add_image",
    );
    expect(canvasTools).toContain("add_chart");
    expect(canvasTools).toContain("add_image");
    expect(canvasTools.length).toBeGreaterThanOrEqual(6);
    for (const t of canvasTools) {
      expect(getToolMeta(t)?.cat).toBe("write");
    }
  });

  it("bi_* / kb_* tools default to cat=query (read-only meta)", () => {
    const queryTools = [
      "bi_list_measures",
      "bi_list_dimensions",
      "bi_schema",
      "kb_list_tables",
      "kb_query_records",
    ];
    for (const t of queryTools) {
      expect(getToolMeta(t)?.cat).toBe("query");
    }
  });
});
