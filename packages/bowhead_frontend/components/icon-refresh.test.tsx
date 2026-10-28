import { cleanup, render } from "@testing-library/react";
import { Check as LucideCheck, X as LucideX } from "lucide-react";
import { afterEach, describe, expect, it } from "vitest";

import { LucideGlyph, lucideIcon, refreshStrokeWidth, snapIconSize } from "./icon-refresh";

afterEach(cleanup);

const Sized = lucideIcon(LucideX, 14);
const Fixed = lucideIcon(LucideX, 12, { sized: false });
const WithDefaults = lucideIcon(LucideX, 14, { defaults: { "aria-hidden": true } });

function svg(container: HTMLElement): SVGSVGElement {
  return container.querySelector("svg") as SVGSVGElement;
}

describe("refreshStrokeWidth", () => {
  it("is 1.5 for the 14 / 16 / 20 tiers and scales inline markers by the 14 tier ratio", () => {
    expect(refreshStrokeWidth(20)).toBe(1.5);
    expect(refreshStrokeWidth(16)).toBe(1.5);
    expect(refreshStrokeWidth(14)).toBe(1.5);
    expect(refreshStrokeWidth(13)).toBe(1.5);
    expect(refreshStrokeWidth(12)).toBe(1.29);
    expect(refreshStrokeWidth(10)).toBe(1.07);
    expect(refreshStrokeWidth(Number.NaN)).toBe(1.5);
  });
});

describe("snapIconSize", () => {
  it("maps legacy sizes to the nearest 14 / 16 / 20 tier, ties to the default 16", () => {
    const table = [8, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 28, 42, 48].map((size) => [size, snapIconSize(size)]);
    expect(table).toEqual([
      [8, 14], [10, 14], [11, 14], [12, 14], [13, 14], [14, 14], [15, 16], [16, 16], [17, 16], [18, 16],
      [19, 20], [20, 20], [21, 20], [22, 20], [28, 20], [42, 20], [48, 20],
    ]);
  });

  it("leaves non-positive and non-finite values alone", () => {
    expect(snapIconSize(0)).toBe(0);
    expect(Number.isNaN(snapIconSize(Number.NaN))).toBe(true);
  });
});

describe("lucideIcon", () => {
  it("renders Lucide: snapped size, non-scaling 1.5 stroke, no automatic aria-hidden", () => {
    const { container } = render(<Sized size={18} className="a" />);
    const el = svg(container);
    expect(el.getAttribute("class")).toBe("lucide lucide-x bh-icon a");
    expect(el.getAttribute("width")).toBe("16");
    expect(el.getAttribute("height")).toBe("16");
    expect(el.getAttribute("stroke-width")).toBe("1.5");
    expect(el.style.strokeWidth).toBe("1.5");
    expect(el.hasAttribute("aria-hidden")).toBe(false);
    for (const child of Array.from(el.children)) expect(child.getAttribute("vector-effect")).toBe("non-scaling-stroke");
  });

  it("passes through caller aria and focusable; width/height win over size", () => {
    const el = svg(render(<Sized width={11} height={11} aria-hidden="true" focusable="false" />).container);
    // 11 snaps to the 14 tier, so the stroke is the full 1.5 (no thinned inline-marker stroke any more)
    expect(el.getAttribute("width")).toBe("14");
    expect(el.getAttribute("aria-hidden")).toBe("true");
    expect(el.getAttribute("focusable")).toBe("false");
    expect(el.getAttribute("stroke-width")).toBe("1.5");
    expect(el.style.strokeWidth).toBe("1.5");
  });

  it("ignores size for a fixed-box icon (default box, snapped)", () => {
    const el = svg(render(<Fixed size={18} />).container);
    expect(el.getAttribute("width")).toBe("14");
    expect(el.hasAttribute("size")).toBe(false);
  });

  it("keeps the default aria-hidden, overridable by the caller", () => {
    expect(svg(render(<WithDefaults />).container).getAttribute("aria-hidden")).toBe("true");
    cleanup();
    expect(svg(render(<WithDefaults aria-hidden={false} />).container).getAttribute("aria-hidden")).toBe("false");
  });
});

describe("LucideGlyph", () => {
  it("draws Lucide, keeping a non-square box and copying non-drawing attributes", () => {
    const el = svg(render(<LucideGlyph icon={LucideCheck} size={10} height={14} className="k" aria-hidden />).container);
    expect(el.getAttribute("class")).toBe("lucide lucide-check bh-icon k");
    expect(el.getAttribute("width")).toBe("10");
    expect(el.getAttribute("height")).toBe("14");
    expect(el.getAttribute("aria-hidden")).toBe("true");
  });

  it("snaps a square box and keeps non-pixel boxes", () => {
    const snapped = svg(render(<LucideGlyph icon={LucideCheck} size={13} />).container);
    expect([snapped.getAttribute("width"), snapped.getAttribute("height")]).toEqual(["14", "14"]);
    cleanup();
    const em = svg(render(<Sized width="1em" height="1em" />).container);
    expect([em.getAttribute("width"), em.getAttribute("height")]).toEqual(["1em", "1em"]);
  });
});
