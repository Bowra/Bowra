import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";
import { SourceLinkageStrip } from "./trust-strip";

const narrative = (markdown: string, review: string[] = []): ReportSection => ({
  id: "n1",
  kind: "narrative",
  markdown,
  unverified_numbers: review,
}) as ReportSection;

/** Real shape from 116 cv_51ac5cfb5a5a: every unverified number is on insight_card; narrative has none. */
const insightCard = (review: string[]): ReportSection => ({
  id: "ic1",
  kind: "insight_card",
  title: "全年规模稳定",
  narrative: "全年合计 16,534,928,991 元",
  unverified_numbers: review,
}) as ReportSection;

const sources = (count: number): ReportSection => ({
  id: "src",
  kind: "sources",
  entries: Array.from({ length: count }, (_, index) => ({ label: `Source ${index + 1}` })),
}) as unknown as ReportSection;

afterEach(() => cleanup());

describe("SourceLinkageStrip", () => {
  it("renders three independent facts without percentage or verified language", () => {
    const { container } = renderIntl(
      <SourceLinkageStrip sections={[narrative("A [1], B [2]", ["19"]), sources(2)]} />,
      { locale: "zh-CN" },
    );

    expect(container.textContent).toContain("2 处来源引用");
    expect(container.textContent).toContain("1 个数字待核对");
    expect(container.textContent).toContain("2 条来源");
    expect(container.textContent).not.toMatch(/%|已验证|已核验/);
    expect(container.querySelector(".verified")).toBeNull();
  });

  it("counts insight_card review numbers too (116 cv_51ac5cfb5a5a shape)", () => {
    const { container } = renderIntl(
      <SourceLinkageStrip sections={[insightCard(["16,534,928,991", "16,532,251,346"]), sources(9)]} />,
      { locale: "zh-CN" },
    );
    expect(container.textContent).toContain("2 个数字待核对");
  });

  it("keeps check-only visible and jumps to the first number to check", () => {
    const target = document.createElement("div");
    target.className = "v2-narrative-unverified";
    target.scrollIntoView = vi.fn();
    const article = document.createElement("div");
    article.className = "canvas-report-article";
    article.append(target);
    document.body.append(article);

    renderIntl(<SourceLinkageStrip sections={[narrative("No citations", ["19"])]} />);
    fireEvent.click(screen.getByRole("button", { name: "定位到第一个待核对数字" }));
    expect(target.scrollIntoView).toHaveBeenCalledTimes(1);
    article.remove();
  });

  it("hides an empty terminal strip", () => {
    const { container } = renderIntl(<SourceLinkageStrip sections={[narrative("Plain text")]} />);
    expect(container.firstChild).toBeNull();
  });

  it("uses safe streaming copy and omits source totals", () => {
    const { container } = renderIntl(
      <SourceLinkageStrip
        sections={[narrative("A [1]", ["19"]), sources(2)]}
        isStreaming
      />,
      { locale: "en-US" },
    );
    expect(container.textContent).toContain("Collecting source citations…");
    expect(container.textContent).toContain("1 source citation");
    // en once translated “number” as "figure", which in a chart-filled report reads as “1 chart to review”,
    // leading me personally to mistake it for duplicate counting. Both en and zh refer to numbers.
    expect(container.textContent).toContain("1 number to check");
    expect(container.textContent).not.toMatch(/figure/i);
    expect(container.textContent).not.toContain("2 sources");
    expect(container.textContent).not.toMatch(/%|verified/i);
  });

  it("supports hover, keyboard pinning, Escape and explicit accessible relationships", () => {
    renderIntl(<SourceLinkageStrip sections={[narrative("A [1]"), sources(1)]} />);
    const trigger = screen.getByRole("button", {
      name: "来源引用不代表数字已经过数值核验。",
    });

    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    fireEvent.pointerEnter(trigger.parentElement!);
    const note = screen.getByRole("note", {
      name: "来源引用不代表数字已经过数值核验。",
    });
    expect(note.textContent).toContain("待核对计数只包含当前检测到的项目");
    expect(trigger.getAttribute("aria-controls")).toBe(note.id);

    fireEvent.pointerLeave(trigger.parentElement!);
    expect(screen.queryByText(/待核对计数只包含/)).toBeNull();
    fireEvent.focus(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(document.activeElement).toBe(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });

  it("toggles on tap/click and closes an outside pointer interaction", () => {
    renderIntl(<SourceLinkageStrip sections={[narrative("A [1]"), sources(1)]} />);
    const trigger = screen.getByRole("button", {
      name: "来源引用不代表数字已经过数值核验。",
    });
    fireEvent.pointerDown(trigger, { pointerType: "touch" });
    fireEvent.pointerUp(trigger, { pointerType: "touch" });
    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");

    fireEvent.pointerDown(document.body);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
  });
});
