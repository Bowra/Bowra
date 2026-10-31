/**
 * Source contract for P2 batch 6 (content type scale, brand appears once): styles/ui-refresh-content.css.
 *
 * jsdom does no cascade, so this pins what the source can prove; pixels, line lengths and contrast
 * identity are verified in real browsers (docs/design/组件统一-技术设计.md "P2 第 6 批结果").
 *   1. Every rule starts with the :root:root prefix; no literal colours or z-index; imported last by the authenticated
 *      layout and never by the public layout, so the export renderer (/internal/canvas-export/*) is untouched.
 *   2. "P2 Icons & Type Spec" content roles: body 14 / 1.7, report title 28 / 700, chapter 20 / 650, subsection
 *      16 / 600, caption 12.5, table 13 with an 11.5 header, code 12.5 mono, KPI 30 / 600; sizes derive from --fs-*.
 *   3. Serif display only switches report and chapter titles; model output gets text-autospace, Help copy does not.
 */
import { describe, expect, it } from "vitest";

import { decls, LITERAL_COLOR, norm, parseRules, PREFIX, read, splitSelectors, stripComments } from "./css-contract";

const CSS = stripComments(read("styles/ui-refresh-content.css"));
const RULES = parseRules(CSS);

/** Merged declarations of the top-level rules whose selector list contains `selector` (after the :root:root prefix). */
function rule(selector: string, media: string | null = null): Record<string, string> {
  const found = RULES.filter(
    (r) =>
      r.media === media &&
      splitSelectors(r.selector).some((full) => norm(full.slice(PREFIX.length)) === norm(selector)),
  );
  expect(found.length, selector).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

/** Token declarations on a root selector. */
function tokens(root: string): Record<string, string> {
  const found = RULES.filter((r) => r.media === null && norm(r.selector) === norm(root));
  expect(found.length, root).toBeGreaterThan(0);
  return Object.assign({}, ...found.map((r) => decls(r.body)));
}

const PROSE = ':is([data-section-kind="narrative"], [data-section-kind="deck"], [data-section-kind="callout"])';

describe("styles/ui-refresh-content.css: every rule starts with the :root:root prefix", () => {
  it("every selector starts with :root:root (inside @media too)", () => {
    expect(RULES.length).toBeGreaterThan(40);
    expect(RULES.flatMap((r) => splitSelectors(r.selector)).filter((sel) => !sel.startsWith(PREFIX))).toEqual([]);
  });

  it("writes no literal colours and no z-index", () => {
    expect(RULES.flatMap((r) => r.body.split(";").filter((x) => LITERAL_COLOR.test(x)))).toEqual([]);
    expect(RULES.filter((r) => "z-index" in decls(r.body)).map((r) => r.selector)).toEqual([]);
  });

  it("keeps the ordered authenticated styles starting with ui-refresh-content.css", () => {
    const imports = Array.from(read("app/(app)/layout.tsx").matchAll(/^import "([^"]+\.css)";$/gm), (m) => m[1]);
    // Only the P2 batch 5 knowledge base sheet (styles/ui-refresh-kb.test.ts) follows it
    const expectedImports = ["../../styles/ui-refresh-content.css", "../../styles/ui-refresh-kb.css"];
    const start = imports.indexOf(expectedImports[0]);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(imports.slice(start, start + expectedImports.length)).toEqual(expectedImports);
    expect(read("app/(public)/layout.tsx")).not.toContain("ui-refresh-content.css");
  });
});

describe("content type scale (P2 Icons & Type Spec)", () => {
  it("derives the content sizes from the --fs-* scale and the measure from the body size", () => {
    expect(tokens(PREFIX)).toMatchObject({
      "--fs-content-body": "var(--fs-base)",
      "--fs-content-title": "calc(var(--fs-base) * 2)",
      "--fs-content-chapter": "var(--fs-xl)",
      "--fs-content-subsection": "var(--fs-lg)",
      "--fs-content-caption": "calc(var(--fs-base) - 0.09375rem)",
      "--fs-content-table": "var(--fs-md)",
      "--fs-content-table-head": "calc(var(--fs-md) - 0.09375rem)",
      "--fs-content-code": "calc(var(--fs-md) - 0.03125rem)",
      "--fs-content-kpi": "calc(var(--fs-base) * 15 / 7)",
      "--lh-content-body": "1.7",
      "--content-measure": "var(--content-measure-cjk)",
      "--content-measure-latin": "calc(var(--fs-content-body) * 35)",
      "--content-measure-cjk": "calc(var(--fs-content-body) * 40)",
      "--font-content-display": "var(--font-v2-sans)",
      "--fw-content-title": "700",
      "--fw-content-chapter": "650",
    });
    expect(tokens(`${PREFIX}[data-typography-profile="readable"]`)).toEqual({ "--lh-content-body": "1.75" });
  });

  it("keys the line-length cap off lang: CJK 40 characters by default, Latin (about 75) in a non-Chinese UI", () => {
    expect(tokens(`${PREFIX}:not(:lang(zh))`)).toEqual({ "--content-measure": "var(--content-measure-latin)" });
    // Content detected as CJK (lang on the answer or report) keeps the CJK measure inside a Latin UI
    expect(rule("[lang]:is(:lang(zh), :lang(ja), :lang(ko))")).toEqual({ "--content-measure": "var(--content-measure-cjk)" });
  });

  it("switches only report and chapter titles to serif in Serif display", () => {
    expect(tokens(`${PREFIX}[data-typography-profile="serif-display"]`)).toEqual({
      "--font-content-display": "var(--font-v2-serif)",
      "--fw-content-title": "600",
      "--fw-content-chapter": "600",
    });
    const displayUsers = RULES.filter((r) => decls(r.body)["font-family"] === "var(--font-content-display)");
    expect(displayUsers.flatMap((r) => splitSelectors(r.selector).map((s) => norm(s.slice(PREFIX.length))))).toEqual([
      ".canvas-report-article .v2-report-h1",
      ".canvas-report-article .v2-report-section h2",
      ".canvas-report-article h2",
      `.canvas-report-article ${PROSE} h1`,
    ]);
  });

  it("sets the report roles", () => {
    expect(rule(".canvas-report-article .v2-report-h1")).toMatchObject({
      "font-size": "var(--fs-content-title)",
      "font-weight": "var(--fw-content-title)",
      "line-height": "1.3",
    });
    expect(rule(".canvas-report-article h2")).toMatchObject({
      "font-size": "var(--fs-content-chapter)",
      "font-weight": "var(--fw-content-chapter)",
      "line-height": "1.4",
    });
    expect(rule(`.canvas-report-article ${PROSE} h3`)).toMatchObject({
      "font-family": "var(--font-v2-sans)",
      "font-size": "var(--fs-content-subsection)",
      "font-weight": "600",
    });
    expect(rule(".canvas-report-article p:not(.v2-report-deck)")).toMatchObject({
      color: "var(--c-ink)",
      "font-size": "var(--fs-content-body)",
      "line-height": "var(--lh-content-body)",
      "text-autospace": "normal",
    });
    expect(rule(`.canvas-report-article ${PROSE} :is(p, ol, ul, blockquote)`)).toEqual({ "max-width": "var(--report-content-measure, calc(var(--content-measure) * 1.5))" });
    expect(rule(".canvas-report-article p.rc-fig-caption:not(.v2-report-deck)")).toMatchObject({
      color: "var(--c-ink-3)",
      "font-size": "var(--fs-content-caption)",
    });
    expect(rule(`.canvas-report-article ${PROSE} pre`)).toMatchObject({
      "font-family": "var(--font-v2-mono)",
      "font-size": "var(--fs-content-code)",
    });
  });

  it("sets KPI values to sans 600 and restates the length tiers it would otherwise outrank", () => {
    expect(rule(".canvas-report-article .v2-kpi-cell .val")).toMatchObject({
      "font-family": "var(--font-v2-sans)",
      "font-size": "var(--fs-content-kpi)",
      "font-style": "normal",
      "font-weight": "600",
      "font-variant-numeric": "tabular-nums lining-nums",
    });
    for (const [len, size] of [["m", "1.5rem"], ["l", "1.125rem"], ["xl", "0.9375rem"]]) {
      expect(rule(`.canvas-report-article .v2-kpi-cell .val[data-len="${len}"]`)["font-size"]).toBe(size);
    }
    expect(rule(".v2-kpi-cell .val .v2-kpi-unit")).toMatchObject({
      color: "var(--c-ink-3)",
      "font-size": "var(--fs-md)",
    });
    for (const [len, size] of [["m", "1rem"], ["l", "0.8125rem"], ["xl", "0.6875rem"]]) {
      expect(rule(`.canvas-report-article .v2-kpi-cell .val[data-len="${len}"]`, "@media (max-width: 640px)")["font-size"]).toBe(size);
    }
  });

  it("sets chat answers to body 14 / 1.7 with answer headings at 16 / 600 and text-autospace", () => {
    expect(rule(".lead-narrative-md > .v2-md")).toEqual({ "font-family": "var(--font-v2-sans)", "text-autospace": "normal" });
    expect(rule(".lead-narrative-md > .v2-md :is(p, ul, ol)")).toMatchObject({
      "max-width": "var(--content-measure)",
      "font-size": "var(--fs-content-body)",
      "line-height": "var(--lh-content-body)",
    });
    expect(rule(".lead-narrative-md > .v2-md :is(h1, h2)")).toMatchObject({
      "font-size": "var(--fs-content-subsection)",
      "font-weight": "600",
    });
  });

  it("sets Help articles without text-autospace (hand-written copy keeps its own spaces)", () => {
    expect(rule(".ui-help-article")).toMatchObject({
      "font-size": "var(--fs-content-body)",
      "line-height": "var(--lh-content-body)",
    });
    expect(rule(".ui-help-article h2")).toMatchObject({
      "font-family": "var(--font-v2-sans)",
      "font-size": "var(--fs-content-chapter)",
      "font-weight": "650",
    });
    expect(rule(".ui-help-article > :is(p, ul, ol, blockquote, h2, h3, h4)")["padding-right"]).toBe(
      "max(0px, calc(min(100%, 880px) - var(--content-measure)))",
    );
    // The lead paragraph obeys the same measure (no override)
    expect(RULES.some((r) => norm(r.selector).endsWith(".ui-help-article > h1 + p"))).toBe(false);
    const autospaced = RULES.filter((r) => "text-autospace" in decls(r.body)).flatMap((r) => splitSelectors(r.selector));
    expect(autospaced.some((s) => s.includes("ui-help"))).toBe(false);
  });
});

describe("brand appears once", () => {
  it("hides the agent pages' brand link", () => {
    expect(rule(".am-topbar > .am-brand")).toEqual({ display: "none" });
    expect(rule(".am-topbar")).toEqual({ "justify-content": "flex-end" });
  });
});
