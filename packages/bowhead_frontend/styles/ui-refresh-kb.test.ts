/**
 * Source contract for P2 batch 5 (two-level knowledge base navigation, knowledge base dialog interiors):
 * styles/ui-refresh-kb.css.
 *
 * jsdom does no cascade, so this pins what the source can prove; pixels and contrast are
 * verified in real browsers (docs/design/组件统一-技术设计.md "P2 第 5 批结果").
 */
import { describe, expect, it } from "vitest";

import { cascadeProblems, decls, LITERAL_COLOR, norm, parseRules, PREFIX, read, splitSelectors, stripComments } from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-kb.css"));
const RULES = parseRules(CSS);

/** Declarations of the rules whose selector list contains `sel` (after the :root:root prefix), in the given media. */
function rule(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => norm(full.slice(PREFIX.length)) === norm(sel));
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}


describe("styles/ui-refresh-kb.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(10);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("writes no literal colours, and layers only through --z-* tokens", () => {
    const literal = RULES.flatMap((r) => r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)));
    expect(literal).toEqual([]);
    const zIndex = RULES.map((r) => decls(r.body)["z-index"]).filter((v): v is string => v !== undefined);
    expect(zIndex.filter((v) => !/^var\(--z-[a-z-]+\)$/.test(v))).toEqual([]);
  });

  it("focus rings fall back to the accent (an undefined --c-focus would make the outline shorthand invalid)", () => {
    expect(CSS.match(/var\(--c-focus\)/g)).toBeNull();
    expect(rule(".kb-page-v2 .kb-controls .kb-controls-main .kb-filterchip:focus-visible")).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))" });
  });

  it("no later rule has lower specificity than an earlier rule for the same element", () => {
    expect(cascadeProblems(RULES)).toEqual([]);
  });

  it("follows the content stylesheet in the authenticated layout and stays out of the public layout", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    const content = imports.indexOf("../../styles/ui-refresh-content.css");
    expect(content).toBeGreaterThanOrEqual(0);
    expect(imports[content + 1]).toBe("../../styles/ui-refresh-kb.css");
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-kb.css");
  });
});

describe("sections (Header, Language & Paging Spec §2, D5)", () => {
  it("stacks the content under the PageTabs; no own tab rail, pill row or dropdown remains", () => {
    expect(rule(".kb-page-v2 .kbshell")).toMatchObject({ "flex-direction": "column", "align-items": "stretch" });
    const legacy = parseRules(stripComments(read("styles/kb-nav.css")));
    const navSelectors = [...RULES, ...legacy]
      .flatMap((r) => splitSelectors(r.selector))
      .filter((sel) => /\.(kbnav|kb-phnav|kbh|ph-sel|ph-menu)\b/.test(sel));
    expect(navSelectors).toEqual([]);
  });
});

