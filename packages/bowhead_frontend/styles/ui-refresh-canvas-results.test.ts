/**
 * Canvas results alignment style contracts (technical design “Canvas results preview alignment”).
 *
 * Source facts only (jsdom does not cascade); real-browser screenshots validate pixels.
 *   1. Results surfaces (explorer, quick switcher, navigator, object detail, code runs, states) use the current design language:
 *      sidebar-style 44 rows without borders, filled search fields and secondary buttons in the system font, hairline separators,
 *      no card nested inside the results card, one accent-text primary per toolbar, 14 / 16 / 20 icon tiers.
 *   2. Split gutter (workbench file): two rounded cards with the divider inside the pane gap, never crossing a card edge.
 *   3. Chat file card: content-card pattern, system font, filled 28 download icon button.
 * Every rule starts with :root:root (enforced for whole files by ui-refresh-canvas / ui-refresh-workbench tests).
 */
import { describe, expect, it } from "vitest";

import { decls, norm, parseRules, PREFIX, read, specificity, splitSelectors, stripComments, type Rule } from "./css-contract";

function section(rel: string, marker: string, end?: string): Rule[] {
  const css = read(rel);
  const at = css.indexOf(marker);
  expect(at, `${rel}: ${marker}`).toBeGreaterThan(-1);
  // Slice from the comment that carries the marker up to the comment that carries the end marker.
  const start = css.lastIndexOf("/*", at);
  const endAt = end ? css.indexOf(end, at + marker.length) : -1;
  const stop = endAt < 0 ? -1 : css.lastIndexOf("/*", endAt);
  return parseRules(stripComments(css.slice(start, stop < 0 ? undefined : stop)));
}

const RESULTS = section("styles/ui-refresh-canvas.css", "Canvas results alignment (P2 follow-up");
const SPLIT = section("styles/ui-refresh-workbench.css", "Canvas results alignment · split gutter", "═══ Collapsed restore strip");

/** Expand a prefixed selector into plain alternatives: `:is(a, b):hover` → [`a:hover`, `b:hover`]. */
function alternatives(selector: string): string[] {
  const rest = norm(selector.slice(PREFIX.length));
  if (!rest.startsWith(":is(")) return [rest];
  let depth = 0;
  let close = -1;
  for (let i = 3; i < rest.length; i += 1) {
    if (rest[i] === "(") depth += 1;
    else if (rest[i] === ")" && --depth === 0) {
      close = i;
      break;
    }
  }
  const suffix = rest.slice(close + 1);
  return splitSelectors(rest.slice(4, close)).map((x) => norm(x + suffix));
}

/** Merged declarations of every rule (in `rules`) whose selector list contains `sel`; `media` filters by condition. */
function rule(rules: Rule[], sel: string, media: RegExp | null = null): Record<string, string> {
  const found = rules.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => alternatives(full).includes(norm(sel)));
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

/** Declarations of the single rule whose selector text contains every fragment (for compound `:is() :is()` selectors). */
function ruleWith(rules: Rule[], ...fragments: string[]): Record<string, string> {
  const found = rules.filter((r) => r.media === null && fragments.every((f) => norm(r.selector).includes(f)));
  expect(found.length, fragments.join(" + ")).toBe(1);
  return decls(found[0].body);
}

describe("Results list rows: sidebar row pattern", () => {
  it.each([".results-explorer-row", ".results-quick-option", ".results-nav-item"])("%s is 44 high, r2, borderless, transparent", (sel) => {
    expect(rule(RESULTS, sel)).toMatchObject({
      "min-height": "44px",
      border: "0",
      "border-radius": "var(--r-2)",
      background: "transparent",
      "font-family": "var(--font-control)",
    });
    expect(rule(RESULTS, `${sel}:hover`)).toEqual({ background: "var(--c-fill-1)" });
  });

  it("selected rows are fill-2 (no accent border), title 600, meta raised to ink-2 for 4.5:1 on fill-2", () => {
    for (const sel of [".results-explorer-row.selected", ".results-quick-option.selected", ".results-nav-item.active"]) {
      expect(rule(RESULTS, sel)).toEqual({ background: "var(--c-fill-2)" });
    }
    const selected = ".results-explorer-row.selected, .results-quick-option.selected, .results-nav-item.active";
    expect(ruleWith(RESULTS, selected, ".results-explorer-row-copy strong")).toEqual({ "font-weight": "600" });
    expect(ruleWith(RESULTS, selected, ".results-explorer-row-copy span")).toEqual({ color: "var(--c-ink-2)" });
  });

  it("type badges are filled chips without a border box; titles 13/500, meta 12 tabular", () => {
    expect(rule(RESULTS, ".results-explorer-badge")).toMatchObject({ border: "0", background: "var(--c-fill-2)", "border-radius": "var(--r-1)" });
    expect(rule(RESULTS, ".results-quick-copy strong")).toMatchObject({ "font-size": "var(--fs-md)", "font-weight": "500" });
    expect(rule(RESULTS, ".results-nav-copy span")).toMatchObject({ "font-size": "var(--fs-sm)", "font-variant-numeric": "tabular-nums" });
  });

  it("group headings are 12/600 system font, not mono uppercase", () => {
    expect(rule(RESULTS, ".results-nav-group h3")).toMatchObject({
      "font-family": "var(--font-control)",
      "font-size": "var(--fs-sm)",
      "text-transform": "none",
      "letter-spacing": "normal",
    });
    expect(rule(RESULTS, ".results-explorer-group button")).toMatchObject({ "min-height": "32px", "border-radius": "var(--r-2)" });
  });

  it("row focus is an inset 2px focus ring with accent fallback", () => {
    expect(rule(RESULTS, ".results-explorer-row:focus-visible")).toEqual({
      outline: "2px solid var(--c-focus, var(--c-accent))",
      "outline-offset": "-2px",
    });
  });
});

