/**
 * The running band must not render Chinese in the English UI (2026-08-17, user caught this in a 116 en-US screenshot).
 *
 * ★ Two Chinese leaks, **both from existing frontend English strings that nobody read**:
 *   ① Step heading: `header_zh` (Chinese by definition) unconditionally overrode the tool catalog,
 *      rendering “加图表” and “跑代码”, although the catalog's `Add chart` / `Run code` had always existed;
 *   ② Agent name: `AGENT_META.compute.en = "Data Analyst"` existed, but
 *      `agentDisplayName` always returned `.zh` ⇒ rendered “主分析”.
 * User decision: **band text follows the UI language**.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { agentDisplayName } from "./agent-meta";
import { humanLabel } from "./running-band-model";
import type { StepView } from "./types";

const origDoc = globalThis.document;
const origNav = globalThis.navigator;
const origWin = globalThis.window;

const setLocale = (loc: "zh-CN" | "en-US") => {
  Object.defineProperty(globalThis, "document", {
    value: { cookie: `NEXT_LOCALE=${loc}`, documentElement: { lang: loc } },
    writable: true, configurable: true,
  });
  Object.defineProperty(globalThis, "navigator", {
    value: { language: loc }, writable: true, configurable: true,
  });
  Object.defineProperty(globalThis, "window", {
    value: { localStorage: { getItem: () => null } }, writable: true, configurable: true,
  });
};

afterEach(() => {
  Object.defineProperty(globalThis, "document", { value: origDoc, writable: true, configurable: true });
  Object.defineProperty(globalThis, "navigator", { value: origNav, writable: true, configurable: true });
  Object.defineProperty(globalThis, "window", { value: origWin, writable: true, configurable: true });
});

const CJK = /[一-鿿]/;
const step = (over: Record<string, unknown>): StepView => ({
  stepId: "s", parentStepId: null, concurrencyGroupId: null,
  stepType: "tool_run", state: "done", ...over,
} as unknown as StepView);

describe("① 步骤抬头跟界面语言走", () => {
  // Mutation: remove `humanLabel`'s `!getCurrentLocale().startsWith("zh")` branch → both English cases fail.
  it("en-US：静态工具用目录英文名，**不许**出现 CJK", () => {
    setLocale("en-US");
    const chart = humanLabel(step({ toolName: "add_chart", headerZh: "加图表" }), false);
    const code = humanLabel(step({ toolName: "python_sandbox", headerZh: "跑代码" }), true);
    expect(CJK.test(chart)).toBe(false);
    expect(CJK.test(code)).toBe(false);
    expect(chart).toBeTruthy();
    expect(code).toBeTruthy();
  });

  it("★ 正对照：zh-CN 仍用后端抬头（它更具体，别把中文侧改坏）", () => {
    setLocale("zh-CN");
    expect(humanLabel(step({ toolName: "bi_schema", headerZh: "查 schema · 27 测度 + 8 维度" }), false))
      .toBe("查 schema · 27 测度 + 8 维度");
  });

  it("★ 正对照：团队 MCP 工具**仍**用后端抬头（目录只会合成裸 slug，那才是黑话）", () => {
    setLocale("en-US");
    expect(humanLabel(step({ toolName: "teammcp.github.list_issues", headerZh: "列 issue" }), false))
      .toBe("列 issue");
  });
});

describe("② 内置 agent 名跟界面语言走", () => {
  // Built-in names come from the Marketplace display names (one source for marketplace, settings and trace).
  it("en-US: compute → the Marketplace name, no CJK", () => {
    setLocale("en-US");
    const n = agentDisplayName("compute");
    expect(n).toBe("Data & Compute Analyst");
    expect(CJK.test(n)).toBe(false);
  });

  it("★ 正对照：zh-CN 是市场里的「数据与计算分析师」", () => {
    setLocale("zh-CN");
    expect(agentDisplayName("compute")).toBe("数据与计算分析师");
  });

  it("a built-in snapshot that is only its identifier is not a chosen name (116: trace showed \"compute\")", () => {
    setLocale("en-US");
    expect(agentDisplayName("compute", "compute")).toBe("Data & Compute Analyst");
    expect(agentDisplayName("kb-analyst", "kb")).toBe("Knowledge Researcher");
    expect(agentDisplayName("business-analyst", "business-analyst")).toBe("Business Growth Analyst");
  });

  it("Lead is the product name in every language, never \"Lead\"", () => {
    setLocale("en-US");
    expect(agentDisplayName("lead")).toBe("Bowhead");
    setLocale("zh-CN");
    expect(agentDisplayName("lead")).toBe("Bowhead");
  });

  it("★ 正对照：团队自配的名字**原样透出**，不许翻译（那是用户自己起的名）", () => {
    setLocale("en-US");
    expect(agentDisplayName("compute", "财务小组")).toBe("财务小组");
  });
});
