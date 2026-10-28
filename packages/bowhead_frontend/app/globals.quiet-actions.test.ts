/**
 * CSS-side quiet-by-default invariant (chart-config-ux ⑥, 2026-09-02).
 *
 * Component tests (`table-actions.quiet.test.tsx`) lock only **DOM structure**: switcher outside `⋯`.
 * But switcher **visibility** depends on CSS—moving the fade rule from `.is-more` back onto the entire
 * `.v2-chart-actions.is-quiet` leaves DOM unchanged and component tests green, while the user's chart-kind switcher
 * disappears with `⋯`, precisely what the user rejected. jsdom lacks layout and cannot measure opacity,
 * so lock source facts here.
 *
 * Real-browser evidence (2026-09-02, local dev + Playwright, `/spike/chart-audit`):
 * without hover, switcher opacity=1 and `⋯` opacity=0; hover makes `⋯` 1, affecting only the hovered chart.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "globals.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** Extract bodies of `@media (hover: hover)` blocks by balancing braces. */
function hoverHoverBlocks(): string[] {
  const out: string[] = [];
  const opener = /@media\s*\(\s*hover:\s*hover\s*\)\s*\{/g;
  for (let m = opener.exec(CSS); m !== null; m = opener.exec(CSS)) {
    let depth = 1;
    let i = m.index + m[0].length;
    const start = i;
    while (i < CSS.length && depth > 0) {
      if (CSS[i] === "{") depth += 1;
      else if (CSS[i] === "}") depth -= 1;
      i += 1;
    }
    out.push(CSS.slice(start, i - 1));
  }
  return out;
}

/** Split a block body into `selector { declarations }` pairs. */
function rules(body: string): { selector: string; decls: string }[] {
  return [...body.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1]!.trim(),
    decls: m[2]!,
  }));
}

const QUIET_HIDE = hoverHoverBlocks()
  .flatMap(rules)
  .filter((r) => /is-quiet/.test(r.selector) && /opacity:\s*0\b/.test(r.decls));

describe("默认安静：桌面 hover 淡出规则", () => {
  it("规则存在（否则下面几条断言全是空转）", () => {
    expect(QUIET_HIDE.length).toBeGreaterThan(0);
  });

  it("★★ 淡出的必须是 `⋯` 自己，不能是整条动作条——否则图种切换器跟着一起藏", () => {
    for (const rule of QUIET_HIDE) {
      for (const sel of rule.selector.split(",").map((s) => s.trim()).filter(Boolean)) {
        expect(sel, `选择器「${sel}」把整条动作条藏了`).toMatch(/is-more\s*$/);
      }
    }
  });

  it("★ 图和表两个 header 都要盖到——同一个组件，只盖一边会「图安静、表不安静」", () => {
    const all = QUIET_HIDE.map((r) => r.selector).join(",");
    expect(all).toContain("v2-chart-figure");
    expect(all).toContain("v2-chart-table");
  });

  it("★ 触屏不许淡出：淡出规则只能住在 `@media (hover: hover)` 里", () => {
    // On devices without hover, hover-only visibility means the action is forever unreachable.
    const outside = CSS.replace(/@media\s*\(\s*hover:\s*hover\s*\)\s*\{[\s\S]*?\n\}/g, "");
    const stray = [...outside.matchAll(/([^{}]*is-quiet[^{}]*)\{([^{}]*)\}/g)].filter((m) =>
      /opacity:\s*0\b/.test(m[2]!),
    );
    expect(stray.map((m) => m[1]!.trim())).toEqual([]);
  });

  it("★ 菜单开着时不许淡出——点开了却看不见入口最难受", () => {
    const reveal = hoverHoverBlocks()
      .flatMap(rules)
      .filter((r) => /opacity:\s*1\b/.test(r.decls))
      .map((r) => r.selector)
      .join(",");
    expect(reveal).toContain("is-open");
  });
});
