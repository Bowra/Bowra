/**
 * report-streaming D3/D2 — top status bar must describe what is actually happening now.
 *
 * Background (116 production measurements, docs/sprints/report-streaming/discovery.md §2.5/§2.6):
 * each report turn has p50 62s while AI tools run without a single word of artifact output, yet the old banner
 * says "正在更新报告 — 第 N / M 节" throughout the turn, with the denominator counting
 * every canvas section (including prior turns).
 *
 * After D2, responsibilities narrow: **status bar exclusively owns what is happening now**, while canvas band
 * (wait-band.tsx) exclusively owns output counts — no repeated sentence on the same screen.
 */

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { onSectionAdded, resetCanvas } from "@/lib/canvas-store";
import { applyStepDone, applyStepStarted, resetTurn } from "@/lib/turn-store";
import type { ReportSection } from "@/lib/types";

import { StreamingBanner } from "./streaming-banner";

const TURN = "t_d3";
const OTHER_TURN = "t_prev";

function section(
  id: string,
  kind: string,
  turnId: string,
  extra: Record<string, unknown> = {},
): ReportSection {
  return { id, kind, turn_id: turnId, ...extra } as unknown as ReportSection;
}

function chart(id: string, turnId = TURN) {
  return section(id, "chart", turnId, { spec: { title: id } });
}

/** Select only copy area (role=status also contains turn pill / stop button, contaminating whole-sentence assertions). */
function copyText(): string {
  return (
    screen.getByRole("status").querySelector(".rc-stream-copy")?.textContent ?? ""
  ).trim();
}

function startStep(headerZh: string, label?: string, stepId = "s1") {
  applyStepStarted({
    turnId: TURN,
    stepId,
    stepType: "tool_run",
    headerZh,
    label: label ?? null,
  });
  return stepId;
}

afterEach(() => {
  cleanup();
  resetCanvas();
  resetTurn(TURN);
  resetTurn(OTHER_TURN);
});

describe("StreamingBanner — 工作阶段（compose 未起）", () => {
  it("显示当前在跑的动作，而不是「正在更新报告」", () => {
    onSectionAdded({ section: chart("c1") });
    startStep("查询数据", "sales.xlsx");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("进行中");
    expect(copy).toContain("查询数据 · sales.xlsx");
    // Exact old behavior — artifacts do not exist yet, so this claim is invalid.
    expect(copy).not.toContain("正在更新报告");
    expect(copy).not.toContain("个部分");
  });

  it("★ 空档时状态条什么都不说——产出计数归画布带独占，不同屏重复", () => {
    onSectionAdded({ section: chart("c1") });
    onSectionAdded({ section: chart("c2") });
    const id = startStep("查询数据");
    applyStepDone({ turnId: TURN, stepId: id, state: "done" });

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("进行中");
    // Counts belong to WaitBand (see wait-band.test.tsx); status bar must not repeat them.
    expect(copy).not.toContain("张图");
    expect(copy).not.toContain("个部分");
    // Nor fall back to rotating fictional copy.
    expect(copy).not.toContain("正在创建报告结构");
    // ★ P1-2 (caught on real device in 1.0.74): asserting forbidden words alone is insufficient — separator renders unconditionally;
    // production showed "进行中 —" with nothing after the dash. Assert the **whole sentence**.
    expect(copyText()).toBe("进行中");
  });

  it("★ P1-2：detail 为空时分隔符也不能渲染（悬空破折号）", () => {
    // No canvas artifacts or active actions — none of the four detail candidates apply.
    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    expect(copyText()).toBe("进行中");
    expect(copyText()).not.toMatch(/—/);
  });

  it("detail 有内容时分隔符照常在", () => {
    onSectionAdded({ section: chart("c1") });
    startStep("查询数据");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    expect(screen.getByRole("status").textContent).toContain("进行中 — 查询数据");
  });

  it("裸英文工具名不上横幅", () => {
    // 116 production: about 8% of steps have bare tool names in headerZh (add_image 146 times,
    // web_search 79, skill_list 55…). Report panel serves business users; internal identifiers must not appear.
    onSectionAdded({ section: chart("c1") });
    startStep("add_image");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).not.toContain("add_image");
    expect(copy).toContain("进行中");
  });

  it("带括号英文的中文名仍然显示（判据是有没有中文，不是黑名单）", () => {
    onSectionAdded({ section: chart("c1") });
    startStep("查询数据 (bi_query)");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    expect(screen.getByRole("status").textContent).toContain("查询数据 (bi_query)");
  });

  it("上一轮的报告头还在 store 里时，本轮仍判为工作阶段（不被追问轮误判）", () => {
    // Follow-up turn: prior h1 is not marked superseded until this turn's compose lands.
    onSectionAdded({ section: section("h_old", "h1", OTHER_TURN, { text: "上一份报告" }) });
    onSectionAdded({ section: chart("c1") });
    startStep("查询数据");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("进行中");
    expect(copy).not.toContain("正在撰写报告");
  });
});

describe("StreamingBanner — 撰写阶段（compose 已起）", () => {
  it("本轮报告头落地后切到「正在撰写报告」", () => {
    onSectionAdded({ section: chart("c1") });
    onSectionAdded({ section: section("h1", "h1", TURN, { text: "销售分析" }) });

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("正在更新报告");
    expect(copy).toContain("正在撰写报告");
  });

  it("单调闩：报告头落地后，即使又冒出 running step 也不退回工作阶段", () => {
    onSectionAdded({ section: section("kpi", "kpi_row", TURN, { cells: [] }) });
    startStep("整理引用");

    renderIntl(<StreamingBanner turnId={TURN} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("正在撰写报告");
    expect(copy).not.toContain("进行中 —");
  });
});

describe("StreamingBanner — turnId 缺失", () => {
  it("没有 turnId 时不崩", () => {
    onSectionAdded({ section: chart("c1") });

    renderIntl(<StreamingBanner turnId={null} />, { locale: "zh-CN" });

    const copy = screen.getByRole("status").textContent ?? "";
    expect(copy).toContain("进行中");
    expect(copy).not.toContain("张图");
  });
});
