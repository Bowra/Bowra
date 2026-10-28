/**
 * Interface language for tool names/results (i18n cleanup batch 1).
 *
 * These modules are pure functions (outside the React tree): tool names come from a YAML-generated table; other copy uses
 * `staticTranslator()` with the messages `tool_text` namespace and `getCurrentLocale()`. Enforce three contracts:
 *   1. Chinese interface output remains verbatim (expectations captured before the change);
 *   2. English/German/French interfaces show no Chinese here; key German/French tool copy uses the respective language;
 *   3. Step-row text rendered live and during replay is identical character for character.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => Object.assign((key: string) => key, { rich: (key: string) => key, has: () => true }),
}));

import type { TurnEventDTO } from "@/lib/api";
import { TOOL_DISPLAY } from "@/lib/_generated/tool-display";
import type { Locale } from "@/lib/locale";
import { builtinTools, toolDisplay, localizedToolText } from "@/lib/tool-display";
import { STATIC_TOOL_NAMES, toolActionLine, toolLabel, toolResultLine } from "@/lib/tool-labels";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { AgentTrace } from "@/components/chat/step-list/AgentTrace";
import { stepsToStepViews } from "@/components/chat/step-list/step-view";
import { getToolMeta, TOOL_CATALOG_KEYS } from "@/components/chat/step-list/tool-catalog";

const HAN = /\p{Script=Han}/u;

function setLocale(locale: Locale): void {
  document.documentElement.lang = locale;
}

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  document.documentElement.lang = "";
});

// Legacy historical-trace names (dotted names, retired tools) + pseudo-steps from catalog overrides + active tools.
const LEGACY_NAMES = [
  "bi.ask_question", "bi.fetch_rows", "bi.profile", "bi.attribute", "bi.breakdown", "bi.list_measures",
  "bi.list_dimensions", "bi.get_card", "bi.get_cards", "bi.explain", "bi.plan_query", "bi.fetch_account",
  "bi.call_llm_async", "bi.get_llm_result", "bi.update_measures", "bi.create_measures", "bi.update_dimensions",
  "bi.delete_measures", "skill.list", "skill.read", "skill.invoke", "skill.save_generated", "subagent.spawn",
  "subagent.return", "canvas.add_chart", "canvas.add_image", "canvas.add_insight", "canvas.add_iframe",
  "canvas.add_html_artifact", "canvas.add_external_iframe", "image.generate", "web.search", "external_iframe",
  "html_artifact", "bi_ask_question", "bi_profile", "kb_read", "canvas_add_narrative", "canvas_add_section",
  "canvas_add_chart", "canvas_add_image",
];
const ALL_NAMES = [
  ...new Set([...Object.keys(TOOL_DISPLAY), ...TOOL_CATALOG_KEYS, ...STATIC_TOOL_NAMES, ...LEGACY_NAMES]),
].sort();

// ASCII-only inputs: any Chinese output necessarily indicates untranslated interface copy.
const ARGS: Array<Record<string, unknown>> = [
  {},
  { text: "inventory turnover" },
  { question: "why did east drop", comparison_type: "yoy" },
  { name: "analyst-sop" },
  { profile: "compute", task: "attribution" },
  { profile: "debate", task: "x" },
  { profile: "unknown-role" },
  { query: "latest GDP" },
  { title: "Monthly board" },
  { kind: "both" },
  { queries: ["east sales", "south sales"] },
  { bail_kind: "ref_unresolvable" },
  { code: "print(1)" },
];

const PREVIEWS: Array<[string, boolean, string | null, Record<string, unknown> | null]> = [
  ["", false, null, null],
  [JSON.stringify({ type: "metric", metrics: [{ name: "GMV", content: "12" }] }), true, null, null],
  [JSON.stringify({ type: "image" }), true, null, null],
  [JSON.stringify({ type: "unsupported" }), true, null, null],
  [JSON.stringify({ type: "table" }), true, null, null],
  ["", true, null, { type: "ask_question", metric_count: 3, q_type: "metric" }],
  ["", true, null, { type: "attribute", row_count: 4, measure_name: "GMV" }],
  ["", true, null, { type: "attribute", row_count: 1 }],
  [JSON.stringify([1, 2, 3]), true, null, null],
  [JSON.stringify({ items: [1, 2] }), true, null, null],
  [JSON.stringify({ body: "abcd" }), true, null, null],
  [JSON.stringify({ tool_count: 3, tokens_input: 1200 }), true, null, null],
];

/** All display output of one tool (excluding rendering). */
function textsOf(name: string): string[] {
  const out: string[] = [];
  const label = toolLabel(name);
  out.push(label.name);
  const disp = toolDisplay(name);
  out.push(disp.label, disp.blurb);
  const meta = getToolMeta(name);
  if (meta) out.push(meta.name, meta.activeName ?? "");
  for (const a of ARGS) out.push(toolActionLine(name, JSON.stringify(a)));
  for (const [p, ok, err, m] of PREVIEWS) out.push(toolResultLine(name, p, ok, err, m));
  return out;
}

