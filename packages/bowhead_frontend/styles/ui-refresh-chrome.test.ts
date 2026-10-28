/**
 * Source contract for P2 batch 3 (sidebar and topbar visuals, icon size tiers): styles/ui-refresh-chrome.css.
 *
 * jsdom does no cascade, so this pins what the source can prove; pixels and contrast are
 * verified in real browsers (docs/design/组件统一-技术设计.md "P2 第 3 批结果").
 *   1. Every rule starts with the :root:root prefix; no literal colours;
 *      imported last by the authenticated layout and never by the public layout.
 *   2. Spec values from "P2 Sidebar & Topbar Spec": 32 rows with 16 icons, neutral selection, section headers,
 *      session rows, identity row, rail, touch metrics, topbar and its three controls.
 *   3. Icon sizes: every icon box this file sets is one of 14 / 16 / 20, and the icon-size overrides only touch
 *      refreshed (Lucide) icons, so legacy SVGs, brand marks and charts keep their sizes.
 */
import { describe, expect, it } from "vitest";

import { decls, LITERAL_COLOR, norm, parseRules, PREFIX, read, splitSelectors, stripComments } from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-chrome.css"));
const RULES = parseRules(CSS);

/** Expands a leading `:is(a, b)rest` (balanced parentheses) into `a rest`, `b rest`. */
function expandLeadingIs(selector: string): string[] {
  if (!selector.startsWith(":is(")) return [selector];
  let depth = 0;
  for (let i = 3; i < selector.length; i += 1) {
    if (selector[i] === "(") depth += 1;
    else if (selector[i] === ")") {
      depth -= 1;
      if (depth === 0) {
        const rest = selector.slice(i + 1);
        return splitSelectors(selector.slice(4, i)).map((x) => norm(x + rest));
      }
    }
  }
  return [selector];
}

/** Declarations of rules (outside @media unless `media` is given) whose selector list contains `sel`. */
function rule(sel: string, media?: RegExp): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => expandLeadingIs(norm(full.slice(PREFIX.length))).includes(norm(sel)));
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

/** Declarations of the rules whose selector text contains every fragment. */
function ruleWith(...fragments: string[]): Record<string, string> {
  const found = RULES.filter((r) => r.media === null && fragments.every((f) => norm(r.selector).includes(norm(f))));
  expect(found.length, fragments.join(" ")).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("phone top bar: Space chip next to the session ⋯", () => {
  it("turns icon-only and stops shrinking when the bar also carries the session menu (390 sliver)", () => {
    const phone = /max-width: 767\.98px/;
    expect(rule(".v2-mobile-topbar-right:has(.session-topbar-menu) .v2-ws-picker", phone)).toMatchObject({
      flex: "none",
      "max-width": "none",
    });
    expect(rule(".v2-mobile-topbar-right:has(.session-topbar-menu) .v2-ws-picker-name", phone)).toMatchObject({
      display: "none",
    });
  });
});

describe("styles/ui-refresh-chrome.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(40);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("writes no literal colours and no z-index", () => {
    // mask-image tokens (--glyph-*) draw with black on purpose: it is the mask, not a colour
    const literal = RULES.flatMap((r) =>
      r.body.split(";").map((x) => x.trim()).filter((x) => !x.startsWith("--glyph-") && LITERAL_COLOR.test(x)),
    );
    expect(literal).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-chrome.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    // P2 batch 6 content styles and the P2 batch 5 knowledge base sheet come after it
    const expectedImports = ["../../styles/ui-refresh-chrome.css", "../../styles/ui-refresh-content.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-chrome.css");
  });
});