describe("Results controls", () => {
  it("search fields follow the filled 28 search spec with an accent focus ring", () => {
    for (const sel of [".results-explorer-controls > input[type=\"search\"]", ".results-quick-search-wrap input", ".results-nav-search"]) {
      expect(rule(RESULTS, sel)).toMatchObject({
        "min-height": "var(--control-h-md)",
        border: "0",
        "border-radius": "var(--r-2)",
        background: "var(--c-fill-2)",
      });
      expect(rule(RESULTS, `${sel}:focus-visible`)["box-shadow"]).toContain("var(--c-focus, var(--c-accent))");
    }
  });

  it("toolbar and state buttons are secondary buttons: filled, borderless, system font, control height", () => {
    for (const sel of [
      ".result-file-viewer .artifact-viewer-actions button",
      ".result-file-viewer .artifact-viewer-actions a",
      ".results-quick-footer button",
      ".results-explorer-state button",
      ".code-run-actions button",
    ]) {
      expect(rule(RESULTS, sel)).toMatchObject({
        border: "0",
        "border-radius": "var(--r-2)",
        background: "var(--c-fill-2)",
        "font-family": "var(--font-control)",
        "min-height": "var(--control-h-md)",
      });
    }
  });

  it("one accent-text primary per toolbar: the first of two adjacent primaries drops back to ink", () => {
    expect(rule(RESULTS, ".result-file-viewer .result-object-action-primary")).toMatchObject({ color: "var(--c-accent-text) !important" });
    expect(rule(RESULTS, ".result-file-viewer .result-object-action-primary:has(+ .result-object-action-primary)")).toMatchObject({
      color: "var(--c-ink) !important",
    });
  });

  it("return bars are ghost toolbar buttons", () => {
    expect(rule(RESULTS, ".results-return-context button")).toMatchObject({
      background: "transparent",
      border: "0",
      "font-size": "var(--fs-md)",
      color: "var(--c-ink-2)",
    });
    expect(rule(RESULTS, ".results-explorer-back:hover")).toMatchObject({ background: "var(--c-fill-1)" });
  });
});

describe("Object detail: no nested card", () => {
  it("the viewer and details inside the results card drop border, radius, background and shadow", () => {
    for (const sel of [".result-file-viewer > .artifact-viewer", ".result-file-viewer > .artifact-details"]) {
      expect(rule(RESULTS, sel)).toEqual({ border: "0", "border-radius": "0", background: "transparent", "box-shadow": "none" });
    }
    expect(rule(RESULTS, ".result-file-viewer > .artifact-viewer > .artifact-viewer-head")).toMatchObject({
      background: "transparent",
      "border-bottom": "var(--hairline) solid var(--c-separator)",
    });
  });

  it("metadata, status rows and footnotes use the 12 system font (no mono), details values 13", () => {
    expect(rule(RESULTS, ".result-file-viewer .artifact-document-status")).toMatchObject({
      "font-family": "var(--font-control)",
      "font-size": "var(--fs-sm)",
      background: "transparent",
    });
    expect(rule(RESULTS, ".result-file-viewer .artifact-viewer-footnote")).toMatchObject({ "font-family": "var(--font-control)" });
    expect(rule(RESULTS, ".result-file-viewer .artifact-details dd")).toMatchObject({
      "font-family": "var(--font-control)",
      "font-size": "var(--fs-md)",
    });
  });

  it("content frames are a single hairline r3 frame; the content toolbar is a plain row", () => {
    expect(rule(RESULTS, ".result-file-viewer .safe-html-preview")).toMatchObject({
      border: "var(--hairline) solid var(--c-separator)",
      "border-radius": "var(--r-3)",
    });
    expect(rule(RESULTS, ".result-file-viewer .artifact-content-toolbar")).toMatchObject({ border: "0", background: "transparent" });
  });

  it("code runs are hairline r3 cards without shadow, keeping running / error status edges", () => {
    expect(rule(RESULTS, ".code-run-card")).toMatchObject({ "border-radius": "var(--r-3)", "box-shadow": "none" });
    expect(rule(RESULTS, ".code-run-card[data-status=\"running\"]")["border-color"]).toContain("var(--c-accent)");
    expect(rule(RESULTS, ".code-run-card[data-status=\"error\"]")["border-color"]).toContain("var(--c-err)");
  });
});

