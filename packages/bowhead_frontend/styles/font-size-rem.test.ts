/**
 * Text sizes follow the user's root text size (WCAG 1.4.4 "Resize text"): the --fs-* type scale and every literal
 * font size are rem relative to the 16px browser default, never px. A px font size stays put when the user raises the
 * browser or system text size (the composer model button stayed 13px at 200%); rem renders the same px at the
 * default root and scales with it. See docs/DESIGN_TOKENS.md "字号".
 *
 * Allowed px: the iOS focus-zoom floor written as an argument of max() (`max(1rem, 16px)`), which keeps touch inputs
 * at least 16px whatever the root size. Line heights are unitless or rem.
 */
import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

import { FRONTEND, parseRules, read, stripComments, tsxFiles } from "./css-contract";

const ENTERPRISE = join(FRONTEND, "../../enterprise/frontend");

function cssUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return ["node_modules", ".next", "out"].includes(name) ? [] : cssUnder(path);
    return name.endsWith(".css") ? [path] : [];
  });
}

const SHEETS = [...["app", "components", "styles", "lib"].flatMap((d) => cssUnder(join(FRONTEND, d))), ...cssUnder(ENTERPRISE)];

/** Type declarations: font-size, the font shorthand, line-height and font-size custom properties. */
const TYPE_DECL = /(?<![\w-])(font-size|font|line-height|--fs-[\w-]+|--[\w-]*-fs|--[\w-]*font-size)\s*:\s*([^;{}]+)/g;

/** px literals left after removing max(...) groups (one level of nested parentheses, e.g. max(16px, var(--fs-lg))). */
function pxOutsideMax(value: string): string[] {
  return value.replace(/max\((?:[^()]|\([^()]*\))*\)/g, "").match(/(?<![\w.-])\d*\.?\d+px\b/g)?.filter((px) => Number.parseFloat(px) !== 0) ?? [];
}

function typeDecls(css: string): { prop: string; value: string }[] {
  return Array.from(stripComments(css).matchAll(TYPE_DECL), (m) => ({ prop: m[1], value: m[2].trim() }));
}