describe("sidebar (P2 Sidebar & Topbar Spec)", () => {
  it("persistent expanded sidebar is 248 wide with 8 side padding; header 48, search 28", () => {
    expect(rule(".unified-sidebar.is-expanded")).toMatchObject({ width: "248px", "flex-basis": "248px" });
    expect(rule(".unified-sidebar-expanded")).toMatchObject({ padding: "0 8px 8px", "font-family": "var(--font-control)" });
    expect(rule(".unified-sidebar-header")).toMatchObject({ height: "48px" });
    expect(rule(".unified-work-search input")).toMatchObject({ height: "28px" });
  });

  it("navigation rows: 32 high, radius 7, 13px system font, 16px icon in --c-ink-2", () => {
    for (const sel of [".unified-sidebar-nav-item", ".unified-drawer-nav-item", ".unified-sidebar-subnav-item", ".unified-drawer-subnav-item"]) {
      expect(rule(sel), sel).toMatchObject({
        "min-height": "32px",
        "border-radius": "7px",
        font: "400 var(--fs-md) / 1.35 var(--font-control)",
      });
    }
    expect(rule(".unified-sidebar-nav-item > svg:first-child")).toMatchObject({ width: "16px", height: "16px", color: "var(--c-ink-2)" });
  });

  it("selection is neutral fill-2 + 600 with an accent icon and no colour bar; hover fill-1, pressed fill-2", () => {
    expect(rule('.unified-sidebar-nav-item:is( .is-active, [aria-current="page"] )')).toMatchObject({
      background: "var(--c-fill-2)",
      color: "var(--c-ink)",
      "font-weight": "600",
    });
    expect(ruleWith('[aria-current="page"]', "> svg:first-child")).toMatchObject({ color: "var(--c-accent)" });
    expect(ruleWith(".unified-sidebar-nav-item", ".is-active::before")).toMatchObject({ content: "none" });
    expect(rule(".unified-sidebar-nav-item:hover")).toMatchObject({ background: "var(--c-fill-1)" });
    expect(rule(".unified-sidebar-nav-item:active")).toMatchObject({ background: "var(--c-fill-2)" });
    expect(ruleWith(".unified-sidebar-nav-group-row", ':has(> [aria-current="page"])')).toMatchObject({ background: "var(--c-fill-2)" });
    expect(rule(".unified-sidebar-nav-group-row.is-active")).toMatchObject({ background: "transparent" });
  });

  it("keyboard focus is a 2px inset ring (offset -2) on rows, rail buttons, session rows and the identity row", () => {
    const focus = ruleWith(".unified-sidebar-nav-item", ".unified-work-item-main", ".unified-sidebar-user", ":focus-visible");
    expect(focus).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))", "outline-offset": "-2px" });
  });

  it("group chevron: 24 hit area, 14px, --c-ink-3, right when collapsed and down when expanded", () => {
    expect(rule(".unified-sidebar-group-toggle")).toMatchObject({ width: "24px", height: "24px", color: "var(--c-ink-3)" });
    expect(rule(".unified-sidebar-group-toggle > svg")).toMatchObject({ width: "14px", height: "14px", transform: "rotate(-90deg)" });
    expect(rule('.unified-sidebar-group-toggle[aria-expanded="true"] > svg')).toMatchObject({ transform: "none" });
    expect(rule(".unified-sidebar-subnav-item")).toMatchObject({ "padding-left": "32px" });
    expect(rule(".unified-sidebar-subnav::before")).toMatchObject({ content: "none" });
  });

  it("section headers are 11/600 system font in --c-ink-3, 12 above and 4 below, never uppercase or monospace", () => {
    for (const sel of [".unified-sidebar-section-label", ".unified-work-group h3", ".unified-sidebar-flyout-title"]) {
      expect(rule(sel), sel).toMatchObject({
        padding: "12px 8px 4px",
        color: "var(--c-ink-3)",
        font: "600 0.6875rem / 1rem var(--font-control)",
        "text-transform": "none",
        "letter-spacing": "normal",
      });
    }
  });

  it("session rows: 44 high, title 13/500, second line 12px tabular; selected fill-2 with a --c-ink-2 second line", () => {
    expect(rule(".unified-work-item")).toMatchObject({ "min-height": "44px" });
    // only block and left padding: the legacy right padding keeps room for the row actions (hover and touch)
    expect(rule(".unified-work-item-main")).toMatchObject({ "padding-block": "5px", "padding-left": "8px" });
    expect(rule(".unified-work-item-main")).not.toHaveProperty("padding");
    expect(rule(".unified-work-item-main")).not.toHaveProperty("padding-right");
    expect(rule(".unified-work-title")).toMatchObject({ font: "500 var(--fs-md) / 1.125rem var(--font-control)" });
    expect(rule(".unified-work-meta")).toMatchObject({
      "min-height": "16px",
      font: "400 var(--fs-sm) / 1rem var(--font-control)",
      "font-variant-numeric": "tabular-nums",
    });
    expect(rule(".unified-work-item.is-active")).toMatchObject({ background: "var(--c-fill-2)", "box-shadow": "none" });
    expect(rule(".unified-work-item.is-active .unified-work-meta")).toMatchObject({ color: "var(--c-ink-2)" });
    expect(rule(".unified-work-item.is-active .unified-work-title")).toMatchObject({ "font-weight": "600" });
    expect(rule(".unified-work-item.is-unread .unified-work-title")).toMatchObject({ "font-weight": "600" });
  });

  it("session rows: touch keeps room for the always-visible “⋯” without inline buttons; no unread dot rules", () => {
    expect(rule(".unified-work-item.has-more .unified-work-item-main", /^@media \(hover: none\)$/)).toMatchObject({
      "padding-right": "42px",
    });
    expect(RULES.filter((r) => r.selector.includes("unified-work-read-state"))).toEqual([]);
  });

  it("states: still accent dot for running (2s fade only without reduced motion), warning dot, error circle-x; text colours readable", () => {
    expect(rule(".unified-work-lifecycle-state.active")).toMatchObject({ color: "var(--c-accent-text)" });
    expect(rule(".unified-work-lifecycle-state.pending")).toMatchObject({ color: "var(--c-warn-ink)" });
    expect(rule(".unified-work-lifecycle-state.error")).toMatchObject({ color: "var(--c-err-ink)" });
    expect(rule(".unified-work-lifecycle-state:is(.active, .pending)::before")).toMatchObject({ width: "6px", height: "6px", background: "var(--c-accent)" });
    expect(rule(".unified-work-lifecycle-state.pending::before")).toMatchObject({ background: "var(--c-warn)" });
    expect(rule(".unified-work-lifecycle-state.error::before")).toMatchObject({ mask: "var(--glyph-circle-x) center / contain no-repeat" });
    const animated = RULES.filter((r) => "animation" in decls(r.body));
    expect(animated.length).toBeGreaterThan(0);
    expect([...new Set(animated.map((r) => r.media))]).toEqual(["@media (prefers-reduced-motion: no-preference)"]);
  });

  it("identity row 52 with a 28 avatar and 14 chevron; rail buttons 36 with 20 icons and no colour bar", () => {
    expect(rule(".unified-sidebar-user")).toMatchObject({ height: "auto", "min-height": "52px" });
    expect(rule(".unified-sidebar-user .v2-sb-avatar")).toMatchObject({ width: "28px", height: "28px" });
    expect(rule(".unified-sidebar-user > svg:last-child")).toMatchObject({ width: "14px", height: "14px", color: "var(--c-ink-3)" });
    expect(rule(".unified-sidebar-user-copy small")).toMatchObject({ color: "var(--c-ink-3)", font: "400 var(--fs-sm) / 1rem var(--font-control)" });
    expect(rule(".unified-sidebar-compact-button")).toMatchObject({ width: "36px", height: "36px" });
    expect(rule(".unified-sidebar-compact-button > svg")).toMatchObject({ width: "20px", height: "20px" });
    expect(rule(".unified-sidebar-compact-button.is-active::before")).toMatchObject({ content: "none" });
  });

  it("touch (pointer: coarse) rows 44 with 20 icons, session rows 56, identity row 60, chevron 44", () => {
    const coarse = /\(pointer: coarse\)/;
    expect(rule(".unified-sidebar-nav-item", coarse)).toMatchObject({ "min-height": "44px" });
    expect(rule(".unified-sidebar-nav-item > svg:first-child", coarse)).toMatchObject({ width: "20px", height: "20px" });
    expect(rule(".unified-work-item", coarse)).toMatchObject({ "min-height": "56px" });
    expect(rule(".unified-sidebar-user", coarse)).toMatchObject({ height: "auto", "min-height": "60px" });
    expect(rule(".unified-sidebar-group-toggle", coarse)).toMatchObject({ width: "44px", height: "44px" });
  });
});

