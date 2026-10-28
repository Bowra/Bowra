import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, it } from "vitest";

import { catalogText, expectRefreshedGlyph, type GlyphLocale } from "@/__tests__/catalog-glyph";
import { getCurrentLocale } from "@/lib/locale";

import { MissingSectionIdNote } from "./_helpers";

afterEach(() => {
  cleanup();
});

describe("MissingSectionIdNote", () => {
  it("renders ⚠ before the fallback message", () => {
    const { container } = render(<MissingSectionIdNote kind="chart" />);
    const message = catalogText(getCurrentLocale() as GlyphLocale, "canvas_node_fallback.missing_section_id", { kind: "chart" });
    const note = container.firstElementChild as HTMLElement;
    expectRefreshedGlyph(note, "⚠", "start", message);
  });
});
