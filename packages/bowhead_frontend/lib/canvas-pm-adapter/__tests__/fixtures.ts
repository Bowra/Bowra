/**
 * 24 kind ReportSection fixtures + recursive nesting case.
 *
 * Used by round-trip.test.ts to verify lossless canvasToPMDoc / pmDocToSections conversion.
 *
 * All values are synthetic examples for serialization and editing coverage.
 */

import type { ReportSection } from "@/lib/types";

export const FIXTURE_eyebrow: ReportSection = {
  id: "f_eyebrow",
  kind: "eyebrow",
  text: "Q1 销售简报",
};

export const FIXTURE_h1: ReportSection = {
  id: "f_h1",
  kind: "h1",
  text: "2025 年第一季度业绩",
};

export const FIXTURE_meta: ReportSection = {
  id: "f_meta",
  kind: "meta",
  pills: ["2025-Q1", "门店数据", "已审"],
};

export const FIXTURE_kpi_row: ReportSection = {
  id: "f_kpi",
  kind: "kpi_row",
  cells: [
    { lbl: "总营业额", val: "¥96.27 亿", sub: "+12% YoY", subTone: "pos" },
    { lbl: "Top1 门店", val: "示例门店甲", sub: "占比 23%", subTone: "neutral" },
    { lbl: "活跃门店", val: "847", sub: "-3", subTone: "neg" },
  ],
};

export const FIXTURE_data_table: ReportSection = {
  id: "f_table",
  kind: "data_table",
  caption: "Top 5 渠道",
  columns: [
    { key: "ch", header: "渠道" },
    { key: "amt", header: "金额", numeric: true },
  ],
  rows: [
    { ch: "天猫", amt: 347000 },
    { ch: "京东", amt: 230000 },
  ],
};

export const FIXTURE_callout: ReportSection = {
  id: "f_callout",
  kind: "callout",
  label: "重要提醒",
  markdown: "**Q2 起调整**: 渠道占比策略优化, 详见附件.",
};

export const FIXTURE_next_list: ReportSection = {
  id: "f_next",
  kind: "next_list",
  items: [
    { title: "拉 Q2 GMV", detail: "对比 Q1 同期", action: "tool" },
    { title: "看渠道明细", detail: "drill 天猫", action: "tool" },
  ],
};

export const FIXTURE_datarc_iframe: ReportSection = {
  id: "f_iframe1",
  kind: "datarc_iframe",
  cardUrl: "https://demo.datarc.cn/card/abc",
  title: "渠道分布图",
  height: 400,
};

export const FIXTURE_html_artifact: ReportSection = {
  id: "f_html",
  kind: "html_artifact",
  artifact_id: "art_xyz",
  title: "AI 生成的图",
  height: 500,
  sandbox: true,
};

export const FIXTURE_external_iframe: ReportSection = {
  id: "f_ext",
  kind: "external_iframe",
  url: "https://example.com",
  title: "外站",
  height: 400,
};

export const FIXTURE_image: ReportSection = {
  id: "f_image",
  kind: "image",
  artifact_id: "art_1234567890ab",
  caption: "AI 生成的夏季促销海报",
  alt: "冰镇奶茶海报",
};

export const FIXTURE_design: ReportSection = {
  id: "f_design",
  kind: "design",
  title: "夏季新品促销设计稿",
  size_preset: "portrait_3_4",
  canvas_width: 1080,
  canvas_height: 1440,
  background: {
    type: "artifact",
    artifact_id: "art_design_bg123",
    fit: "cover",
    opacity: 1,
  },
  layers: [
    {
      id: "headline",
      type: "text",
      text: "夏日新品",
      x: 0.1,
      y: 0.12,
      w: 0.8,
      h: 0.12,
      font_family: "Noto Sans SC",
      font_size: 72,
      color: "#111111",
      align: "center",
      rotation: 0,
      z: 10,
      locked: false,
    },
    {
      id: "cta",
      type: "text",
      text: "第二杯半价",
      x: 0.18,
      y: 0.76,
      w: 0.64,
      h: 0.08,
      font_family: "Noto Sans SC",
      font_size: 48,
      color: "#ffffff",
      align: "center",
      rotation: 0,
      z: 20,
      locked: false,
    },
  ],
  flattened_artifact_id: null,
  flattened_source_rev: null,
  flattened_at: null,
};

