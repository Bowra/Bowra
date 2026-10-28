import { cleanup, render } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import * as icons from "./icons";

/**
 * Contract of `components/icons.tsx`: every export except the brand mark is a Lucide glyph on the
 * 14 / 16 / 20 tiers with a 1.5 stroke, and caller aria, focusable and className pass through.
 */
const COMBOS: Record<string, Record<string, unknown>> = {
  bare: {},
  sized: { size: 20, "aria-hidden": "true", className: "x" },
  box: { width: 13, height: 13, focusable: "false" },
};

type IconComponent = (props: Record<string, unknown>) => unknown;
const entries = Object.entries(icons as unknown as Record<string, IconComponent>).sort(([a], [b]) => a.localeCompare(b));

function svgOf(C: IconComponent, props: Record<string, unknown>): SVGSVGElement {
  const { container } = render(createElement(C as never, props as never));
  const svg = (container.querySelector("svg") as SVGSVGElement).cloneNode(true) as SVGSVGElement;
  cleanup();
  return svg;
}

describe("icons.tsx", () => {
  it("exports 94 icons", () => {
    expect(entries).toHaveLength(94);
  });

  it.each(entries.filter(([name]) => name !== "Mark"))("%s is Lucide on a tier box with the caller's aria", (_name, C) => {
    for (const [combo, props] of Object.entries(COMBOS)) {
      const svg = svgOf(C, props);
      expect(svg.classList.contains("bh-icon")).toBe(true);
      expect(svg.classList.contains("lucide")).toBe(true);
      expect(["14", "16", "20"]).toContain(svg.getAttribute("width"));
      expect(svg.getAttribute("height")).toBe(svg.getAttribute("width"));
      expect(svg.getAttribute("stroke-width")).toBe("1.5");
      expect(svg.getAttribute("stroke")).toBe("currentColor");
      expect(svg.getAttribute("fill")).toBe("none");
      if (combo === "sized") {
        expect(svg.getAttribute("aria-hidden")).toBe("true");
        expect(svg.classList.contains("x")).toBe(true);
      }
      if (combo === "box") expect(svg.getAttribute("focusable")).toBe("false");
    }
  });

  it("Mark is the hand-drawn brand mark", () => {
    const svg = svgOf(icons.Mark as IconComponent, {});
    expect(svg.classList.contains("lucide")).toBe(false);
    expect(svg.querySelectorAll("path")).toHaveLength(3);
  });
});
