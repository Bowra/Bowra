/**
 * Source contract for the first-level page header and column (Header, Language & Paging Spec §2, D4–D7):
 * styles/ui-refresh-page-frame.css. jsdom does not cascade; pixels are checked in real-browser screenshots.
 */
import { describe, expect, it } from "vitest";

import { cascadeProblems, decls, LITERAL_COLOR, norm, parseRules, PREFIX, read, splitSelectors, stripComments } from "./css-contract";

const RULES = parseRules(stripComments(read("styles/ui-refresh-page-frame.css")));

function rule(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => norm(full.slice(PREFIX.length)) === norm(sel));
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

const PHONE = /max-width: 767\.98px/;

describe("styles/ui-refresh-page-frame.css", () => {
  it("prefixes every rule with :root:root, writes no literal colours and keeps the cascade ordered", () => {
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
    const literal = RULES.flatMap((r) => r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)));
    expect(literal).toEqual([]);
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("one column: min(1120, main − 2 × gutter), gutter padding, centered, plain page background (D4, D7)", () => {
    expect(rule(".ui-page-frame")).toMatchObject({
      "box-sizing": "content-box",
      width: "min(var(--content-w-table), calc(100% - 2 * var(--page-gutter)))",
      "margin-inline": "auto",
      padding: "8px var(--page-gutter) 48px",
    });
    expect(rule('.ui-page-frame[data-width="form"]')).toMatchObject({
      width: "min(var(--content-w-form), calc(100% - 2 * var(--page-gutter)))",
    });
    expect(rule(".ui-page-frame-scroll")).toMatchObject({ overflow: "auto", background: "var(--c-bg)" });
    expect(RULES.some((r) => /gradient/.test(r.body))).toBe(false);
  });

  it("header: copy left, actions right, 16 to the tabs; tabs are underline links with 11px ink4 counts (D5)", () => {
    expect(rule(".ui-page-frame .ui-page-header")).toMatchObject({ display: "flex", "justify-content": "space-between", margin: "0 0 16px" });
    expect(rule(".ui-page-header__copy .ui-page-desc")).toMatchObject({ "max-width": "560px" });
    expect(rule(".ui-page-tabs")).toMatchObject({ margin: "0 0 16px", "box-shadow": "inset 0 calc(-1 * var(--hairline)) 0 var(--c-separator)" });
    expect(rule('.ui-page-tab[aria-current="page"]')).toMatchObject({ color: "var(--c-ink)", "font-weight": "600" });
    expect(rule('.ui-page-tab[aria-current="page"]::after')).toEqual({ opacity: "1" });
    expect(rule(".ui-page-tab .cnt")).toMatchObject({ color: "var(--c-ink-4)", "font-size": "var(--fs-xs)" });
  });

  it("phones: the header hides (the top bar names the page and carries the actions); ≤ 3 tabs become a segmented row", () => {
    expect(rule(".ui-page-frame .ui-page-header", PHONE)).toEqual({ display: "none" });
    expect(rule(".ui-page-mobile-actions", PHONE)).toMatchObject({ display: "inline-flex" });
    expect(rule(".ui-page-mobile-action")).toMatchObject({ width: "44px", height: "44px" });
    expect(rule('.ui-page-tabs:not([data-count="4"], [data-count="5"], [data-count="6"]) .ui-page-tab[aria-current="page"]', PHONE))
      .toMatchObject({ background: "var(--c-segment-on)" });
    expect(rule('.ui-page-tabs:is([data-count="4"], [data-count="5"], [data-count="6"]) .ui-page-tab', PHONE))
      .toMatchObject({ "min-height": "44px" });
  });

  it("is imported by the authenticated layout after the knowledge-base sheet and stays out of the public layout", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const kb = imports.indexOf("../../styles/ui-refresh-kb.css");
    expect(imports[kb + 1]).toBe("../../styles/ui-refresh-page-frame.css");
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-page-frame.css");
  });
});
