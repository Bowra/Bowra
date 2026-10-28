import { readFileSync } from "node:fs";
import { join } from "node:path";

import { cleanup, render } from "@testing-library/react";
import { Download } from "lucide-react";
import { afterEach, describe, expect, it } from "vitest";


import { CHAR_ICON_MAP, CHAR_ICON_STROKE, CharIcon, CharIconText, CharIconTextOnly, charIconFor } from "./char-icon";

afterEach(cleanup);

describe("CharIcon", () => {
  it("draws the mapped Lucide glyph at 1em and keeps the character as the accessible name", () => {
    const { container } = render(<button type="button"><CharIcon ch="✕" /></button>);
    const svg = container.querySelector("svg") as SVGSVGElement;
    expect(svg.getAttribute("class")).toContain("lucide-x");
    expect(svg.getAttribute("class")).toContain("bh-icon bh-glyph");
    expect(svg.getAttribute("width")).toBe("1em");
    expect(svg.getAttribute("height")).toBe("1em");
    expect(svg.getAttribute("role")).toBe("img");
    expect(svg.getAttribute("aria-label")).toBe("✕");
    expect(svg.getAttribute("aria-hidden")).toBeNull();
    expect(svg.style.strokeWidth).toBe(CHAR_ICON_STROKE);
    expect(container.textContent).toBe("");
  });

  it("snaps a pixel size to the icon tiers and strokes it at 1.5", () => {
    const { container } = render(<CharIcon ch="✓" size={12} />);
    const svg = container.querySelector("svg") as SVGSVGElement;
    expect(svg.getAttribute("width")).toBe("14");
    expect(svg.style.strokeWidth).toBe("1.5");
  });

  it("stays hidden where the character was hidden", () => {
    const { container } = render(<span aria-hidden><CharIcon ch="→" aria-hidden /></span>);
    const svg = container.querySelector("svg") as SVGSVGElement;
    expect(svg.getAttribute("class")).toContain("lucide-arrow-right");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(svg.getAttribute("role")).toBeNull();
    expect(svg.getAttribute("aria-label")).toBeNull();
  });

  it("uses an explicit glyph, ignores the emoji variation selector and leaves unmapped characters as text", () => {
    const { container } = render(
      <p>
        <CharIcon ch="↓" icon={Download} />
        <CharIcon ch="🖼️" />
        <CharIcon ch="①" />
      </p>,
    );
    const classes = Array.from(container.querySelectorAll("svg")).map((el) => el.getAttribute("class") ?? "");
    expect(classes[0]).toContain("lucide-download");
    expect(classes[1]).toContain("lucide-image");
    expect(classes).toHaveLength(2);
    expect(container.textContent).toBe("①");
  });

  it("renders the bare character inside CharIconTextOnly (export-only visual route)", () => {
    const { container } = render(
      <CharIconTextOnly>
        <CharIcon ch="ⓘ" /> note
      </CharIconTextOnly>,
    );
    expect(container.innerHTML).toBe("ⓘ note");
  });
});

describe("CHAR_ICON_MAP", () => {
  it("covers every tool emoji the step list shows", () => {
    const root = join(__dirname, "..");
    const sources = ["components/chat/step-list/tool-catalog.ts", "lib/_generated/tool-display.ts"].map((rel) =>
      readFileSync(join(root, rel), "utf8"),
    );
    const emoji = new Set(sources.flatMap((src) => Array.from(src.matchAll(/(?:icon|sigil): "([^"]+)"/g), (m) => m[1])));
    expect(emoji.size).toBeGreaterThan(20);
    for (const ch of emoji) expect(charIconFor(ch), ch).toBeDefined();
  });

  it("stores keys without the emoji variation selector", () => {
    for (const key of Object.keys(CHAR_ICON_MAP)) expect(key, key).not.toContain("️");
  });
});

describe("CharIconText", () => {
  it("draws the glyph and keeps one space", () => {
    const { container } = render(<button type="button"><CharIconText ch="→" at="end">Next</CharIconText></button>);
    const button = container.querySelector("button") as HTMLButtonElement;
    const svg = button.querySelector("svg") as SVGSVGElement;
    expect(svg.getAttribute("class")).toContain("lucide-arrow-right");
    expect(svg.getAttribute("aria-label")).toBe("→");
    expect(button.textContent).toBe("Next ");
    expect(button.lastChild).toBe(svg);
  });
});
