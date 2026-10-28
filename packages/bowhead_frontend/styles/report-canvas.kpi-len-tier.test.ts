/**
 * **Specificity** guard for KPI-value length tiers (feedback-triage-2026-08-24).
 *
 * Context (measured on real 116 v1.1.x, not inferred): fixed 30/32px serif KPI values in cards
 * using `minmax(180px, 1fr)`. Overlong values wrap—`示例品｜23.45亿元` takes 2 lines,
 * `内销销售金额合计 104.71亿元` takes 3, 26 ASCII characters take 4; each consumes all bottom
 * padding (last line ends at padding edge). Cards grow with it (91→125→158,
 * **not** border overflow), but a KPI number wrapping to three/four lines is itself invalid. Reduce font size by display width.
 *
 * This test checks whether **desktop tier rules override mobile**: desktop tier uses
 * `.canvas-report-article .v2-kpi-cell .val[data-len="m"]` = (0,4,0),
 * while mobile media-query baseline is `.canvas-report-article .v2-kpi-cell .val` = (0,3,0).
 * Media queries **add no specificity**, so desktop tiers outrank mobile baseline; without restating inside media,
 * an intended 20px mobile value becomes 27px—worse than no tiers.
 *
 * jsdom cannot arbitrate cascade or measure real computed values; selector specificity and media restatements,
 * however, are deterministically calculable from source.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
// The report KPI value sizes and tiers live in the interface stylesheet (the base declarations in report-canvas.css
// that it always overrode were deleted).
const CANVAS_CSS = readFileSync(join(HERE, "ui-refresh-content.css"), "utf8");
const GLOBALS_CSS = readFileSync(join(HERE, "..", "app", "globals.css"), "utf8");

const TIERS = ["m", "l", "xl"] as const;

/** (id, class, element) specificity. Only forms used here: classes, attributes, elements. */
function specificity(selector: string): [number, number, number] {
  const ids = (selector.match(/#[\w-]+/g) ?? []).length;
  const classes = (selector.match(/\.[\w-]+/g) ?? []).length + (selector.match(/\[[^\]]+\]/g) ?? []).length;
  const elements = (selector.match(/(?:^|[\s>+~])([a-zA-Z][\w-]*)/g) ?? []).length;
  return [ids, classes, elements];
}

function compare(left: number[], right: number[]): number {
  for (let i = 0; i < 3; i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return 0;
}

/** Extract the full `@media (max-width: 640px)` block (brace matching, not greedy regex). */
function mobileBlock(css: string): string {
  const start = css.indexOf("@media (max-width: 640px)");
  if (start < 0) throw new Error("没找到 640px media query —— CSS 形态变了就更新本测试");
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error("640px media query 没有闭合");
}

/** Extract font-size (px at the 16px default root; px or rem literals) from a rule whose selector contains needle. */
function fontSizeOf(css: string, needle: string): number {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const match of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!match[1].includes(needle)) continue;
    const size = match[2].match(/(^|[;\s])font-size\s*:\s*([\d.]+)(px|rem)/);
    if (size) return Number(size[2]) * (size[3] === "rem" ? 16 : 1);
  }
  throw new Error(`没找到含 ${needle} 且声明 font-size 的规则`);
}

describe("KPI 值长度分档", () => {
  it("★ 桌面分档规则比移动端基准更具体——这正是必须在 media 内重申的原因", () => {
    const tier = specificity('.canvas-report-article .v2-kpi-cell .val[data-len="m"]');
    const mobileBase = specificity(".canvas-report-article .v2-kpi-cell .val");
    expect(compare(tier, mobileBase)).toBeGreaterThan(0);
  });

  it("★ 移动端 media query 必须重申全部三档，否则桌面档会穿透进来", () => {
    const mobile = mobileBlock(CANVAS_CSS);
    for (const tier of TIERS) {
      expect(mobile).toContain(`[data-len="${tier}"]`);
    }
  });

  it("★ 移动端每一档都不得大于桌面同档（穿透的症状就是「移动端反而更大」）", () => {
    const mobile = mobileBlock(CANVAS_CSS);
    const mobileBase = fontSizeOf(mobile, ".v2-kpi-cell .val");
    for (const tier of TIERS) {
      const desktopSize = fontSizeOf(CANVAS_CSS, `.v2-kpi-cell .val[data-len="${tier}"]`);
      const mobileSize = fontSizeOf(mobile, `.v2-kpi-cell .val[data-len="${tier}"]`);
      expect(mobileSize).toBeLessThan(desktopSize);
      expect(mobileSize).toBeLessThanOrEqual(mobileBase);
    }
  });

  it("★ 档位必须单调递减——分档写反了比不分档更糟", () => {
    for (const css of [CANVAS_CSS, GLOBALS_CSS]) {
      const sizes = TIERS.map((tier) => fontSizeOf(css, `.v2-kpi-cell .val[data-len="${tier}"]`));
      expect(sizes[0]).toBeGreaterThan(sizes[1]);
      expect(sizes[1]).toBeGreaterThan(sizes[2]);
    }
  });

  it("★ chat 面（globals.css，无 .canvas-report-article 祖先）也要有分档", () => {
    // Canvas rules require a .canvas-report-article ancestor; chat inline KPIs and export render pages
    // lack that class, using only globals. Omitting either side leaves that surface unfixed.
    for (const tier of TIERS) {
      expect(() => fontSizeOf(GLOBALS_CSS, `.v2-kpi-cell .val[data-len="${tier}"]`)).not.toThrow();
    }
  });
});