export const FIXTURE_chart: ReportSection = {
  id: "f_chart",
  kind: "chart",
  spec: {
    kind: "bar",
    title: "Top 5 门店",
    rows: [
      { 门店: "示例门店甲", 金额: 12345.67 },
      { 门店: "示例门店乙", 金额: 9876.54 },
    ],
    row_count: 2,
    columns: [
      { key: "门店", header: "门店", type: "categorical" },
      { key: "金额", header: "金额", type: "numeric" },
    ],
    data_artifact_id: "art_roundtrip_example",
    fallback_kind: "table",
  },
};

// Sprint ι (Phase A): code_block fixture for round-trip coverage.
export const FIXTURE_code_block: ReportSection = {
  id: "f_code",
  kind: "code_block",
  call_id: "tl_python_sandbox_42",
  lang: "python",
  code: "import pandas as pd\ndf = pd.read_csv('input.csv')\nprint(df.groupby('store')['amount'].sum())",
  dur_ms: 842,
  stdout_preview: "store_a    1234.5\nstore_b    9876.0\n",
  input_artifact_ids: ["art_input_x"],
  output_artifact_ids: ["art_output_y"],
  summary_payload: { n_lines: 3, statement_summary: "pandas groupby", dur_ms: 842 },
  turn_id: "t_sample_turn",
};

export const FIXTURE_spec_block: ReportSection = {
  id: "f_spec",
  kind: "spec_block",
  format: "graph",
  title: "指标树",
  direction: "TB",
  nodes: [
    { id: "gmv", label: "GMV", emphasis: "highlight" },
    { id: "traffic", label: "客流" },
    { id: "conversion", label: "转化率" },
  ],
  edges: [
    { from: "traffic", to: "gmv", label: "驱动" },
    { from: "conversion", to: "gmv", label: "驱动" },
  ],
};

export const FIXTURE_sources: ReportSection = {
  id: "f_sources",
  kind: "sources",
  label: "来源",
  entries: [
    { tool: "datarc_nl2sql", query: "Q1 销售", artifact_id: "art_roundtrip_example" },
  ],
};

export const FIXTURE_insight_card: ReportSection = {
  id: "f_insight",
  kind: "insight_card",
  title: "渠道头部效应",
  thesis: "Top 3 渠道贡献 ≥ 70% GMV",
  evidence: [
    { kind: "callout", caption: "占比", callout_text: "前 3 名占 73%" },
  ],
  narrative: "头部门店持续主导,长尾增长空间显著.",
  impact: { quantity: 12, unit: "%", direction: "+", timeframe: "Q2", note: "预期" },
  confidence: "high",
};

export const FIXTURE_recommendations: ReportSection = {
  id: "f_recs",
  kind: "recommendations",
  items: [
    {
      priority: "P0",
      action: "调整 Q2 渠道资源分配",
      owner: "运营",
      timeline: "Q2 前两周",
      expected_impact: "GMV +8%",
      rationale: "头部集中度高",
    },
  ],
};

export const FIXTURE_narrative: ReportSection = {
  id: "f_narr",
  kind: "narrative",
  // citation-audit M3: gen3 (`value [N]`) is the only live format; this fixture represents the **DB after cleanup**.
  // The original gen2 `[num:849000:CNY:src:s_001]` turned the round-trip `changed=false` assertion into
  // a lock on "the forward adapter does not bake", freezing review P1 (missing stripLegacy hook in canvas body,
  // raw markers leaking into reports) as expected behavior, directly allowing P1 to evade the M3-1 self-check.
  markdown: "Q1 整体 GMV 同比上升, 天猫渠道贡献最大. 详见附件 84.9万元 [1].",
  unverified_numbers: ["849000"],
};

