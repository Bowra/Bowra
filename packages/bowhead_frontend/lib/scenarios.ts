/**
 * Mock scenarios—development timelines used before Gateway SSE is connected.
 * Real path: `useTurnRunner`'s `useRealSSE` branch.
 *
 * Aligned with Claude Design handoff (scenarios.jsx).
 *
 * Development / e2e fixtures, used only under NEXT_PUBLIC_BOWHEAD_SSE_MODE=mock; do not translate fixture data.
 * Production builds always use `sse`; Chinese here never appears in production UI or reaches models/backend.
 * Only mock branches may import this file (consumer allowlist in `scenarios.test.ts`);
 * before adding a consumer, confirm it reads this data only in mock mode.
 */

import type { Scenario, SessionGroup } from "./types";

export const SCENARIOS: Record<string, Scenario> = {
  lookup: {
    id: "lookup",
    label: "Lookup · GMV 速查",
    user: "今年营业额是多少？",
    sessionTitle: "今年 GMV 速查",
    turnId: "t_5c0a1af430c0",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 250, kind: "intent", intent: "lookup", complexity: 0.15, rationale: "简单的指标查询，单一时间窗口。" },
      { at: 500, kind: "trace_step", step: { id: "1", title: "Classification", dur: "15ms", kind: "success", detail: { intent: "lookup", complexity: 0.15, model: "qwen-turbo" } } },
      { at: 700, kind: "trace_step", step: { id: "2", title: "Lead Plan", dur: "210ms", kind: "success", detail: { "prompt tokens": "4,651", model: "qwen-plus", strategy: "single-tool" } } },
      { at: 1000, kind: "tool_call", id: "tc1", name: "bi.ask_question", input: '{"text":"今年营业额"}' },
      { at: 1900, kind: "tool_result", id: "tc1", dur: 226, output: '{"type":"metric","metrics":[...]}', sql: `SELECT SUM(gmv) AS m17\nFROM sales_fact\nWHERE date BETWEEN '2025-01-01' AND CURRENT_DATE\n  AND status = 'paid';` },
      { at: 2200, kind: "partial_answer", text: "今年营业额为 " },
      { at: 2350, kind: "partial_answer", text: "**¥5,955,411,305.39**" },
      { at: 2500, kind: "partial_answer", text: "（约 **59.55 亿元**），" },
      { at: 2700, kind: "partial_answer", text: "相较去年同期增长 **+2.3%**。" },
      { at: 2900, kind: "artifact", card: {
        type: "metric", kw: "今年营业额",
        value: "5,955,411,305.39", currency: "¥",
        approx: "约 59.55 亿", delta: "+2.3%", deltaPos: true,
        sql: `SELECT SUM(gmv) AS m17\nFROM sales_fact\nWHERE date BETWEEN '2025-01-01' AND CURRENT_DATE\n  AND status = 'paid';`,
        meta: { Model: "sales_model", Dimensions: "[]", Measures: "[营业额 (m17)]", Filters: "[today in 2025]" },
      } },
      { at: 3100, kind: "source", text: "datarc · 今年营业额", url: "#" },
      { at: 3300, kind: "done", summary: { total: "7.24s", cost: "$0.0019", llm: 2, tools: 1 } },
    ],
  },

  causal: {
    id: "causal",
    label: "Causal · 华东 GMV 归因",
    user: "为什么华东 Q3 GMV 同比下滑？拆一下原因。",
    sessionTitle: "华东 Q3 下滑归因",
    turnId: "t_c386869083ea",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 280, kind: "intent", intent: "causal", complexity: 0.78, rationale: "多维度下钻，归因任务。" },
      { at: 500, kind: "trace_step", step: { id: "1", title: "Classification", dur: "18ms", kind: "success", detail: { intent: "causal", complexity: 0.78 } } },
      { at: 700, kind: "trace_step", step: { id: "2", title: "Lead Plan", dur: "340ms", kind: "success", detail: { "prompt tokens": "5,212", strategy: "multi-tool: list_dims → attribute" } } },
      { at: 1000, kind: "tool_call", id: "tc1", name: "bi.list_dimensions", input: '{"model":"sales_model"}' },
      { at: 1500, kind: "tool_result", id: "tc1", dur: 142, output: '[{"name":"省","key":"province"},...]' },
      { at: 1700, kind: "tool_call", id: "tc2", name: "bi.attribute", input: '{"metric":"gmv","period":"2025Q3 vs 2024Q3","filter":"region=华东"}' },
      { at: 3300, kind: "tool_result", id: "tc2", dur: 1640, output: '{"delta":-87420000,...}', sql: `WITH q3_2025 AS (\n  SELECT province, category, SUM(gmv) AS gmv\n  FROM sales_fact\n  WHERE quarter = '2025Q3' AND region = '华东'\n  GROUP BY 1,2\n),\nq3_2024 AS (\n  SELECT province, category, SUM(gmv) AS gmv\n  FROM sales_fact\n  WHERE quarter = '2024Q3' AND region = '华东'\n  GROUP BY 1,2\n)\nSELECT a.province, a.category,\n       (a.gmv - b.gmv) AS delta\nFROM q3_2025 a JOIN q3_2024 b USING (province, category)\nORDER BY ABS(delta) DESC LIMIT 5;` },
      { at: 3600, kind: "partial_answer", text: "华东 Q3 GMV " },
      { at: 3750, kind: "partial_answer", text: "同比下降 **8,742 万元（-8.4%）**。" },
      { at: 3950, kind: "partial_answer", text: "主要拖累集中在 **江苏外套品类** 与 **上海上装品类**：" },
      { at: 4200, kind: "artifact", card: { type: "attribution" } },
      { at: 4500, kind: "partial_answer", text: "\n建议下一步：拉取 江苏 × 外套 的 SKU 明细，并比对线上 / 线下渠道。" },
      { at: 4900, kind: "source", text: "datarc · 华东 Q3 归因报告", url: "#" },
      { at: 5100, kind: "done", summary: { total: "5.10s", cost: "$0.0042", llm: 2, tools: 2 } },
    ],
  },

  trend: {
    id: "trend",
    label: "Trend · 营业额走势",
    user: "今年每月营业额走势怎么样？",
    sessionTitle: "今年月度营业额趋势",
    turnId: "t_864cfc255461",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 250, kind: "intent", intent: "trend", complexity: 0.32, rationale: "时间分组趋势分析。" },
      { at: 500, kind: "trace_step", step: { id: "1", title: "Classification", dur: "12ms", kind: "success", detail: { intent: "trend", complexity: 0.32 } } },
      { at: 700, kind: "trace_step", step: { id: "2", title: "Lead Plan", dur: "180ms", kind: "success", detail: { "prompt tokens": "4,820" } } },
      { at: 1000, kind: "tool_call", id: "tc1", name: "bi.list_measures", input: '{"model":"sales_model"}' },
      { at: 1300, kind: "tool_result", id: "tc1", dur: 96, output: "[...]" },
      { at: 1500, kind: "tool_call", id: "tc2", name: "bi.ask_question", input: '{"text":"今年每月营业额"}' },
      { at: 2400, kind: "tool_result", id: "tc2", dur: 312, output: '{"type":"timeseries","points":[...]}', sql: `SELECT DATE_TRUNC('month', date) AS m,\n       SUM(gmv) AS gmv\nFROM sales_fact\nWHERE date >= '2025-01-01'\nGROUP BY 1 ORDER BY 1;` },
      { at: 2700, kind: "partial_answer", text: "今年 1–9 月累计营业额 " },
      { at: 2900, kind: "partial_answer", text: "**¥5,955,411,305.39**。" },
      { at: 3100, kind: "partial_answer", text: "走势整体上行，**6 月达到峰值 8.42 亿**，" },
      { at: 3300, kind: "partial_answer", text: "7 月有一次回落（618 后效应），8 月企稳。" },
      { at: 3500, kind: "artifact", card: { type: "trend" } },
      { at: 3800, kind: "source", text: "datarc · 今年月度营业额", url: "#" },
      { at: 4000, kind: "done", summary: { total: "4.01s", cost: "$0.0028", llm: 2, tools: 2 } },
    ],
  },

  // D8 e2e: HITL options kind (V2.7 P1)—Lead lists 5 analysis directions at the end, multi-select chips.
  hitl_options: {
    id: "hitl_options",
    label: "HITL · 多选 options",
    user: "Aspen 抗血栓 SFE 数据", sessionTitle: "Aspen SFE",
    turnId: "t_hitl_options_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "lookup", complexity: 0.3, rationale: "ambiguous compound" },
      { at: 400, kind: "partial_answer", text: "你想看什么？比如:\n- 各省份市场潜力\n- 渗透率\n- 高潜医院\n- 区域达成\n- 市场份额变化\n" },
      { at: 600, kind: "clarification_required", payload: {
        request_id: "req_opts_1", turn_id: "t_hitl_options_1",
        workspace_id: "ws_test", token: "__chat_options__",
        rationale: "Lead 列了多个分析方向, 请选你想看的 (可多选)",
        kind: "options", multi_select: true,
        candidates: [
          { value: "各省份市场潜力", label: "各省份市场潜力" },
          { value: "渗透率", label: "渗透率" },
          { value: "高潜医院", label: "高潜医院" },
          { value: "区域达成", label: "区域达成" },
          { value: "市场份额变化", label: "市场份额变化" },
        ],
      } },
      { at: 800, kind: "done", summary: { total: "0.8s", cost: "$0.0001", llm: 1, tools: 0 } },
    ],
  },

  // D8 e2e: HITL task_plan kind (V2.7 P2)—sub-task list + ordering.
  hitl_task_plan: {
    id: "hitl_task_plan",
    label: "HITL · 多选 + 排序 task_plan",
    user: "最近经营怎么样", sessionTitle: "全面经营复盘",
    turnId: "t_hitl_task_plan_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "report", complexity: 0.85 },
      { at: 400, kind: "clarification_required", payload: {
        request_id: "req_tp_1", turn_id: "t_hitl_task_plan_1",
        workspace_id: "ws_test", token: "__task_plan__",
        rationale: "问题较复合, 我拆成 4 个子任务, 请勾选要看的 (默认前 3)",
        kind: "task_plan", multi_select: true,
        candidates: [
          { value: "0", label: "近 7 天 GMV", hint: "时间序列" },
          { value: "1", label: "区域销售 Top 5", hint: "对比维度" },
          { value: "2", label: "异常归因", hint: "为什么下滑" },
          { value: "3", label: "渠道分布", hint: "线上 vs 线下" },
        ],
      } },
      { at: 600, kind: "done", summary: { total: "0.6s", cost: "$0.0001", llm: 1, tools: 0 } },
    ],
  },

  // ─── Sprint ρ Phase D3 e2e scenarios ──────────────────────────────────
  //
  // These power the rho/state-04..10 + journey-j2 specs. Each scenario
  // drives the slots that `synthesiseEvents` in `rho-turn-stream-inner`
  // reads (peersDispatched / clarificationPending / errorReason / traceSteps)
  // so the rho layout surfaces the correct primitives.

  // state-04: Lead spawns a compute peer that runs ~5 tool calls then closes.
  peer: {
    id: "peer",
    label: "ρ State 04 · peer running→done",
    user: "帮我分析下 Q3 营收的同比变化",
    sessionTitle: "Q3 营收同比 (peer)",
    turnId: "t_rho_peer_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "causal", complexity: 0.6 },
      { at: 400, kind: "peer_dispatched", peer: { peerName: "compute", displayName: "营收分析专家", subagentId: "mock_peer_compute", dispatchedAt: Date.now(), parentCtxSummary: "拆 Q3 营收同比" } },
      { at: 600, kind: "tool_call", id: "p1", name: "bi.ask_question", input: '{"text":"Q3 营收"}', subagentId: "mock_peer_compute" },
      { at: 1100, kind: "tool_result", id: "p1", dur: 480, output: "{...}" },
      { at: 1300, kind: "tool_call", id: "p2", name: "bi.attribute", input: '{"metric":"gmv"}', subagentId: "mock_peer_compute" },
      { at: 2200, kind: "tool_result", id: "p2", dur: 870, output: "{...}" },
      { at: 2400, kind: "partial_answer", text: "Q3 营收同比 -8.4%；主要拖累集中在江苏外套。" },
      { at: 2800, kind: "done", summary: { total: "2.8s", cost: "$0.003", llm: 2, tools: 2 } },
    ],
  },

  // state-07: ≥35 tool chips so FoldedRunStub fires (threshold 30 per types).
  long: {
    id: "long",
    label: "ρ State 07 · long-turn fold",
    user: "跑 35 个查询",
    sessionTitle: "35 查询 long-turn",
    turnId: "t_rho_long_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 100, kind: "intent", intent: "compute", complexity: 0.4 },
      // Emit 35 tool_call + tool_result pairs of same name (groups into ToolGroup
      // and then trips FOLDED_RUN_STUB_TRIGGER=30). Spread 80ms apart so they
      // stay sequential under the playback timer budget.
      ...Array.from({ length: 35 }, (_, i) => ({
        at: 200 + i * 60,
        kind: "tool_call" as const,
        id: `l${i}`,
        name: "bi.query",
        input: `{"q":${i}}`,
      })),
      ...Array.from({ length: 35 }, (_, i) => ({
        at: 240 + i * 60,
        kind: "tool_result" as const,
        id: `l${i}`,
        dur: 40,
        output: "ok",
      })),
      { at: 2600, kind: "partial_answer", text: "35 个查询跑完，结果摘要见上。" },
      { at: 2900, kind: "done", summary: { total: "2.9s", cost: "$0.004", llm: 1, tools: 35 } },
    ],
  },

  // state-09: peer runs, up-raises HITL clarification, user picks, peer resumes.
  // We use θ-style clarification_pending so rho InlineClarification renders.
  "peer-hitl": {
    id: "peer-hitl",
    label: "ρ State 09 · peer up-raises HITL",
    user: "拉一下竞品数据帮我对比",
    sessionTitle: "竞品对比 (peer HITL)",
    turnId: "t_rho_peer_hitl_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "lookup", complexity: 0.5 },
      { at: 400, kind: "peer_dispatched", peer: { peerName: "compute", subagentId: "mock_peer_hitl_compute", dispatchedAt: Date.now(), parentCtxSummary: "对比竞品" } },
      { at: 600, kind: "tool_call", id: "ph1", name: "bi.list_dimensions", input: "{}", subagentId: "mock_peer_hitl_compute" },
      { at: 1000, kind: "tool_result", id: "ph1", dur: 320, output: "[...]" },
      { at: 1200, kind: "clarification_pending", pending: {
        candidates: ["阿里", "京东", "拼多多"],
        fallbackText: "其它（手动输入）",
        clarificationId: "clar_peer_1",
      } },
    ],
  },

  // state-10: historical turn (≥5 min old). Scenario seeds a single historical
  // assistant message in `runner.messages[]`. GAP: HistoricalTurn rendering
  // depends on `lastTraceSteps`/`lastSummary` hydration which the mock runner
  // doesn't replay; we surface `.rho-historical-turn` via direct timeline
  // attribute on the rendered Lead. If the rho layer has no historical
  // re-render path from mock yet, the assertion may smoke-only.
  historical: {
    id: "historical",
    label: "ρ State 10 · historical compact",
    user: "回看 10 分钟前的查询",
    sessionTitle: "历史 turn 回放",
    turnId: "t_rho_historical_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "lookup", complexity: 0.2 },
      { at: 400, kind: "partial_answer", text: "（这是 10 分钟前完成的历史 turn 摘要。）" },
      { at: 600, kind: "done", summary: { total: "0.6s", cost: "$0.0002", llm: 1, tools: 0 } },
    ],
  },

  // state-06: failing turn → FinalFailBanner rendered inside Lead body.
  fail: {
    id: "fail",
    label: "ρ State 06 · fail banner",
    user: "超复杂查询触发超时",
    sessionTitle: "失败 turn",
    turnId: "t_rho_fail_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "compute", complexity: 0.95 },
      { at: 400, kind: "tool_call", id: "f1", name: "bi.heavy_compute", input: "{}" },
      { at: 800, kind: "error_set", reason: { source: "turn_failed", reasonCode: "wallclock_exceeded" } },
    ],
  },

  // canvas: compute peer produces a Q3 trend chart; canvas surfaces via
  // canvas_add events emitted from backend (not from mock). Mock scenario
  // exercises the peer block + answer prose only.
  canvas: {
    id: "canvas",
    label: "ρ canvas trend",
    user: "给我做个 Q3 营收趋势图",
    sessionTitle: "Q3 趋势图 (canvas)",
    turnId: "t_rho_canvas_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "trend", complexity: 0.5 },
      { at: 400, kind: "peer_dispatched", peer: { peerName: "compute", subagentId: "mock_peer_canvas_compute", dispatchedAt: Date.now(), parentCtxSummary: "做 Q3 趋势图" } },
      { at: 600, kind: "tool_call", id: "c1", name: "python_sandbox", input: '{"chart":"trend"}', subagentId: "mock_peer_canvas_compute" },
      { at: 1400, kind: "tool_result", id: "c1", dur: 760, output: "{...}" },
      { at: 1600, kind: "partial_answer", text: "Q3 营收趋势图见 canvas。" },
      { at: 2000, kind: "done", summary: { total: "2.0s", cost: "$0.003", llm: 1, tools: 1 } },
    ],
  },

  // state-05: ambiguous query → θ-style clarification inline in Lead body.
  hitl: {
    id: "hitl",
    label: "ρ State 05 · HITL inline",
    user: "最近几年的销售情况",
    sessionTitle: "销售情况 (ambiguous)",
    turnId: "t_rho_hitl_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 200, kind: "intent", intent: "lookup", complexity: 0.4 },
      { at: 400, kind: "clarification_pending", pending: {
        candidates: ["近 3 年", "近 5 年", "近 10 年"],
        fallbackText: "其它（手动输入区间）",
        clarificationId: "clar_hitl_1",
      } },
    ],
  },

  // [verify] ClarificationCard · multi_select · grouped questions + explicit submission
  hitl_ms: {
    id: "hitl_ms",
    label: "[verify] HITL · 多选多问题",
    user: "看看最近经营",
    sessionTitle: "多选多问题验证",
    turnId: "t_hitl_ms_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 150, kind: "intent", intent: "lookup", complexity: 0.5 },
      { at: 300, kind: "clarification_pending", pending: {
        candidates: ["全国", "华东", "华南", "华北", "销售额", "利润率", "环比增速"],
        fallbackText: "请按方向勾选后提交,每个问题可多选。",
        clarificationId: "clar_ms_1",
        questions: [
          { question: "主要看哪个区域?", options: ["全国", "华东", "华南", "华北"], type: "multi_select" },
          { question: "重点看哪些指标?", options: ["销售额", "利润率", "环比增速"], type: "multi_select" },
        ],
      } },
    ],
  },

  // [verify] ClarificationCard · single_select · single question submits on click
  hitl_single: {
    id: "hitl_single",
    label: "[verify] HITL · 单选单问题",
    user: "看看销售",
    sessionTitle: "单选验证",
    turnId: "t_hitl_single_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 150, kind: "intent", intent: "lookup", complexity: 0.3 },
      { at: 300, kind: "clarification_pending", pending: {
        candidates: ["本周销售达成", "重点产品表现", "重点医院销售"],
        fallbackText: "您想分析哪个范围?",
        clarificationId: "clar_single_1",
        questions: [
          { question: "您想分析哪个范围?", options: ["本周销售达成", "重点产品表现", "重点医院销售"], type: "single_select" },
        ],
      } },
    ],
  },

  // [verify] ClarificationCard · document draft pause (Chat Cards Spec §3)
  hitl_doc: {
    id: "hitl_doc",
    label: "[verify] HITL · 文档草稿补充信息",
    user: "按模板生成 Q3 经营复盘",
    sessionTitle: "文档草稿验证",
    turnId: "t_hitl_doc_1",
    timeline: [
      { at: 0, kind: "turn_started" },
      { at: 150, kind: "intent", intent: "report", complexity: 0.5 },
      { at: 300, kind: "clarification_pending", pending: {
        candidates: [],
        fallbackText: "复盘按哪个口径统计销售额?",
        clarificationId: "clar_doc_1",
        purpose: "document_draft",
        questions: [
          { question: "复盘按哪个口径统计销售额?", options: ["含税", "不含税"], type: "single_select" },
        ],
      } },
    ],
  },
};