describe("topbar (P2 Sidebar & Topbar Spec)", () => {
  it("44 high; title 13/600; session ⋯ menu 28; right controls 4 apart", () => {
    expect(rule(".v2-main-head")).toMatchObject({ height: "44px", padding: "0 12px 0 20px" });
    expect(rule(".v2-main-head-titles")).toMatchObject({ display: "flex", "align-items": "baseline" });
    expect(rule(".v2-main-head .title")).toMatchObject({ font: "600 var(--fs-md) / 1.125rem var(--font-control)" });
    // --c-ink-2: --c-ink-3 falls under 4.5:1 on the scrolled bar material in the default warm light theme
    expect(rule(".v2-main-head .meta")).toMatchObject({ color: "var(--c-ink-2)", "font-variant-numeric": "tabular-nums" });
    expect(rule(".v2-main-head .session-topbar-menu__trigger.unified-work-more")).toMatchObject({ width: "var(--control-h-md)", height: "var(--control-h-md)" });
    expect(rule(".v2-main-head-controls")).toMatchObject({ gap: "4px", "margin-left": "auto" });
    expect(rule(".v2-main-head .v2-icon-btn", /not \(pointer: coarse\)/)).toMatchObject({ "--control-h-md": "28px" });
  });

  it("space chip 28 high on fill-1, at most 200 wide; bell badge accent with a ring; credits change colour only when low", () => {
    expect(rule(".v2-ws-picker")).toMatchObject({ height: "28px", "max-width": "200px", background: "var(--c-fill-1)" });
    expect(rule(".notification-bell__badge")).toMatchObject({
      "min-width": "16px",
      "min-height": "16px",
      background: "var(--c-accent)",
      color: "var(--c-accent-ink)",
      "box-shadow": "0 0 0 2px var(--c-bg)",
    });
    expect(rule(".notification-bell__dot")).toMatchObject({ width: "7px", height: "7px", background: "var(--c-accent)" });
    expect(rule(".ui-credit-badge")).toMatchObject({ height: "max(28px, 1.6em)", color: "var(--c-ink-2)", "font-variant-numeric": "tabular-nums" });
    expect(rule('.ui-credit-badge[data-level="low"]')).toMatchObject({ color: "var(--c-warn-ink)" });
    expect(rule('.ui-credit-badge[data-level="critical"]')).toMatchObject({ background: "var(--c-err-soft)" });
  });

  it("space chip button is reset by class (no inline all: unset) and focus rings the whole chip", () => {
    expect(rule(".v2-ws-picker > .v2-ws-picker-button")).toMatchObject({ border: "0", background: "transparent", gap: "6px" });
    expect(rule(".v2-ws-picker:has(> .v2-ws-picker-button:focus-visible)")).toMatchObject({
      outline: "2px solid var(--c-focus, var(--c-accent))",
    });
  });

  it("credits badge is a reset button with hover, open and focus states", () => {
    expect(rule(".ui-credit-badge")).toMatchObject({ border: "0", background: "transparent", cursor: "pointer" });
    expect(rule('.ui-credit-badge[aria-expanded="true"]')).toMatchObject({ background: "var(--c-fill-1)" });
    expect(rule(".ui-credit-badge:focus-visible")).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))" });
  });

  it("768–1023 icon-only credits: a 44 x 44 button with the visible 26 x 28 chip, fills and focus ring on ::before", () => {
    const band = /min-width: 768px\) and \(max-width: 1023\.98px/;
    const sel = ".v2-mobile-topbar-right .ui-credit-badge";
    // The button is the touch target; negative vertical margins keep the 28 row inside the 44 bar.
    expect(rule(sel, band)).toMatchObject({ position: "relative", "min-width": "44px", height: "44px", margin: "-8px 0", padding: "0" });
    expect(rule(`${sel}:is(:hover, :active, [aria-expanded="true"], [data-level])`, band)).toMatchObject({ background: "transparent" });
    expect(rule(`${sel}:focus-visible`, band)).toMatchObject({ outline: "none" });
    // 44 - 2 x 9 = 26 wide, 44 - 2 x 8 = 28 high: the chip keeps its previous size.
    expect(rule(`${sel}::before`, band)).toMatchObject({ position: "absolute", inset: "8px 9px", "pointer-events": "none" });
    expect(rule(`${sel}:is(:hover, [aria-expanded="true"])::before`, band)).toMatchObject({ background: "var(--c-fill-1)" });
    expect(rule(`${sel}:active::before`, band)).toMatchObject({ background: "var(--c-fill-2)" });
    expect(rule(`${sel}[data-level="critical"]::before`, band)).toMatchObject({ background: "var(--c-err-soft)" });
    expect(rule(`${sel}:focus-visible::before`, band)).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))" });
    // The icon paints above the chip by order, not z-index; its size is untouched (14 from the base rule).
    expect(rule(`${sel} > svg`, band)).toEqual({ position: "relative" });
  });

  it("credits popover: overlay material, readable text tokens, state-coloured bar and message", () => {
    expect(rule(".ui-credit-pop")).toMatchObject({
      background: "var(--ui-material-bg)",
      "backdrop-filter": "var(--ui-material-filter)",
      color: "var(--c-ink)",
      "border-radius": "12px",
    });
    expect(rule(".ui-credit-pop__title")).toMatchObject({ color: "var(--c-ink-2)" });
    expect(rule(".ui-credit-pop__meta")).toMatchObject({ color: "var(--c-ink-2)" });
    expect(rule(".ui-credit-pop__fill")).toMatchObject({ width: "var(--ui-credit-used, 0%)", background: "var(--c-accent)" });
    expect(rule('.ui-credit-pop__bar[data-level="low"] .ui-credit-pop__fill')).toMatchObject({ background: "var(--c-warn)" });
    expect(rule('.ui-credit-pop__bar[data-level="critical"] .ui-credit-pop__fill')).toMatchObject({ background: "var(--c-err)" });
    expect(rule('.ui-credit-pop__state[data-level="low"]')).toMatchObject({ color: "var(--c-warn-ink)" });
    expect(rule('.ui-credit-pop__state[data-level="critical"]')).toMatchObject({ color: "var(--c-err-ink)" });
    expect(rule(".ui-credit-pop__link")).toMatchObject({ color: "var(--c-accent-text)" });
  });

  it("mobile topbar 44 high with a borderless 44 menu button", () => {
    const narrow = /max-width: 1023\.98px/; // P2 batch 4: the mobile top bar is below 1024
    expect(rule(".v2-mobile-topbar", narrow)).toMatchObject({ height: "44px" });
    expect(rule(".v2-mobile-topbar-hamburger", narrow)).toMatchObject({ width: "44px", height: "44px", border: "0" });
  });
});

