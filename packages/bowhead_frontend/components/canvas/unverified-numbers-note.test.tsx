/**
 * Parity between counting and rendering.
 *
 * computeReportTrust counts unverified_numbers from narrative **and** insight_card
 * in the strip's "N 个数字待核对", while the button navigates using
 * scrollToFirstUnverified() to find .canvas-report-article .v2-narrative-unverified.
 * These once used different sources: insight_card was counted but never rendered that block;
 * observed on 116 v1.1.5 cv_51ac5cfb5a5a: both 2/2 belonged to insight_card, so no body warning, and clicking
 * left scrollTop always at 0.
 *
 * Run each kind here: **anything counted must render a navigation target**.
 */

import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";
import { computeReportTrust, scrollToFirstUnverified } from "@/lib/report-trust";
import { NarrativeSection } from "./sections/narrative";
import { InsightCardSection } from "./sections/insight-card";

const REVIEW = ["16,534,928,991", "16,532,251,346"];

/** Wrap in .canvas-report-article; scrollToFirstUnverified's selector requires this ancestor. */
function renderInArticle(ui: ReactElement) {
  const { container } = renderIntl(<div className="canvas-report-article">{ui}</div>);
  return container;
}

type Case = {
  name: string;
  section: ReportSection;
  render: () => ReactElement;
};

const CASES: Case[] = [
  {
    name: "narrative",
    section: { id: "n1", kind: "narrative", markdown: "全年合计 16,534,928,991 元", unverified_numbers: REVIEW } as ReportSection,
    render: () => (
      <NarrativeSection
        data={{ id: "n1", kind: "narrative", markdown: "全年合计 16,534,928,991 元", unverified_numbers: REVIEW }}
      />
    ),
  },
  {
    name: "insight_card (带 narrative)",
    section: { id: "i1", kind: "insight_card", title: "全年规模稳定", narrative: "全年合计 16,534,928,991 元", unverified_numbers: REVIEW } as ReportSection,
    render: () => (
      <InsightCardSection
        data={{ id: "i1", kind: "insight_card", title: "全年规模稳定", narrative: "全年合计 16,534,928,991 元", unverified_numbers: REVIEW }}
      />
    ),
  },
  {
    // Composer scans thesis + narrative + evidence[].callout_text:
    // With numbers only in thesis, the card has no narrative block; attach the warning at card level to render it.
    name: "insight_card (thesis-only, 无 narrative)",
    section: { id: "i2", kind: "insight_card", title: "全年规模稳定", thesis: "全年 16,534,928,991 元", unverified_numbers: REVIEW } as ReportSection,
    render: () => (
      <InsightCardSection
        data={{ id: "i2", kind: "insight_card", title: "全年规模稳定", thesis: "全年 16,534,928,991 元", unverified_numbers: REVIEW }}
      />
    ),
  },
];

afterEach(() => cleanup());

describe("被计数的 section 必须渲得出待核对数字", () => {
  for (const c of CASES) {
    it(`${c.name}: 计进 strip 且跳转目标存在`, () => {
      expect(computeReportTrust([c.section]).unverified).toBe(REVIEW.length);

      const container = renderInArticle(c.render());
      const note = container.querySelector(".v2-narrative-unverified");
      expect(note, `${c.name} 被计数却没渲出 .v2-narrative-unverified`).not.toBeNull();
      for (const n of REVIEW) expect(note!.textContent).toContain(n);

      // The navigation button can actually find its target (the strip calls this exact function)
      (note as HTMLElement).scrollIntoView = vi.fn();
      expect(scrollToFirstUnverified(container as HTMLElement)).toBe(true);
      expect((note as HTMLElement).scrollIntoView).toHaveBeenCalledTimes(1);
    });
  }

  it("没有未核验数字时不渲块，也没有跳转目标（negative control）", () => {
    const clean: ReportSection = { id: "n2", kind: "narrative", markdown: "纯文字" } as ReportSection;
    expect(computeReportTrust([clean]).unverified).toBe(0);
    const container = renderInArticle(<NarrativeSection data={{ id: "n2", kind: "narrative", markdown: "纯文字" }} />);
    expect(container.querySelector(".v2-narrative-unverified")).toBeNull();
    expect(scrollToFirstUnverified(container as HTMLElement)).toBe(false);
  });
});