export const SESSIONS: SessionGroup[] = [
  {
    id: "today",
    label: "TODAY",
    items: [
      { id: "s1", title: "今年 GMV 速查", intents: ["lookup"], time: "12m ago", state: "active" },
      { id: "s2", title: "华东 Q3 下滑归因", intents: ["causal", "lookup"], time: "36m ago", state: "done" },
      { id: "s3", title: "各渠道 ROI 对比", intents: ["compute"], time: "1h ago", state: "done" },
    ],
  },
  {
    id: "yest",
    label: "YESTERDAY",
    items: [
      { id: "s4", title: "品类毛利率排名", intents: ["report"], time: "昨天", state: "done" },
      { id: "s5", title: "GMV 定义校准（待批）", intents: ["modeling"], time: "昨天", state: "pending" },
      { id: "s6", title: "北京新店爬坡曲线", intents: ["trend"], time: "昨天", state: "done" },
    ],
  },
  {
    id: "last7",
    label: "LAST 7 DAYS",
    items: [
      { id: "s7", title: "Q2 复盘报告大纲", intents: ["report"], time: "3d ago", state: "done" },
      { id: "s8", title: "净营业额定义查询", intents: ["lookup"], time: "5d ago", state: "done" },
    ],
  },
];

export function pickScenarioFromText(text: string): string {
  // D8 e2e hooks (magic tokens select hitl scenarios; main flow never triggers them).
  if (text.includes("__e2e_hitl_options__")) return "hitl_options";
  if (text.includes("__e2e_hitl_task_plan__")) return "hitl_task_plan";
  // Sprint ρ Phase D3 e2e keywords → scenario IDs.
  // Order matters: more specific matches come first.
  if (/竞品.*对比|对比.*竞品/.test(text)) return "peer-hitl";
  if (/最近几年|近几年的销售|ambiguous/.test(text)) return "hitl";
  if (/35 个|35个查询|跑 \d+ 个查询/.test(text)) return "long";
  if (/触发超时|超复杂查询|turn_failed/.test(text)) return "fail";
  if (/趋势图|chart|canvas/i.test(text)) return "canvas";
  if (/同比|peer/i.test(text)) return "peer";
  if (/为什么|归因|下滑|拆/.test(text)) return "causal";
  if (/走势|趋势|每月|每天|月度/.test(text)) return "trend";
  return "lookup";
}

/**
 * Sprint ρ Phase D3 — URL `?scenario=X` override.
 * Returns the scenario id only when it matches one in `SCENARIOS`; e2e specs
 * use this to drive scenarios that don't type into the composer (e.g.
 * `historical` State 10). Caller is expected to set `scenarioId` + bump
 * `replayKey` if the value is non-null.
 */
export function pickScenarioFromUrl(search: string): string | null {
  try {
    const params = new URLSearchParams(search);
    const sc = params.get("scenario");
    if (sc && Object.prototype.hasOwnProperty.call(SCENARIOS, sc)) return sc;
  } catch {
    // ignore — SSR-safe fallback
  }
  return null;
}
