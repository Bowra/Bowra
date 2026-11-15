import { afterEach, describe, expect, it } from "vitest";

import { applyFrameOptionsToDoc, normalizeFrameOptions } from "./frame-options";

afterEach(() => applyFrameOptionsToDoc(undefined));

describe("team frame options", () => {
  it("keeps only known fields changed from the product look", () => {
    expect(
      normalizeFrameOptions({
        corners: "square",
        border: "standard",
        elevation: "glow",
        lineContrast: "strong",
        css: "a{}",
      }),
    ).toEqual({ corners: "square", lineContrast: "strong" });
    expect(normalizeFrameOptions(null)).toEqual({});
    expect(normalizeFrameOptions(["square"])).toEqual({});
  });

  it("writes chosen options to the page and removes the ones no longer chosen", () => {
    const root = document.documentElement;
    applyFrameOptionsToDoc({ corners: "square", border: "bold", elevation: "hard" });
    expect(root.getAttribute("data-frame-corners")).toBe("square");
    expect(root.getAttribute("data-frame-border")).toBe("bold");
    expect(root.getAttribute("data-frame-elevation")).toBe("hard");
    expect(root.hasAttribute("data-frame-line-contrast")).toBe(false);

    applyFrameOptionsToDoc({ lineContrast: "strong" });
    expect(root.hasAttribute("data-frame-corners")).toBe(false);
    expect(root.hasAttribute("data-frame-border")).toBe(false);
    expect(root.hasAttribute("data-frame-elevation")).toBe(false);
    expect(root.getAttribute("data-frame-line-contrast")).toBe("strong");
  });
});