describe("font-size tokens are rem", () => {
  const globals = typeDecls(read("app/globals.css")).filter((d) => /^--fs-/.test(d.prop));

  it("defines the whole --fs-* scale and the dialog / page titles once, in rem", () => {
    const names = new Set(globals.map((d) => d.prop));
    for (const name of ["--fs-2xs", "--fs-xs", "--fs-sm", "--fs-md", "--fs-base", "--fs-lg", "--fs-xl", "--fs-2xl", "--fs-dialog-title", "--fs-page-title"]) {
      expect(names, name).toContain(name);
    }
    // One definition: the personal text size scales the root, not the tokens.
    expect(globals.filter((d) => d.prop === "--fs-md")).toHaveLength(1);
    for (const d of globals) expect(d.value, d.prop).toMatch(/^\d*\.?\d+rem$/);
  });

  it("keeps the default sizes at the 16px root: 10 / 11 / 12 / 13 / 14 / 16 / 20 / 24, dialog 15, page 22", () => {
    const first = new Map<string, string>();
    for (const d of globals) if (!first.has(d.prop)) first.set(d.prop, d.value);
    const px = (name: string) => Number.parseFloat(first.get(name)!) * 16;
    expect(["--fs-2xs", "--fs-xs", "--fs-sm", "--fs-md", "--fs-base", "--fs-lg", "--fs-xl", "--fs-2xl"].map(px)).toEqual([10, 11, 12, 13, 14, 16, 20, 24]);
    expect([px("--fs-dialog-title"), px("--fs-page-title")]).toEqual([15, 22]);
  });

  it("derives the content tokens from --fs-* with rem offsets only", () => {
    const content = typeDecls(read("styles/ui-refresh-content.css")).filter((d) => d.prop.startsWith("--fs-content-"));
    expect(content.length).toBeGreaterThanOrEqual(9);
    for (const d of content) {
      expect(d.value, d.prop).toMatch(/var\(--fs-/);
      expect(pxOutsideMax(d.value), d.prop).toEqual([]);
    }
  });
});

describe("the personal text size scales the root font size", () => {
  const globalsRules = parseRules(stripComments(read("app/globals.css")));
  const scaleRules = globalsRules.filter((r) => r.selector.includes("data-type-scale"));
  const settingRules = scaleRules.filter((r) => /^:root\[data-type-scale="[\w-]+"\]$/.test(r.selector));
  const applyRules = scaleRules.filter((r) => !settingRules.includes(r));
  /** `--root-text-scale: N / D` -> N / D. */
  const factor = (body: string) => {
    const m = body.match(/--root-text-scale:\s*(\d+) \/ (\d+)\s*(;|$)/);
    return m ? Number(m[1]) / Number(m[2]) : null;
  };
  const declNames = (body: string) => Array.from(body.matchAll(/(?<![\w-])(--?[\w-]+|[a-z-]+)\s*:/g), (m) => m[1]);

  it("gives each setting a root factor so --fs-base is 13 / 15 / 16px at the 16px default", () => {
    expect(settingRules.map((r) => [r.selector, r.media])).toEqual([
      [':root[data-type-scale="compact"]', null],
      [':root[data-type-scale="large"]', null],
      [':root[data-type-scale="extra-large"]', null],
    ]);
    const basePx = settingRules.map((r) => Math.round(0.875 * 16 * factor(r.body)! * 1e9) / 1e9);
    expect(basePx).toEqual([13, 15, 16]);
  });

  it("applies the factor as a percentage of the browser default on the root, for exactly those three settings", () => {
    expect(applyRules).toHaveLength(1);
    const [apply] = applyRules;
    expect(apply.media).toBeNull();
    expect(apply.selector).toBe(':root:is([data-type-scale="compact"], [data-type-scale="large"], [data-type-scale="extra-large"])');
    expect(apply.body.trim()).toBe("font-size: calc(100% * var(--root-text-scale));");
  });

  it("leaves the standard setting on the browser root: no rule sets the root font size otherwise", () => {
    const rootRules = globalsRules.filter((r) => r.selector.split(",").some((s) => /^(html|:root)$/.test(s.trim())));
    expect(rootRules.length).toBeGreaterThan(0);
    for (const r of rootRules) expect(r.body, r.selector).not.toMatch(/(?<![\w-])font-size\s*:/);
    expect(scaleRules.some((r) => r.selector.includes('"standard"'))).toBe(false);
  });

  it("does not also remap the size tokens, so nothing scales twice", () => {
    for (const r of scaleRules) {
      const names = declNames(r.body);
      // Only the root size, its factor and the unitless extra-large line heights.
      for (const name of names) expect(["font-size", "--root-text-scale", "--lh-normal", "--lh-relaxed"], `${r.selector} ${name}`).toContain(name);
    }
    const elsewhere = SHEETS.filter((f) => !f.endsWith("app/globals.css") && read(relative(FRONTEND, f)).includes("data-type-scale"));
    expect(elsewhere).toEqual([]);
  });

  it("is applied only by the authenticated layout; public pages and the canvas export never get the setting", () => {
    expect(read("app/(app)/layout.tsx")).toContain("r.dataset.typeScale=");
    for (const rel of ["app/(public)/layout.tsx", "lib/public-color-scheme-bootstrap-script.ts", "components/brand-provider.tsx"]) {
      expect(read(rel), rel).not.toMatch(/typeScale|type-scale/);
    }
  });
});

describe("no px font sizes in the stylesheets", () => {
  it("scans the app, component, Business and edition stylesheets", () => {
    expect(SHEETS.length).toBeGreaterThan(40);
    expect(SHEETS.some((f) => f.endsWith("ui-controls-legacy.css"))).toBe(true);
    expect(SHEETS.some((f) => f.includes("enterprise"))).toBe(true);
  });

  it("writes every font-size, font, line-height and font-size custom property in rem (px only as a max() floor)", () => {
    const offenders = SHEETS.flatMap((file) =>
      typeDecls(read(relative(FRONTEND, file)))
        .filter((d) => pxOutsideMax(d.value).length)
        .map((d) => `${relative(FRONTEND, file)}: ${d.prop}: ${d.value}`),
    );
    expect(offenders).toEqual([]);
  });

  it("keeps the touch-input floors at max(1rem, 16px) so iOS does not zoom on focus", () => {
    expect(read("styles/ui-refresh.css")).toMatch(/\.ui-dialog-sheet\s*:is\(input[^{]*\{\s*font-size: max\(1rem, 16px\);/);
    expect(read("styles/composer-layout-v2.css")).toContain("--composer-input-font-size: max(1rem, 16px)");
    expect(read("styles/public-pages.css")).toContain("--pub-control-fs: max(1rem, 16px)");
    expect(read("styles/models-settings.css")).toContain(".mset :is(input,textarea,select){font-size:max(1rem, 16px)}");
  });

  it("the scanner catches px font sizes, shorthands and line heights", () => {
    expect(typeDecls(".a{font-size:13px}.b{font:600 12px/1.4 var(--f)}.c{line-height:18px}.d{--fs-x:11px}").filter((d) => pxOutsideMax(d.value).length)).toHaveLength(4);
    expect(typeDecls(".a{font-size:max(1rem, 16px)}.b{font:600 0.75rem/1.4 var(--f)}.c{font-family:x;letter-spacing:1px}").filter((d) => pxOutsideMax(d.value).length)).toHaveLength(0);
  });
});

describe("no px font sizes in inline styles", () => {
  /** `fontSize:` expressions with a bare numeric literal (React appends px); arithmetic on a box size is allowed. */
  const NUMERIC_FONT_SIZE = /fontSize:\s*([^,}\n]+?)(?=\s*[,}\n])/g;
  const offenders = (text: string) =>
    Array.from(text.matchAll(NUMERIC_FONT_SIZE), (m) => m[1]).filter(
      (expr) => !/[*/+]|Math\./.test(expr) && /(^|[\s?:|(])\d+(\.\d+)?(?=$|[\s:)])/.test(expr),
    );

  it("uses rem strings, tokens or box-relative arithmetic for fontSize", () => {
    const files = [...["app", "components", "lib"].flatMap((d) => tsxFiles(join(FRONTEND, d))), ...tsxFiles(ENTERPRISE)];
    expect(files.length).toBeGreaterThan(200);
    const found = files.flatMap((f) => offenders(read(relative(FRONTEND, f))).map((expr) => `${relative(FRONTEND, f)}: fontSize: ${expr}`));
    expect(found).toEqual([]);
  });

  it("the inline scanner catches literal and conditional px sizes", () => {
    expect(offenders('style={{ fontSize: 13, a: 1 }} x={{ fontSize: big ? 12 : 11.5 }} y={{ fontSize: size || 11 }}')).toHaveLength(3);
    expect(offenders('style={{ fontSize: "0.8125rem" }} x={{ fontSize: size * 0.5 }} y={{ fontSize: "var(--fs-sm)" }}')).toHaveLength(0);
  });
});

describe("small text boxes grow with the root text size", () => {
  /** Body of the first rule whose (whitespace-normalised) text contains `${needle} {`. */
  const ruleBody = (rel: string, needle: string) => {
    const css = stripComments(read(rel)).replace(/\s+/g, " ");
    const at = css.indexOf(`${needle} {`);
    expect(at, `${rel}: ${needle}`).toBeGreaterThan(-1);
    return css.slice(at, css.indexOf("}", at));
  };

  it("form fields and grid headers keep their default height and grow with the text (em floor, WebKit selects ignore min-height)", () => {
    const body = ruleBody("styles/ui-refresh.css", ".memory-v2-filters select )");
    expect(body).toContain("height: max(var(--control-h-md), 1.6em);");
    expect(body).toContain("min-height: var(--control-h-md);");
    expect(ruleBody("app/globals.css", ".f-tools .sel")).toContain("height: max(26px, 1.6em);");
    expect(ruleBody("styles/ui-refresh.css", ":root:root .f-head")).toContain("height: max(var(--table-head-h), 2.4em);");
    expect(ruleBody("styles/ui-refresh-pages-admin.css", ":root:root .legacy-turns-th")).toContain("height: max(var(--table-head-h), 2.4em);");
  });

  it("badges, counters and list rows use min-height so 200% text stays inside", () => {
    const badge = ruleBody("styles/ui-refresh.css", ".kb-otpl-mybadge )");
    expect(badge).toContain("height: auto;");
    expect(badge).toContain("min-height: var(--ui-badge-min-h, var(--ui-badge-h, 20px));");
    expect(ruleBody("styles/ui-refresh-surfaces.css", ".legacy-preset-pill )")).toContain("min-height: var(--ui-badge-h, 20px);");
    expect(ruleBody("styles/ui-refresh-chrome.css", ":root:root .notification-bell__badge")).toContain("min-height: 16px;");
    expect(ruleBody("styles/ui-refresh-chrome.css", ":root:root .unified-work-meta")).toContain("min-height: 16px;");
    expect(ruleBody("styles/ui-refresh-chrome.css", ":root:root .unified-sidebar-user")).toContain("min-height: 52px;");
    expect(ruleBody("styles/ui-refresh-surfaces.css", ":root:root .legacy-share-type-tag")).toContain("min-height: 18px;");
    expect(ruleBody("styles/ui-refresh-surfaces.css", ":root:root .mx-cbadge .num")).toContain("min-height: 18px;");
    expect(ruleBody("app/globals.css", ".notification-tab span")).toContain("min-height: 16px;");
    // Extra-large text size on a 200% browser root (a 36.6px root): these boxes keep their default size and grow with the text.
    expect(read("styles/kb-management.css")).toContain(".kb-ftype { width: max(24px, 2.6em); height: max(24px, 2.6em);");
    expect(read("styles/kb-management.css")).toContain(".mrow .kb-ftype { width: max(28px, 2.6em); height: max(28px, 2.6em);");
    expect(ruleBody("styles/ui-controls-legacy.css", ".kb-ftype.kb-bundle-ftype")).toContain("height: max(20px, 2.6em);");
    expect(ruleBody("styles/ui-refresh-chrome.css", ":root:root .ui-credit-badge")).toContain("height: max(28px, 1.6em);");
    expect(ruleBody("app/globals.css", ".v2-topbar-locale")).toContain("height: max(var(--theme-icon-btn, 30px), 1.6em);");
    expect(read("components/knowledge/matrix/matrix.css")).toContain("height: max(var(--control-h-md, 28px), 1.6em);");
    expect(ruleBody("styles/ui-refresh.css", ":root:root .kb-upload .drop")).toContain("flex-shrink: 0;");
    expect(read("styles/agent-marketplace.css")).toContain(".am-capability-row { min-height: 52px; flex-shrink: 0;");
    // The source number must stay exactly 16px at the default root (its content is 16.5px tall), so it scales in rem.
    expect(ruleBody("styles/report-canvas.css", ".rc-source .src-n")).toContain("height: 1rem;");
  });
});