describe("States and icon tiers", () => {
  it("state titles 14/600, state icon 20, error icon in the error colour", () => {
    expect(rule(RESULTS, ".results-explorer-state strong")).toMatchObject({ "font-size": "var(--fs-base)", "font-weight": "600" });
    expect(rule(RESULTS, ".results-state-icon svg")).toEqual({ width: "20px", height: "20px" });
    expect(rule(RESULTS, ".results-state-icon[data-tone=\"error\"]")).toEqual({ color: "var(--c-err)" });
  });

  it("every svg size in the section is a 14 / 16 / 20 tier", () => {
    const sizes = RESULTS.filter((r) => /svg/.test(r.selector)).flatMap((r) => {
      const d = decls(r.body);
      return [d.width, d.height].filter(Boolean);
    });
    expect(sizes.length).toBeGreaterThan(4);
    expect(sizes.filter((v) => !["14px", "16px", "20px"].includes(v))).toEqual([]);
  });

  it("no bordered boxes come back: no rule in the section sets a 1px solid border or uses --c-line", () => {
    const offenders = RESULTS.filter((r) => /\b1px solid|var\(--c-line\)/.test(r.body)).map((r) => r.selector);
    expect(offenders).toEqual([]);
  });
});

describe("Split gutter (workbench)", () => {
  const DESKTOP = /min-width: 1024px/;

  it("only applies from the 1024 desktop layout and only in the split state (divider mounted)", () => {
    expect(SPLIT.length).toBeGreaterThan(4);
    expect(SPLIT.every((r) => DESKTOP.test(r.media ?? ""))).toBe(true);
    expect(rule(SPLIT, ".v2-split-area:has(> .v2-split-divider) > .v2-split-chat > .v2-ws-left", DESKTOP)).toEqual({
      "margin-right": "calc(var(--theme-ws-pane-gap, 16px) / 2)",
      "border-radius": "var(--theme-ws-pane-radius, 18px)",
    });
    expect(rule(SPLIT, ".v2-split-area:has(> .v2-split-divider) > .v2-split-canvas > .v2-ws-right", DESKTOP)).toEqual({
      "margin-left": "calc(var(--theme-ws-pane-gap, 16px) / 2)",
      "border-radius": "var(--theme-ws-pane-radius, 18px)",
    });
  });

  it("the divider is gutter-wide, card-high and centred on the boundary (SplitPane writes left = boundary − 7px)", () => {
    expect(rule(SPLIT, ".v2-split-area > .v2-split-divider", DESKTOP)).toEqual({
      top: "var(--theme-ws-pane-gap, 16px)",
      bottom: "var(--theme-ws-pane-gap, 16px)",
      width: "var(--theme-ws-pane-gap, 16px)",
      "margin-left": "calc(7px - var(--theme-ws-pane-gap, 16px) / 2)",
    });
  });

  it("no line at rest; ink-3 on hover; 2px accent while dragging and on keyboard focus", () => {
    expect(rule(SPLIT, ".v2-split-area > .v2-split-divider > .v2-split-divider-line", DESKTOP)).toMatchObject({ background: "transparent" });
    expect(rule(SPLIT, ".v2-split-area > .v2-split-divider.is-active > .v2-split-divider-line", DESKTOP)).toEqual({ background: "var(--c-ink-3)" });
    expect(rule(SPLIT, ".v2-split-area > .v2-split-divider.is-active:active > .v2-split-divider-line", DESKTOP)).toMatchObject({
      width: "2px",
      background: "var(--c-accent)",
    });
    expect(rule(SPLIT, ".v2-split-area > .v2-split-divider:focus-visible > .v2-split-divider-line", DESKTOP)).toMatchObject({
      width: "2px",
      background: "var(--c-focus, var(--c-accent))",
    });
  });
});

describe("quick switcher bottom sheet on phones", () => {
  const ALL = parseRules(stripComments(read("styles/ui-refresh-canvas.css")));
  const searchRuleIndex = ALL.findIndex(
    (r) => r.selector.includes(".results-quick-search-wrap input") && !r.selector.includes("ui-dialog-sheet"),
  );
  const sheetIndex = ALL.findIndex((r) =>
    norm(r.selector).includes(
      '.ui-dialog-sheet > .results-quick[data-dialog-presentation="sheet"] .results-quick-search-wrap input',
    ),
  );

  it("keeps the search field at 16px so iOS does not zoom on focus", () => {
    expect(searchRuleIndex).toBeGreaterThan(-1);
    expect(sheetIndex).toBeGreaterThan(-1);
    expect(norm(decls(ALL[sheetIndex]!.body)["font-size"] ?? "")).toBe("max(1rem, 16px)");
    // It must win over the 13px R2 search rule: later in the file and at least as specific.
    expect(sheetIndex).toBeGreaterThan(searchRuleIndex);
    const r2 = Math.max(...splitSelectors(ALL[searchRuleIndex]!.selector).map((s) => specificity(s)[1]));
    expect(specificity(ALL[sheetIndex]!.selector)[1]).toBeGreaterThanOrEqual(r2);
  });
});
