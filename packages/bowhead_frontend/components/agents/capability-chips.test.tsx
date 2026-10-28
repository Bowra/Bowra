import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { CapabilityChips, visibleChipCount } from "./capability-chips";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("visibleChipCount", () => {
  const more = () => 30;

  it("shows every chip when the whole row fits, with no +N", () => {
    expect(visibleChipCount([60, 60, 60], 196, 6, more)).toBe(3);
  });

  it("counts from measured widths and reserves room for the +N chip", () => {
    // 60 + 6 + 60 = 126; adding +N (6 + 30) = 162 fits in 170, the third chip (126 + 66 = 192) does not.
    expect(visibleChipCount([60, 60, 60, 60], 170, 6, more)).toBe(2);
    // Wider chips leave room for fewer; N is not a fixed cut-off at three.
    expect(visibleChipCount([132, 132, 40], 200, 6, more)).toBe(1);
    expect(visibleChipCount([40, 40, 40, 40, 40, 40], 260, 6, more)).toBe(5);
  });

  it("keeps the first chip even when it cannot fit beside +N", () => {
    expect(visibleChipCount([132, 132], 120, 6, more)).toBe(1);
  });

  it("wraps full-width chips into a second row before collapsing into +N", () => {
    // Row 1: 200; row 2: 150 + 6 + 150 = 306 fits in 320. All three fit in two rows.
    expect(visibleChipCount([200, 150, 150], 320, 6, more, 2)).toBe(3);
    // A fourth chip needs a third row, and +N does not fit after two second-row chips (306 + 6 + 30 > 320), so show two.
    expect(visibleChipCount([200, 150, 150, 150], 320, 6, more, 2)).toBe(2);
    // One row only: the same widths collapse to the first chip and +N.
    expect(visibleChipCount([200, 150, 150, 150], 320, 6, more)).toBe(1);
  });
});

describe("CapabilityChips", () => {
  const chips = ["同比拆解", "渠道归因", "异常定位", "口径核对", "指标定义"].map((label, index) => ({ id: `cap_${index}`, label }));

  function stubLayout(rowWidth: number, chipWidth: number) {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const width = this.classList.contains("am-capability-row")
        ? rowWidth
        : this.hasAttribute("data-measure-more")
          ? 28
          : this.hasAttribute("data-measure-chip")
            ? chipWidth
            : 0;
      return { width, height: 24, top: 0, left: 0, right: width, bottom: 24, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
    });
  }

  it("wraps into two rows and merges the chips that do not fit into an outlined +N with the full list", () => {
    // Two 70px chips per 200px row; the fifth chip would need a third row, so it becomes +1 after the fourth.
    stubLayout(200, 70);
    renderIntl(<CapabilityChips label="能力" chips={chips} />);

    const row = screen.getByRole("group", { name: "能力" });
    expect(Array.from(row.querySelectorAll(":scope > .am-capability-chip")).map((chip) => chip.textContent)).toEqual(["同比拆解", "渠道归因", "异常定位", "口径核对"]);
    const more = screen.getByRole("button", { name: "另有 1 项能力" });
    expect(more.textContent).toContain("+1");
    const tip = screen.getByRole("tooltip");
    expect(more.getAttribute("aria-describedby")).toBe(tip.id);
    expect(tip.textContent).toBe("同比拆解 · 渠道归因 · 异常定位 · 口径核对 · 指标定义");
  });

  it("shows every chip and no +N when there is room", () => {
    stubLayout(800, 70);
    renderIntl(<CapabilityChips label="能力" chips={chips} />);

    expect(screen.getByRole("group", { name: "能力" }).querySelectorAll(":scope > .am-capability-chip")).toHaveLength(5);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