describe("scope filter and scope column", () => {
  it("two-line single-select options: 44 high, secondary hint and count (the menu draws the check)", () => {
    expect(rule(".ui-menu .kb-scope-opt")).toMatchObject({ "min-height": "44px" });
    expect(rule(".ui-menu .kb-scope-opt .hint")).toMatchObject({ color: "var(--c-ink-3)" });
    expect(rule(".ui-menu .kb-scope-opt .count")).toMatchObject({ "font-variant-numeric": "tabular-nums" });
  });

  it("the team lock badge in scope cards is 11px secondary system text (no mono)", () => {
    expect(rule(".kb-scope-card .lock")).toMatchObject({ color: "var(--c-ink-3)", "font-family": "var(--font-control)", "font-size": "var(--fs-xs)" });
  });

  it("scope column cells stay on one line", () => {
    expect(rule(".kb-grid .kb-scope-cell")).toMatchObject({ "white-space": "nowrap", "text-overflow": "ellipsis" });
  });

  it("the filter does not reuse the kb-scope-* test ids of the upload and add-to-knowledge-base dialogs", () => {
    const source = read("components/knowledge/kb-scope-filter.tsx");
    expect(source.match(/data-testid=\{?[`"]kb-scope-(?!filter)/g)).toBeNull();
  });
});

describe("base classes moved from inline styles", () => {
  it("callers no longer carry the moved inline styles", () => {
    expect(read("components/knowledge/kb-preview.tsx")).not.toMatch(/style=\{\{|backdropStyle|panelStyle/);
    expect(read("components/knowledge/extraction-template-wizard.tsx")).not.toMatch(/style=\{\{/);
    expect(read("components/knowledge/kb-scope-modal.tsx")).not.toMatch(/style=\{\{/);
    expect(read("components/knowledge/matrix/graduate-dialog.tsx")).not.toMatch(/style=/);
    expect(read("components/knowledge/matrix/run-confirm-modal.tsx")).not.toMatch(/style=/);
  });
});

describe("file preview viewer", () => {
  it("solid panel, --r-4, overlay shadow, --c-scrim, 48 header with a hairline, 28 close", () => {
    expect(rule(".kb-preview-overlay")).toMatchObject({ background: "var(--c-scrim)", "backdrop-filter": "none" });
    expect(rule(".kb-preview-panel")).toMatchObject({ padding: "0", border: "0", "border-radius": "var(--r-4)", "box-shadow": "var(--shadow-overlay)" });
    expect(rule(".kb-preview-head")).toMatchObject({ "min-height": "48px", "border-bottom": "var(--hairline) solid var(--c-separator)" });
    expect(rule(".kb-preview-title")).toMatchObject({ "font-size": "var(--fs-dialog-title)", "font-weight": "650" });
    expect(rule(".kb-preview-close")).toMatchObject({ width: "var(--control-h-md)", height: "var(--control-h-md)" });
    expect(rule(".kb-preview-close", /pointer: coarse/)).toMatchObject({ width: "44px", height: "44px" });
  });
});

describe("dialog and drawer interiors", () => {
  it("notices are filled blocks at --r-3 without the left bar", () => {
    expect(rule(".kb-notice")).toMatchObject({ "border-left": "0", "border-radius": "var(--r-3)", background: "var(--c-fill-1)" });
    expect(rule(".kb-notice.err")).toMatchObject({ background: "var(--c-err-soft)" });
  });

  it("section labels lose the mono capitals; group blocks are filled at --r-3", () => {
    expect(rule(".kb-drawer-panel .sub-t")).toMatchObject({ "text-transform": "none", "font-family": "var(--font-control)", "font-weight": "600" });
    expect(rule(".kb-drawer-panel .chunks")).toMatchObject({ border: "0", "border-radius": "var(--r-3)", background: "var(--c-fill-1)" });
    expect(rule(".kb-pop-label.kb-scope-failed-label")).toMatchObject({ "text-transform": "none" });
  });

  it("selection cards in save-as-template use the radio-card look", () => {
    expect(rule(".kb-modal .mx-mode.on")).toMatchObject({ "box-shadow": "inset 0 0 0 1.5px var(--c-accent)" });
    expect(rule(".kb-modal .mx-mode.on .radio")).toMatchObject({ "box-shadow": "inset 0 0 0 5px var(--c-accent)" });
  });

  it("accent text on fills uses --c-accent-text", () => {
    for (const sel of [".kb-notice .kb-scope-to", ".kb-up-link", ".mx-estimate .big b"]) {
      expect(rule(sel), sel).toMatchObject({ color: "var(--c-accent-text)" });
    }
  });
});

describe("document matrix", () => {
  it("side panels keep their place in the matrix and take the drawer look", () => {
    expect(rule(".mx-panel")).toMatchObject({ "border-left": "0", "border-radius": "var(--r-4) 0 0 var(--r-4)", "box-shadow": "var(--shadow-overlay)" });
    expect(rule(".mx-panel", /max-width: 720px/)).toMatchObject({ "border-radius": "0" });
    expect(rule(".mx-panel-scrim")).toMatchObject({ background: "var(--c-scrim)" });
    expect(rule(".mx-panel-head")).toMatchObject({ "border-bottom": "var(--hairline) solid var(--c-separator)" });
    expect(rule(".mx-panel-head .col")).toMatchObject({ "font-size": "var(--fs-dialog-title)", "font-weight": "650" });
    expect(rule(".mx-panel-foot")).toMatchObject({ background: "transparent", "border-top": "var(--hairline) solid var(--c-separator)" });
    // Visual only (user decision 2026-10-01): no position, inset or z-index change for the panel family.
    for (const sel of [".mx-panel", ".mx-panelwrap", ".mx-panel-scrim"]) {
      const found = RULES.filter((r) => splitSelectors(r.selector).some((x) => norm(x.slice(PREFIX.length)) === sel));
      for (const r of found) expect(Object.keys(decls(r.body)).filter((k) => ["position", "inset", "z-index", "transform"].includes(k)), sel).toEqual([]);
    }
  });

  it("the citation card uses the page-local layer token instead of z-index 220", () => {
    expect(rule(".mx-citepop")).toMatchObject({ "z-index": "var(--z-dropdown)", "border-radius": "var(--r-3)", "box-shadow": "var(--shadow-overlay), inset 0 0 0 var(--hairline) var(--c-separator)" });
  });

  it("the column question tooltip follows the tooltip look", () => {
    expect(rule(".mx-question-text")).toMatchObject({ border: "0", "border-radius": "var(--r-2)", "font-family": "var(--font-control)" });
  });
});

describe("extraction-table wizard", () => {
  it("dialog layer token, scrim, solid panel at --r-4 and a 15/650 title", () => {
    expect(rule(".kt-wizard-overlay")).toMatchObject({ "z-index": "var(--z-dialog)", background: "var(--c-scrim)" });
    expect(rule(".kt-wizard-panel")).toMatchObject({ "border-radius": "var(--r-4)", "box-shadow": "var(--shadow-overlay)" });
    expect(rule(".kt-wizard-title")).toMatchObject({ "font-size": "var(--fs-dialog-title)", "font-weight": "650" });
  });

  it("inputs are controls with a visible focus ring; errors are filled notices", () => {
    expect(rule(":is(.kt-wizard-input, .kt-wizard-cell)")).toMatchObject({ "min-height": "var(--control-h-md)", "border-radius": "var(--r-2)" });
    expect(rule(":is(.kt-wizard-input, .kt-wizard-cell):focus-visible")["box-shadow"]).toContain("var(--c-accent)");
    expect(rule(".kt-wizard-error")).toMatchObject({ "border-radius": "var(--r-3)", background: "var(--c-err-soft)" });
  });
});
