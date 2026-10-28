/**
 * Discoverability of insight-card confidence badges (feedback-triage-2026-08-24).
 *
 * Customer's exact feedback: “报告的图中位置鼠标 hover 上去后会成为一个『?』，但无法点击。
 * 必须要停留一段时间后，才会延迟出现解释。我第一次 hover 上去的时候，没有停留，
 * 导致我以为不能点击是一个 bug。”
 *
 * Root cause: **the cursor promised what the implementation did not deliver**. `cursor: help` promises an explanation,
 * but it was on the native `title`, requiring about 1 second of hover. Use an instant tooltip instead,
 * also making the same explanation available to keyboard and screen-reader users.
 */
import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import {
  CANVAS_FOCUS_DATA_EVENT,
  type CanvasFocusDataDetail,
} from "@/lib/canvas-workspace-focus";

import { InsightCardSection } from "./insight-card";

function card(confidence: "high" | "med" | "low") {
  return {
    id: "sec_ins",
    kind: "insight_card" as const,
    title: "总体",
    thesis: "结论一句话",
    confidence,
  };
}

describe("ConfidenceBadge", () => {
  it("★ 不再依赖原生 title —— 那正是「要停留一秒」的来源", () => {
    const { container } = renderIntl(<InsightCardSection data={card("high") as never} />);
    const badge = container.querySelector(".bh-tip") as HTMLElement | null;
    expect(badge).toBeTruthy();
    expect(badge!.hasAttribute("title")).toBe(false);
  });

  it("★ 解释走 data-tip（即时小签），且内容是该档位的真解释", () => {
    const { container } = renderIntl(<InsightCardSection data={card("low") as never} />);
    const badge = container.querySelector(".bh-tip") as HTMLElement;
    const tip = badge.getAttribute("data-tip") || "";
    expect(tip.length).toBeGreaterThan(0);
    // Low-confidence copy explains insufficient samples / many assumptions; it must not repeat the badge label
    expect(tip).not.toBe(badge.textContent);
  });

  it("★ 键盘可达 + 读屏能同时拿到档位和解释", () => {
    const { container } = renderIntl(<InsightCardSection data={card("med") as never} />);
    const badge = container.querySelector(".bh-tip") as HTMLElement;
    expect(badge.getAttribute("tabindex")).toBe("0");
    const aria = badge.getAttribute("aria-label") || "";
    expect(aria).toContain(badge.getAttribute("data-tip"));
    expect(aria.length).toBeGreaterThan((badge.getAttribute("data-tip") || "").length);
  });

  it("不给 role=button —— 每档解释是固定文案，没有可展开的逐卡依据，别再假装能点", () => {
    const { container } = renderIntl(<InsightCardSection data={card("high") as never} />);
    const badge = container.querySelector(".bh-tip") as HTMLElement;
    expect(badge.getAttribute("role")).toBeNull();
  });

  it("三档各有各的解释，不能塌成同一句", () => {
    const tips = (["high", "med", "low"] as const).map((level) => {
      const { container } = renderIntl(<InsightCardSection data={card(level) as never} />);
      return container.querySelector(".bh-tip")!.getAttribute("data-tip");
    });
    expect(new Set(tips).size).toBe(3);
  });
});

describe("Insight evidence Results navigation", () => {
  it("opens a detail table through the canonical Report route", () => {
    const listener = vi.fn();
    window.addEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
    const { getByRole } = renderIntl(
      <InsightCardSection
        data={{
          ...card("high"),
          evidence: [{
            kind: "table",
            caption: "Pricing detail",
            table_artifact_id: "art_pricing_detail",
          }],
        } as never}
      />,
    );

    fireEvent.click(getByRole("button", { name: /打开.*表|Open.*table/i }));

    const event = listener.mock.calls[0]?.[0] as CustomEvent<CanvasFocusDataDetail>;
    expect(event.detail).toEqual({
      artifactId: "art_pricing_detail",
      entryPoint: "report",
      reportContext: {
        label: "Pricing detail",
        sectionId: "sec_ins",
      },
    });
    window.removeEventListener(CANVAS_FOCUS_DATA_EVENT, listener);
  });
});