describe("unified navigation (P2 batch 4, P2 Navigation Spec)", () => {
  it("keeps the expanded sidebar persistent from 1024: 232 below 1280, 248 from 1280", () => {
    expect(rule(".unified-sidebar.is-expanded", /min-width: 1024px\) and \(max-width: 1279\.98px/)).toMatchObject({
      width: "232px",
      "flex-basis": "232px",
    });
    expect(rule(".unified-sidebar.is-expanded")).toMatchObject({ width: "248px" });
  });

  it("scrolls the settings list on its own and gives the filter the work-search metrics", () => {
    expect(rule(".unified-settings-list")).toMatchObject({ "overflow-y": "auto", "min-height": "0" });
    expect(rule(".unified-settings-search input")).toMatchObject({ height: "28px", background: "var(--c-fill-1)" });
  });

  it("draws counts as plain --c-ink-3 figures and pending counts as accent-soft pills", () => {
    expect(rule(".unified-settings-count")).toMatchObject({
      color: "var(--c-ink-3)",
      "font-variant-numeric": "tabular-nums",
      "margin-left": "auto",
    });
    expect(rule(".unified-settings-count.is-pending")).toMatchObject({
      color: "var(--c-accent-text)",
      background: "var(--c-accent-soft)",
      "border-radius": "999px",
    });
  });
});