describe("中文界面逐字不变", () => {
  it("工具名：生成表、历史旧名、步骤卡目录", () => {
    setLocale("zh-CN");
    expect(toolDisplay("bi_query")).toEqual({ label: "查询数据", blurb: "用自然语言从已连接的数据源取数，返回明细或汇总行。", sigil: "🔍", category: "bi" });
    expect(toolLabel("bi.ask_question")).toEqual({ name: "问 BI", family: "bi", emoji: "🔢" });
    expect(toolLabel("kb_read").name).toBe("读知识库");
    expect(toolLabel("canvas_add_narrative").name).toBe("加叙述");
    expect(toolLabel("web_fetch").name).toBe("读取网页");
    expect(toolLabel("bi_schema").name).toBe("查看数据字段");
    const meta = (n: string) => {
      const m = getToolMeta(n);
      return m && [m.name, m.activeName];
    };
    expect(meta("parse_file")).toEqual(["解析文件", undefined]);
    expect(meta("subagent_spawn")).toEqual(["调用", undefined]);
    expect(meta("lead_thinking_writing_python")).toEqual(["写代码", "正在写代码"]);
    expect(meta("lead_thinking_writing_sql")).toEqual(["写查询", "正在写查询"]);
    expect(meta("python_sandbox")).toEqual(["运行代码", "运行代码"]);
    expect(meta("bi_list_measures")).toEqual(["列指标", undefined]);
    expect(meta("canvas_add_kpi_row")).toEqual(["加 KPI", undefined]);
    expect(meta("bi_call_llm_async")).toEqual(["LLM 异步调用", undefined]);
    expect(meta("skill_list_for_role")).toEqual(["列 skill", undefined]);
    expect(meta("subagent_return")).toEqual(["整理结果", "正在整理结果"]);
    expect(meta("memory_write")).toEqual(["记忆写入", undefined]);
    expect(meta("teammcp.src_1.list_orders")).toEqual(["list_orders", undefined]);
  });

  it("调用参数与结果短句", () => {
    setLocale("zh-CN");
    expect(toolActionLine("bi.list_measures", "")).toBe("列出指标（构造查询前看有啥可用）");
    expect(toolActionLine("subagent.spawn", JSON.stringify({ profile: "compute", task: "归因" }))).toBe("派智能体（compute） │ 归因");
    // Since 7162e70ee only finite reason codes are localized; free-form error prose is not shown.
    expect(toolResultLine("bi.ask_question", "", false, "llm_provider_timeout")).toBe(
      "失败：等待模型回复超时。模型未在规定时间内完成回复，本轮未能完成。可以稍后重试。",
    );
    expect(toolResultLine("bi.ask_question", "", false, "超时")).toBe("失败：未知错误");
    expect(toolResultLine("bi.ask_question", "", false, null)).toBe("失败：未知错误");
    expect(toolResultLine("bi.ask_question", JSON.stringify({ type: "image" }), true)).toBe("返回图表（datarc 直接给图）");
    expect(toolResultLine("bi.ask_question", JSON.stringify({ type: "unsupported" }), true)).toBe("datarc 表示无法直答（需换问法 / 改工具）");
    expect(toolResultLine("bi.attribute", "", true, null, { type: "attribute", row_count: 4, measure_name: "GMV" })).toBe("归因 4 个维度（GMV）");
    expect(toolResultLine("bi.get_card", JSON.stringify([1, 2, 3]), true)).toBe("拿到 3 张图表卡");
    expect(toolResultLine("skill.list", JSON.stringify({ items: [1, 2] }), true)).toBe("共 2 个 Skill");
    expect(toolResultLine("skill.read", JSON.stringify({ body: "abcd" }), true)).toBe("读到 4 字");
    expect(toolResultLine("subagent.spawn", JSON.stringify({ tool_count: 3, tokens_input: 1200 }), true)).toBe("智能体完成（3 次工具 / 1200 tok）");
    expect(toolResultLine("canvas_add_insight", "", true)).toBe("完成");
  });
});

describe.each(["en-US", "de-DE", "fr-FR"] as const)("%s 界面：工具名与工具结果不含中文", (locale) => {
  it("所有工具名、调用参数、结果短句与抽屉", () => {
    setLocale(locale);
    const leaked: string[] = [];
    for (const name of ALL_NAMES) {
      for (const text of textsOf(name)) if (HAN.test(text)) leaked.push(`${name}: ${text}`);
    }
    for (const cap of builtinTools()) {
      if (HAN.test(cap.name) || HAN.test(cap.blurb)) leaked.push(`capability ${cap.ref}: ${cap.name} / ${cap.blurb}`);
    }
    expect(leaked).toEqual([]);
  });

  it("现役工具有本语言名字（不是工具代号）", () => {
    setLocale(locale);
    expect(Object.keys(TOOL_DISPLAY).filter((t) => toolDisplay(t).label === t || toolLabel(t).name === t)).toEqual([]);
  });
});

