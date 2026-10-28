/**
 * chart-table-ux C4 — narrow-screen chart actions must not overflow out of reach.
 *
 * Synthetic narrow-viewport case at 412px, before the layout correction:
 *   `.v2-chart-header` 288px / `.v2-chart-actions` 479px / `flex: 0 0 auto`
 *   ⇒ Download CSV/Inspect right edges 462px/545px exceeded header's 354px right edge,
 *      with no horizontal-scroll ancestor ⇒ **only 3 of 5 buttons reachable**.
 * After injecting fix on same page: actions 479→288px, all 5 reachable, buttons on 2 rows,
 * still no page horizontal scroll; 900px (media query matches but room remains) retains 1 row, unaffected.
 *
 * ⚠ **Shrinkability** matters, not flex-wrap: before fix, computed `.v2-chart-actions`
 * already had `flex-wrap: wrap`, but `flex-shrink:0` + `flex-basis:auto` kept
 * used main size equal to max-content (479px), always fitting internally, so wrapping never triggered.
 *
 * jsdom does no layout and cannot measure these values; enforce **source facts** here, especially the part
 * easily reverted by a later casual normalization to flex:0 0 auto.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "globals.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** Export visual-page viewport width, checked per image by `export_browser_visual._validate_receipt`. */
const EXPORT_VIEWPORT_WIDTH = 1600;

type MediaBlock = { maxWidth: number; body: string };

/** Extract all `@media (max-width: Npx)` blocks (balanced braces, tolerates nested rules). */
function maxWidthMediaBlocks(): MediaBlock[] {
  const blocks: MediaBlock[] = [];
  const opener = /@media\s*\(\s*max-width:\s*(\d+)px\s*\)\s*\{/g;
  for (let m = opener.exec(CSS); m !== null; m = opener.exec(CSS)) {
    let depth = 1;
    let i = m.index + m[0].length;
    const start = i;
    while (i < CSS.length && depth > 0) {
      if (CSS[i] === "{") depth += 1;
      else if (CSS[i] === "}") depth -= 1;
      i += 1;
    }
    blocks.push({ maxWidth: Number(m[1]), body: CSS.slice(start, i - 1) });
  }
  return blocks;
}

/** Declarations for `.v2-chart-actions` itself in block, not descendants/buttons. */
function chartActionsDecls(scope: string): string | null {
  const rule = /(^|[},])\s*\.v2-chart-actions\s*\{([^{}]*)\}/.exec(scope);
  return rule ? rule[2] : null;
}

describe("窄屏图表动作条", () => {
  const narrow = maxWidthMediaBlocks()
    .filter((block) => chartActionsDecls(block.body) !== null);

  it("★ 存在一条窄屏规则，让动作条**可收缩**（flex-shrink ≠ 0）且能换行", () => {
    expect(narrow.length).toBeGreaterThan(0);
    const decls = chartActionsDecls(narrow[0].body)!;
    // `flex: 0 1 auto`: middle value is shrink, must be nonzero or flex basis remains
    // max-content and never shrinks, however narrow the container.
    const flex = /(^|[;\s])flex\s*:\s*([^;]+)/.exec(decls);
    expect(flex, `窄屏块里没给 .v2-chart-actions 写 flex: ${decls}`).not.toBeNull();
    const shrink = flex![2].trim().split(/\s+/)[1];
    expect(shrink).toBeDefined();
    expect(Number(shrink)).toBeGreaterThan(0);
    expect(decls).toMatch(/min-width\s*:\s*0/);
    expect(decls).toMatch(/flex-wrap\s*:\s*wrap/);
  });

  it("★ 基线规则仍是不可收缩的 —— 证明窄屏那条确实在改写它（positive control）", () => {
    // Without media queries, `.v2-chart-actions` should return to `flex: 0 0 auto`.
    // If base also becomes shrinkable, assertion above proves nothing (criterion becomes tautological).
    const withoutMedia = CSS.replace(/@media[\s\S]*?\{[\s\S]*?\n\}/g, "");
    const base = chartActionsDecls(withoutMedia);
    expect(base).not.toBeNull();
    expect(base).toMatch(/flex\s*:\s*0\s+0\s+auto/);
  });

  it("★ 窄窗 + 鼠标：图表头按钮保持 28 高——44 的大触控目标只给粗指针（触屏）", () => {
    // Every viewport-only narrow block (no pointer condition) must leave the button height alone; otherwise a
    // 900px desktop window turns the 28px buttons into 44px slabs and ⋯ into 28×44.
    for (const block of maxWidthMediaBlocks()) {
      const rule = /(^|[},])\s*([^{}]*\.v2-chart-action-btn[^{}]*)\{([^{}]*)\}/g;
      for (let m = rule.exec(block.body); m !== null; m = rule.exec(block.body)) {
        expect(m[3], `${m[2].trim()} @ max-width ${block.maxWidth}`).not.toMatch(/min-height\s*:/);
      }
    }
    // Positive control: the touch rule exists and still asks for 44.
    expect(CSS).toMatch(/@media\s*\(max-width:\s*1023px\)\s*and\s*\(pointer:\s*coarse\)\s*\{\s*\.v2-chart-action-btn,[^{}]*\{[^{}]*min-height:\s*44px/);
  });

  it("★ 只在窄屏改 —— 导出视口(1600px)必须命不中，否则要 bump renderer version", () => {
    // Export visual page also renders this action bar (golden PNG has “下载 PNG / 下载 CSV” buttons),
    // so changes matching 1600px alter export pixels and require bumping both ends'
    // RENDERER_VERSION and regenerating goldens. This gate enforces the premise for avoiding a bump.
    for (const block of narrow) {
      expect(block.maxWidth).toBeLessThan(EXPORT_VIEWPORT_WIDTH);
    }
  });
});