describe("icon sizes (14 / 16 / 20 tiers)", () => {
  /** Rules whose last compound targets an svg or a refreshed icon. */
  const iconRules = RULES.filter((r) =>
    splitSelectors(r.selector).some((sel) => /(?:^|[\s>])(?:svg|[^\s>]*\.bh-icon)[^\s>]*$/.test(norm(sel))),
  );

  it("every icon box this file sets is 14, 16 or 20 px", () => {
    expect(iconRules.length).toBeGreaterThan(15);
    const bad = iconRules.flatMap((r) => {
      const d = decls(r.body);
      return ["width", "height"]
        .filter((k) => k in d && !/^(14|16|20)px( !important)?$/.test(d[k]))
        .map((k) => `${r.selector} ${k}: ${d[k]}`);
    });
    expect(bad).toEqual([]);
  });

  it("step list chevrons (agent block 12 x 12, step 11 x 16) snap to 14", () => {
    expect(rule(".bh-step-list .ag-trace svg.agb-chev.bh-icon")).toMatchObject({ width: "14px", height: "14px" });
    expect(rule(".bh-step-list .ag-trace svg.stp-chev.bh-icon")).toMatchObject({ width: "14px", height: "14px", flex: "0 0 14px" });
  });

  it("icon-size overrides for other files' selectors only target refreshed icons (svg.bh-icon)", () => {
    const raw = read("styles/ui-refresh-chrome.css");
    const start = raw.indexOf("═══ 9 · Icon sizes");
    expect(start).toBeGreaterThan(0);
    // Section 9 ends where section 10 (P2 batch 4 navigation) begins.
    const end = raw.indexOf("/* ═══ 10 ·");
    expect(end).toBeGreaterThan(start);
    const section = stripComments(raw.slice(raw.indexOf("*/", start) + 2, end));
    const sectionRules = parseRules(section);
    expect(sectionRules.length).toBeGreaterThanOrEqual(4);
    for (const r of sectionRules) {
      // the last compound is an svg carrying .bh-icon (optionally with other classes)
      for (const sel of splitSelectors(r.selector)) expect(sel, sel).toMatch(/(?:^|[\s>])svg(?:\.[\w-]+)*\.bh-icon(?:\.[\w-]+)*$/);
    }
  });
});
