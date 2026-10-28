/**
 * Source contract for the chat card shell (Chat Cards Spec, D1): styles/ui-refresh-cards.css.
 *
 * jsdom does no cascade, so this pins what the source can prove; pixels are compared with the spec in a real browser.
 */
import { describe, expect, it } from "vitest";

import { decls, LITERAL_COLOR, norm, parseRules, PREFIX, read, splitSelectors, stripComments } from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-cards.css"));
const RULES = parseRules(CSS);

function rule(sel: string, media: RegExp | null = null): Record<string, string> {
  const found = RULES.filter((r) => {
    if (media ? !media.test(r.media ?? "") : r.media !== null) return false;
    return splitSelectors(r.selector).some((full) => norm(full.slice(PREFIX.length)) === norm(sel));
  });
  expect(found.length, sel).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

describe("styles/ui-refresh-cards.css", () => {
  it("prefixes every rule with :root:root and writes no literal colours", () => {
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
    const literal = RULES.flatMap((r) => r.body.split(";").map((x) => x.trim()).filter((x) => LITERAL_COLOR.test(x)));
    expect(literal).toEqual([]);
  });

  it("defines the spec's card tokens, with dark-theme intent colours", () => {
    const tokens = rule("");
    for (const name of [
      "--card-radius", "--card-edge", "--card-edge-input", "--card-pad", "--card-pad-row", "--card-row-min-h",
      "--card-icon", "--card-icon-gap", "--card-gap", "--card-foot-gap", "--card-eyebrow", "--card-title",
      "--card-body", "--card-meta", "--card-intent-input", "--card-intent-info", "--card-intent-success",
      "--card-intent-failure", "--card-intent-progress",
    ]) {
      expect(tokens[name], name).toBeTruthy();
    }
    expect(tokens["--card-radius"]).toBe("var(--r-4)");
    expect(tokens["--card-edge-input"]).toBe("1px solid color-mix(in srgb, var(--c-warn) 45%, transparent)");
    expect(rule('[data-theme="dark"]')).toMatchObject({
      "--card-intent-input": "var(--c-warn)",
      "--card-intent-success": "var(--c-pos)",
      "--card-intent-failure": "var(--c-err)",
    });
  });

  it("draws one shell: hairline edge, r4, no shadow; only the input intent swaps the edge", () => {
    expect(rule(".ui-card")).toMatchObject({
      border: "var(--card-edge)",
      "border-radius": "var(--card-radius)",
      "box-shadow": "none",
      background: "var(--c-surface)",
      "max-width": "640px",
    });
    expect(rule(".ui-card--input")).toMatchObject({ border: "var(--card-edge-input)" });
    for (const intent of ["success", "failure", "progress"]) {
      expect(rule(`.ui-card--${intent}`).border, intent).toBeUndefined();
    }
  });

  it("keeps rows at 40 and text at 12 or larger", () => {
    expect(rule(".ui-card--row")).toMatchObject({ "min-height": "var(--card-row-min-h)" });
    const sizes = [...CSS.matchAll(/font(?:-size)?:\s*[^;]*?(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
    expect(sizes.filter((px) => px < 12)).toEqual([]);
  });

  it("gives keyboard focus a 2px ring that falls back to the accent", () => {
    expect(CSS.match(/var\(--c-focus\)/g)).toBeNull();
    expect(rule("button.ui-card:focus-visible")).toMatchObject({ outline: "2px solid var(--c-focus, var(--c-accent))" });
    expect(rule(".ui-card--activatable:has(.ui-card__activate:focus-visible)")).toMatchObject({
      outline: "2px solid var(--c-focus, var(--c-accent))",
    });
  });

  it("replaces the spinner with a static dot under reduced motion", () => {
    expect(rule(".ui-card__spinner", /prefers-reduced-motion: reduce/)).toMatchObject({ animation: "none" });
  });

  it("file card: 36 tile, 28 filled download button (touch 44 through --control-h-md), done is positive", () => {
    expect(rule(".ui-card__media")).toMatchObject({ width: "36px", height: "36px", background: "var(--c-fill-2)" });
    expect(rule(".ui-card-dl")).toMatchObject({
      width: "var(--control-h-md)",
      height: "var(--control-h-md)",
      "border-radius": "var(--r-2)",
      background: "var(--c-fill-2)",
    });
    expect(rule('.ui-card-dl[data-state="done"]')).toMatchObject({ background: "var(--c-pos-soft)" });
  });

  it("attachment chip: 32 high, max 300, name ellipsised first, meta never wraps, 28 inner buttons, 44 on touch", () => {
    expect(rule(".v2-chip")).toMatchObject({ height: "32px", "max-width": "300px", "border-radius": "var(--r-2)", background: "var(--c-fill-2)" });
    expect(rule(".v2-chip__name")).toMatchObject({ "text-overflow": "ellipsis", "white-space": "nowrap", "min-width": "0" });
    expect(rule(".v2-chip__meta")).toMatchObject({ flex: "none", "white-space": "nowrap" });
    expect(rule(":is(.v2-chip__x, .v2-chip__chev, button.v2-chip__dl)")).toMatchObject({ width: "var(--control-h-md)", height: "var(--control-h-md)" });
    expect(rule(".v2-chip", /pointer: coarse/)).toMatchObject({ height: "var(--control-h-touch)" });
  });

  it("is imported last by the authenticated layout and stays out of the public layout", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    expect(imports.at(-1)).toBe("../../styles/ui-refresh-cards.css");
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-cards.css");
  });
});
