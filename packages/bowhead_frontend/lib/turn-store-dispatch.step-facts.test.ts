/**
 * Turn visibility FR4: step events with facts are stored with words in the current language; events
 * without facts (earlier Turns, older servers) keep their stored text. Live SSE and replay share this path.
 */
import { afterEach, describe, expect, it } from "vitest";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "./turn-store-dispatch";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "./turn-store";

const T = "turn-facts";

afterEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  document.documentElement.lang = "en-US";
});

function step(id: string) {
  return getTurnSnapshot(T)?.steps.find((s) => s.stepId === id);
}

describe("turn-store-dispatch — step facts", () => {
  it("words an attachment row from its facts", () => {
    document.documentElement.lang = "zh-CN";
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "parse_a",
      step_type: "parse",
      header_zh: "解析 store_sales_2026H1.csv",
      input_preview: "store_sales_2026H1.csv · text/csv, 14827 bytes",
      facts: { v: 1, kind: "attachment_read", filename: "store_sales_2026H1.csv", size_bytes: 14827 },
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "parse_a",
      step_type: "parse",
      state: "success",
      output_preview: "已解析 (14827 bytes · 5017 chars) · 截断",
      facts: { v: 1, kind: "attachment_read", chars_read: 5017, truncated: true },
    });
    expect(step("parse_a")).toMatchObject({ headerZh: "读取附件", label: "store_sales_2026H1.csv", summary: "已读入前 5,017 字" });
  });

  it("words a code run's outputs and a chart write in English", () => {
    document.documentElement.lang = "en-US";
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "step_code",
      step_type: "tool_run",
      tool_name: "python_sandbox",
      header_zh: "运行代码",
      input_preview: "生成单文件离线交互式 HTML 看板",
    });
    dispatchToTurnStore("step_done", {
      card_id: T,
      step_id: "step_code",
      step_type: "tool_run",
      state: "success",
      output_preview: "运行成功 · 3 输出",
      facts: { v: 1, kind: "code_outputs", count: 2, types: ["html", "json"] },
    });
    expect(step("step_code")).toMatchObject({ label: "生成单文件离线交互式 HTML 看板", summary: "Ran OK · 2 outputs (HTML, JSON)" });
  });

  it("keeps the stored text of events without facts", () => {
    document.documentElement.lang = "en-US";
    dispatchToTurnStore("step_started", {
      card_id: T,
      step_id: "step_old",
      step_type: "tool_run",
      tool_name: "add_chart",
      header_zh: "添加图表",
      input_preview: "各城市销售额排名",
    });
    dispatchToTurnStore("step_done", { card_id: T, step_id: "step_old", step_type: "tool_run", state: "success", output_preview: "已添加 · hbar" });
    expect(step("step_old")).toMatchObject({ headerZh: "添加图表", label: "各城市销售额排名", summary: "已添加 · hbar" });
  });
});
