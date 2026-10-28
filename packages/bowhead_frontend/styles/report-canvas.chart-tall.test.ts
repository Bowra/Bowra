/**
 * chart-table-ux — hbar height-by-category works end to end only when **both rules hold**.
 *
 * Synthetic chart with 20 categories and `--chart-h: 740px`:
 * Before fix: wrap 342 / inner 310 / canvas **260**, about 13px per category, making brand labels unreadable.
 * Two independent problems:
 *   A. `--chart-h` height applied only inside `@media (max-width: 640px)` ⇒ desktop wrap always used
 *      baseline `clamp(300px, 38vh, 420px)`.
 *   B. `.v2-insight-card .v2-evidence-grid--* canvas { max-height: 260px !important }`
 *      ⇒ even with wrap at 740, Chart.js `getMaximumSize()` read canvas's own
 *      `max-height`, clamping back to 260. (**Not** mount timing: remove cap, trigger
 *      one re-measure without remount; same canvas immediately 260 → 708.)
 * After both fixes on same page: #0 740/708/**708**, #1 570/538/**538**,
 * #2 without `--chart-h` remained 342/310/**260** (zero-difference control); all three canvas widths unchanged.
 *
 * jsdom cannot lay out or measure these values; enforce **source facts** here, the two places most easily
 * reverted by moving rule back into mobile block or unifying a hardcoded cap.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const stripComments = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, "");

const REPORT_CANVAS_CSS = stripComments(readFileSync(join(HERE, "report-canvas.css"), "utf8"));
const GLOBALS_CSS = stripComments(readFileSync(join(HERE, "..", "app", "globals.css"), "utf8"));

/** Remove all `@media (max-width: …)` blocks, leaving rules applying at any width. */
function withoutMaxWidthMedia(css: string): string {
  let out = "";
  const opener = /@media\s*\([^)]*max-width[^)]*\)\s*\{/g;
  let last = 0;
  for (let m = opener.exec(css); m !== null; m = opener.exec(css)) {
    out += css.slice(last, m.index);
    let depth = 1;
    let i = m.index + m[0].length;
    while (i < css.length && depth > 0) {
      if (css[i] === "{") depth += 1;
      else if (css[i] === "}") depth -= 1;
      i += 1;
    }
    last = i;
    opener.lastIndex = i;
  }
  return out + css.slice(last);
}

/** Declaration body of `--chart-h` wrap rule in given CSS fragment. */
function tallWrapDecls(css: string): string | null {
  const rule = /\.v2-chart-canvas-wrap\[style\*=["']--chart-h["']\]\s*\{([^{}]*)\}/.exec(css);
  return rule ? rule[1] : null;
}

/** Declaration body of `.is-tall` height rule. */
function isTallDecls(css: string): string | null {
  const rule = /\.v2-chart-canvas-wrap\.is-tall\s*\{([^{}]*)\}/.exec(css);
  return rule ? rule[1] : null;
}

describe("hbar 长高", () => {
  it("A: 长高的高度模型在 media query 之外，桌面也吃得到", () => {
    // `.is-tall` owns height (globals.css): height:auto + max-height cap + internal scrolling.
    const desktop = withoutMaxWidthMedia(GLOBALS_CSS);
    const decls = isTallDecls(desktop);
    expect(decls, "`.is-tall` 高度规则不见了或被关进 max-width media query：桌面 hbar 会塌回 342px").not.toBeNull();
    const flat = decls!.replace(/\s/g, "");
    expect(flat).toContain("height:auto");
    expect(flat).toMatch(/max-height:\d+vh/);
    expect(flat).toContain("overflow-y:auto");
    // Actual height belongs to inner.
    const inner = /\.v2-chart-canvas-wrap\.is-tall\s*>\s*\.v2-chart-canvas-inner\s*\{([^{}]*)\}/.exec(desktop);
    expect(inner, "`.is-tall > .v2-chart-canvas-inner` 的高度规则没了").not.toBeNull();
    expect(inner![1]!.replace(/\s/g, "")).toContain("height:var(--chart-h)");
  });

  it("★★ A2: 报告面那条 `--chart-h` 规则不许再钉 height —— 会把 x 轴刻度裁掉", () => {
    // 116 real-device measurement 2026-09-02: `.canvas-report-article .v2-chart-canvas-wrap[style*="--chart-h"]`
    // has specificity (0,3,0), above `.v2-chart-canvas-wrap.is-tall` (0,2,0); setting height there
    // pins wrap to `--chart-h`, while inner also uses `--chart-h` ⇒ wrap's 32px padding
    // always overflows, clipping x-axis ticks (0 / 2亿 / 4亿 …) outside viewport and adding a spurious scrollbar.
    // Injected `height:auto` retest: overflow 32px → 0px, ticks immediately return.
    const decls = tallWrapDecls(REPORT_CANVAS_CSS);
    expect(decls, "这条规则本身没了？那这条断言是空转").not.toBeNull();
    expect(decls!.replace(/\s/g, ""), "报告面又把 height 钉回 --chart-h 了").not.toMatch(/(^|;)height:/);
  });

  it("B: insight_card 证据图的 max-height 上限让位给 --chart-h", () => {
    const caps = [...GLOBALS_CSS.matchAll(
      /\.v2-insight-card\s+\.v2-evidence-grid--(\w+)\s+canvas\s*\{([^{}]*)\}/g,
    )];
    // All three layouts (single_full / grid_2col / stack_full) require cap rule.
    expect(caps.map((m) => m[1]).sort()).toEqual(["grid_2col", "single_full", "stack_full"]);

    for (const [, layout, body] of caps) {
      const flat = body.replace(/\s/g, "");
      expect(flat, `${layout} 的上限写死了，--chart-h 再大也会被 Chart.js clamp 回去`)
        .toMatch(/max-height:var\(--chart-h,\d+px\)/);
    }
  });
});

describe("窄宽图表摘要", () => {
  it("跨报告、聊天和设置预览生效，并用分隔线保持统计项可扫读", () => {
    expect(REPORT_CANVAS_CSS).toMatch(/(?:^|})\s*\.v2-chart-summary\s*\{/m);
    expect(REPORT_CANVAS_CSS).not.toMatch(/\.canvas-report-article\s+\.v2-chart-summary\s*\{/);
    expect(REPORT_CANVAS_CSS).toMatch(
      /\.v2-chart-summary\s*>\s*span\s*\+\s*span\s*\{[^}]*border-left/s,
    );
  });
});