export const FIXTURE_deck: ReportSection = {
  id: "f_deck",
  kind: "deck",
  markdown: "# 简报\n\n这是一段 deck markdown.\n\n- 项 A\n- 项 B",
};

export const FIXTURE_section: ReportSection = {
  id: "f_section",
  kind: "section",
  label: "Q1 业绩",
  h2: "营业额详细分析",
  body: [
    {
      id: "f_section_child_h1",
      kind: "h1",
      text: "Q1 子小标题",
    },
    {
      id: "f_section_child_narr",
      kind: "narrative",
      markdown: "嵌套的 narrative 段落.",
    },
  ],
};

/** Single fixtures for 23 kinds (section is separate for recursive tests). */
export const ALL_SINGLE_FIXTURES: ReportSection[] = [
  FIXTURE_eyebrow,
  FIXTURE_h1,
  FIXTURE_meta,
  FIXTURE_kpi_row,
  FIXTURE_data_table,
  FIXTURE_callout,
  FIXTURE_next_list,
  FIXTURE_datarc_iframe,
  FIXTURE_html_artifact,
  FIXTURE_external_iframe,
  FIXTURE_image,
  FIXTURE_design,
  FIXTURE_chart,
  FIXTURE_code_block,
  FIXTURE_spec_block,
  FIXTURE_sources,
  FIXTURE_insight_card,
  FIXTURE_recommendations,
  FIXTURE_narrative,
  FIXTURE_deck,
];

/** All 24 kinds. */
export const ALL_FIXTURES: ReportSection[] = [
  ...ALL_SINGLE_FIXTURES,
  FIXTURE_section,
];

// ────────────────────────────────────────────────────────────
// W3-9: source_refs fixture for round-trip preservation tests
// (covers the three kinds supporting source_refs: narrative / recommendations / insight_card)
// ────────────────────────────────────────────────────────────

export const FIXTURE_narrative_with_refs: ReportSection = {
  id: "f_narr_refs",
  kind: "narrative",
  markdown: "Q1 GMV 84.9万元 [1], 主因 [2] 天猫渠道转化提升.",
  // Omit unverified_numbers (round-trip drops an empty array to undefined, unrelated to source_refs tests)
  source_refs: ["s_001", "s_002"],
};

export const FIXTURE_recommendations_with_refs: ReportSection = {
  id: "f_recs_refs",
  kind: "recommendations",
  items: [
    {
      priority: "P0",
      action: "复盘 [ref:s_010] 头部门店运营策略",
      owner: "运营",
      timeline: "Q2 前两周",
      expected_impact: "GMV +5%",
      rationale: "Top 3 集中度高 [ref:s_011]",
    },
  ],
  source_refs: ["s_010", "s_011"],
};

export const FIXTURE_insight_card_with_refs: ReportSection = {
  id: "f_insight_refs",
  kind: "insight_card",
  title: "渠道头部效应",
  thesis: "Top 3 渠道贡献 ≥ 70% GMV",
  evidence: [
    { kind: "callout", caption: "占比", callout_text: "前 3 名占 73%" },
  ],
  narrative: "头部门店持续主导, 长尾增长空间显著.",
  impact: { quantity: 12, unit: "%", direction: "+", timeframe: "Q2" },
  confidence: "high",
  source_refs: ["s_020", "s_021", "s_022"],
};

export const SOURCE_REFS_FIXTURES: ReportSection[] = [
  FIXTURE_narrative_with_refs,
  FIXTURE_recommendations_with_refs,
  FIXTURE_insight_card_with_refs,
];

/** Markdown round-trip test samples. */
export const MARKDOWN_SAMPLES = [
  "纯文本一行",
  "# 一级标题\n\n段落内容.",
  "- 项 A\n- 项 B\n- 项 C",
  "**加粗** 和 *斜体* 混合.",
  // citation-audit M3: gen3 shape (`[N]` is plain text in Markdown, not interpreted by markdown-pm)
  "Q1 GMV 84.9万元 [1], 主因 [2] X 渠道.",
];