describe("德法工具使用本语言文案", () => {
  it.each([
    ["de-DE", "Daten abfragen", "Daten aus der verbundenen Datenquelle mit einer natürlichsprachlichen Anfrage abrufen."],
    ["fr-FR", "Interroger les données", "Récupérer les données de la source connectée à partir d’une demande en langage naturel."],
  ] as const)("%s：数据查询工具", (locale, label, blurb) => {
    setLocale(locale);
    expect(toolDisplay("bi_query")).toMatchObject({ label, blurb });
    expect(toolLabel("bi_query").name).toBe(label);
  });
});

// ─── Step rows: identical live/replay rendering ────────────────────────────────────────

type Ev = [string, Record<string, unknown>];

function callEvents(turnId: string, tool: string, writing: boolean): Ev[] {
  let seq = 0;
  const e = (name: string, p: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...p }];
  if (writing) {
    return [
      e("step_list_card_created", {}),
      e("step_started", { step_id: "w1", step_type: "lead_thinking_writing", header_zh: "", tool_name: tool, call_id: "c1" }),
      e("step_progress", { step_id: "w1", content_delta: '{"code": "print(1)"}' }),
    ];
  }
  return [
    e("step_list_card_created", {}),
    e("step_started", { step_id: "s1", step_type: "tool_run", header_zh: "", tool_name: tool, call_id: "c1" }),
    e("step_done", { step_id: "s1", state: "success", duration_ms: 300, tool_name: tool, call_id: "c1" }),
  ];
}

function toRows(events: Ev[]): TurnEventDTO[] {
  return events.map(([name, payload], i) => ({ seq: i + 1, event_type: name, payload, occurred_at: "2026-09-29T08:00:00.000Z" }));
}

function renderedToolName(turnId: string): string {
  const st = getTurnSnapshot(turnId);
  const views = st ? stepsToStepViews(st.steps, st.peerTerminalById) : [];
  const { container, unmount } = render(<AgentTrace steps={views} peerNames={{}} peerProse={{}} />);
  const names = [...container.querySelectorAll(".stp .stp-tool")].map((n) => n.textContent ?? "");
  unmount();
  return names.join(" | ");
}

function stepRowName(tool: string, writing: boolean, tag: string): string {
  for (const [name, payload] of callEvents(`live-${tag}`, tool, writing)) dispatchToTurnStore(name, payload);
  replayTurnEventRows(`replay-${tag}`, toRows(callEvents(`replay-${tag}`, tool, writing)));
  const live = renderedToolName(`live-${tag}`);
  expect(renderedToolName(`replay-${tag}`)).toBe(live);
  return live;
}

describe("步骤行工具名（live 与重放逐字相等）", () => {
  // The glyph renders as an icon (no text), followed by a space and the tool name.
  it("中文界面的工具名文案不变", () => {
    setLocale("zh-CN");
    expect(stepRowName("bi_query", false, "zh-bi")).toBe(" 查询数据");
    expect(stepRowName("bi_query", true, "zh-bi-w")).toBe(" 正在写查询");
    expect(stepRowName("python_sandbox", true, "zh-py-w")).toBe(" 正在写代码");
    expect(stepRowName("subagent_return", false, "zh-ret")).toBe(" 整理结果");
    expect(stepRowName("canvas_add_kpi_row", false, "zh-kpi")).toBe(" 加 KPI");
    expect(stepRowName("memory_search", false, "zh-mem")).toBe(" 搜索记忆");
  });

  it.each(["en-US", "de-DE", "fr-FR"] as const)("%s 界面每个工具的步骤行都没有中文", (locale) => {
    setLocale(locale);
    const leaked: string[] = [];
    for (const tool of ALL_NAMES) {
      for (const writing of [false, true]) {
        const text = stepRowName(tool, writing, `${locale}-${tool}-${writing}`);
        if (HAN.test(text)) leaked.push(`${tool}${writing ? " (writing)" : ""}: ${text}`);
      }
    }
    expect(leaked).toEqual([]);
  });
});

describe("localized authoring catalog tool text", () => {
  it.each([
    ["de-DE", "Daten abfragen", "Zeilen abrufen."],
    ["fr-FR", "Interroger les données", "Récupérer des lignes."],
  ] as const)("uses %s names and descriptions from the catalog response", (locale, name, description) => {
    setLocale(locale);
    const legacy = { display_name: "查询数据", description: "取数。", display_name_en: "Query Data", description_en: "Pull rows." };
    const tool = { ...legacy, display_name_de: "Daten abfragen", description_de: "Zeilen abrufen.", display_name_fr: "Interroger les données", description_fr: "Récupérer des lignes." };
    expect(localizedToolText(tool)).toEqual({ name, description });
    expect(localizedToolText(legacy)).toEqual({ name: "Query Data", description: "Pull rows." });
    expect(localizedToolText({ display_name: "Client query", description: "Original user text" })).toEqual({ name: "Client query", description: "Original user text" });
  });
});
