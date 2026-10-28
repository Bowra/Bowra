/**
 * report-streaming D2 — canvas in-progress band.
 *
 * Addresses the largest production issue: 77% of report turns have longest idle periods before artifacts begin
 * drafting (p50 62s / p90 173s), with only a few completed charts and nothing new on the canvas.
 *
 * Five load-bearing assertions (each corresponds to real regressions; removing the fix must fail):
 *  1. **Appearance**: **do not appear** before any artifact in this turn — earlier display = opening canvas early
 *     = a variant of implying an artifact will certainly arrive (design r1 review hard conflict 2).
 *  2. **Exit**: leave immediately when compose starts (report header/KPI lands), yielding space to report header.
 *  3. **No gap narration**: without an active action, keep every word unchanged (change marker shape only).
 *  4. **Count only this turn**: exclude previous-turn charts.
 *  5. **Timer baseline in store**: unknown start is hidden rather than showing an incorrect 0.
 */

import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { onSectionAdded, resetCanvas } from "@/lib/canvas-store";
import {
  applyCardFinalized,
  applyStepDone,
  applyStepStarted,
  applyTurnStarted,
  resetTurn,
} from "@/lib/turn-store";
import type { ReportSection } from "@/lib/types";

import { WaitBand } from "./wait-band";

const TURN = "t_d2";
const OTHER_TURN = "t_prev";

function section(
  id: string,
  kind: string,
  turnId: string,
  extra: Record<string, unknown> = {},
): ReportSection {
  return { id, kind, turn_id: turnId, ...extra } as unknown as ReportSection;
}

function chart(id: string, title: string, turnId = TURN) {
  return section(id, "chart", turnId, { spec: { title } });
}

function startStep(headerZh: string, stepId = "s1") {
  applyStepStarted({ turnId: TURN, stepId, stepType: "tool_run", headerZh });
  return stepId;
}

function band(): HTMLElement | null {
  return screen.queryByRole("status");
}

afterEach(() => {
  cleanup();
  resetCanvas();
  resetTurn(TURN);
  resetTurn(OTHER_TURN);
});

// ── 1. Appearance ─────────────────────────────────────────────────

describe("WaitBand — 出现时机", () => {
  it("★ 本轮还没有任何画布产物时不出现（不提前打开画布）", () => {
    startStep("查询数据");

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    expect(band()).toBeNull();
  });

  it("第一个产物落地后才出现", () => {
    startStep("查询数据");
    onSectionAdded({ section: chart("c1", "华东区各省 GMV 环比变化") });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    const copy = band()?.textContent ?? "";
    expect(copy).toContain("进行中");
    expect(copy).toContain("已产出 1 张图表");
    expect(copy).toContain("华东区各省 GMV 环比变化");
  });

  it("没有 turnId 时不出现", () => {
    onSectionAdded({ section: chart("c1", "图") });

    renderIntl(<WaitBand turnId={null} />, { locale: "zh-CN" });

    expect(band()).toBeNull();
  });
});

// ── 2. Exit ─────────────────────────────────────────────────

describe("WaitBand — 让位", () => {
  it("★ 报告头落地即退场（位置交给报告头）", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    onSectionAdded({ section: section("h1", "h1", TURN, { text: "销售分析" }) });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    expect(band()).toBeNull();
  });

  it("KPI 带落地同样算 compose 已起", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    onSectionAdded({ section: section("kpi", "kpi_row", TURN, { cells: [] }) });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    expect(band()).toBeNull();
  });

  it("上一轮的报告头不算——追问轮里带照常出现", () => {
    onSectionAdded({ section: section("h_old", "h1", OTHER_TURN, { text: "上一份报告" }) });
    onSectionAdded({ section: chart("c1", "图一") });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    expect(band()?.textContent ?? "").toContain("已产出 1 张图表");
  });
});

// ── 3. No gap narration ───────────────────────────────────────────────

describe("WaitBand — 空档", () => {
  it("★ 没有正在跑的动作时，文本与有动作时一字不差", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    onSectionAdded({ section: chart("c2", "图二") });

    // Active action
    const id = startStep("查询数据");
    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });
    const running = band()?.textContent ?? "";
    cleanup();

    // Gap
    applyStepDone({ turnId: TURN, stepId: id, state: "done" });
    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });
    const idle = band()?.textContent ?? "";

    expect(idle).toBe(running);
    // In particular, avoid narration asserting unknown states such as "上一步已完成 / 正在决定下一步"
    expect(idle).not.toContain("上一步");
    expect(idle).not.toContain("下一步");
  });
});

// ── 4. Count only this turn ─────────────────────────────────────────────

describe("WaitBand — 计数", () => {
  it("上一轮的图不算进来", () => {
    onSectionAdded({ section: chart("old1", "旧图一", OTHER_TURN) });
    onSectionAdded({ section: chart("old2", "旧图二", OTHER_TURN) });
    onSectionAdded({ section: chart("c1", "本轮图") });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    const copy = band()?.textContent ?? "";
    expect(copy).toContain("已产出 1 张图表");
    expect(copy).not.toContain("旧图一");
  });
});

// ── 5. Timing ─────────────────────────────────────────────────────

describe("WaitBand — 计时", () => {
  it("★ 起点未知时不显示时长（不显示一个错的 0）", () => {
    onSectionAdded({ section: chart("c1", "图一") });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    const copy = band()?.textContent ?? "";
    expect(copy).not.toContain("已进行");
    expect(copy).not.toContain("0 秒");
  });

  it("有起点时显示已进行时长", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    applyTurnStarted({ turnId: TURN, startedAt: new Date(Date.now() - 47_000).toISOString() });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    expect(band()?.textContent ?? "").toContain("已进行");
  });

  it("重连重放不把计时刷回 0（applyTurnStarted 只认第一次）", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    const first = new Date(Date.now() - 180_000).toISOString();
    applyTurnStarted({ turnId: TURN, startedAt: first });
    // Reconnect → full turn_started replay carrying the current time
    applyTurnStarted({ turnId: TURN, startedAt: new Date().toISOString() });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    const copy = band()?.textContent ?? "";
    expect(copy).toContain("3 分"); // Still calculate from the earliest start
  });
});

// ── 6. Ended without canvas artifacts (S6, option 1: backend contract unchanged) ──────────────

describe("WaitBand — 结束态", () => {
  it("turn 收尾且未出报告时，说结论在对话里，且不再列 chip", () => {
    onSectionAdded({ section: chart("c1", "图一") });
    applyCardFinalized({ turnId: TURN, finalState: "completed" });

    renderIntl(<WaitBand turnId={TURN} />, { locale: "zh-CN" });

    const copy = band()?.textContent ?? "";
    expect(copy).toContain("已完成");
    expect(copy).toContain("这次的结论在左侧对话中");
    expect(copy).not.toContain("图一");
  });
});
