/**
 * report-streaming — constrain report banner/wait-band labels (caught on dev 1.0.77).
 *
 * Background: D3 moved step `headerZh · label` into a **single-line report-panel status bar**.
 * Backend `_prettify_input_preview` generates `label`: one sentence for most tools, but
 * a `subagent_spawn` label is **a complete task instruction written by the model**, shown verbatim on the device:
 *   `派子 agent · [compute] 用当前工作区（商超零售）的业务数据做一份销售表现分析报告，要求如下： 1. **至少两张图**：`
 *
 * **All** input strings below were **copied from actual dev 1.0.77 observations**, not invented.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  _resetAllTurnsForTesting,
  applyStepStarted,
  getTurnSnapshot,
  selectRunningStepLabel,
} from "./turn-store";

const T = "t_banner";

afterEach(() => {
  _resetAllTurnsForTesting();
});

function running(headerZh: string, label: string | null, stepId = "s1") {
  applyStepStarted({
    turnId: T,
    stepId,
    stepType: "tool_run",
    headerZh,
    label,
  });
  return selectRunningStepLabel(getTurnSnapshot(T));
}

describe("selectRunningStepLabel —— 一行状态条的 label 收敛", () => {
  it("★ 子 agent 的整段任务指令收成一行短句（真机原串）", () => {
    const got = running(
      "派子 agent",
      "用当前工作区（商超零售）的业务数据做一份销售表现分析报告，要求如下： 1. 至少两张图： 2. 需要有结论",
    );
    // End at the semantic boundary (after「要求如下」), rather than cutting off mid-sentence.
    expect(got).toBe("派子 agent · 用当前工作区（商超零售）的业务数据做一份销售表现分析报告，要求如下");
    // Subsequent list items must not enter the status bar.
    expect(got).not.toContain("至少两张图");
    expect(got).not.toContain("2.");
  });

  it("★ 多行指令块只取首行", () => {
    const got = running("派子 agent", "Generate a formal report\n1. two charts\n2. a conclusion");
    expect(got).toBe("派子 agent · Generate a formal report");
    expect(got).not.toContain("\n");
    expect(got).not.toContain("two charts");
  });

  it("★ 短 label 原样透传——`运行: python_sandbox` 不能被冒号切成「运行」", () => {
    // This colon separates the prefix, not clauses; an earlier implementation cut at the wrong position here.
    expect(running("跑代码 (Python)", "运行: python_sandbox")).toBe("跑代码 (Python) · 运行: python_sandbox");
  });

  it("既有的正常 label 一个字都不改（真机原串）", () => {
    expect(running("查询数据 (bi_query)", "2025年 营业额 每月 +1 more", "a")).toBe(
      "查询数据 (bi_query) · 2025年 营业额 每月 +1 more",
    );
    expect(running("子 agent 交付", "2026年1–7月销售表现分析报告", "b")).toBe(
      "子 agent 交付 · 2026年1–7月销售表现分析报告",
    );
    expect(running("解析文件", "sales.xlsx", "c")).toBe("解析文件 · sales.xlsx");
  });

  it("无子句终止符的超长串——硬截断且带省略号", () => {
    const got = running("派子 agent", "あ".repeat(200));
    const label = got.split(" · ")[1];
    expect(label.length).toBeLessThanOrEqual(36);
    expect(label.endsWith("…")).toBe(true);
  });

  it("label 为空时只出 header（不留悬空分隔符）", () => {
    expect(running("查询数据", null)).toBe("查询数据");
    expect(running("查询数据", "   ", "d")).toBe("查询数据");
  });

  it("裸英文工具名仍然不上横幅（D3⑧ 既有闸不受本次改动影响）", () => {
    expect(running("add_image", "whatever")).toBe("");
  });
});
