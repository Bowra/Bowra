/**
 * Apply the system Reduce Motion preference globally (UI redesign P0-3, docs/design/界面改版-Apple设计方向.md).
 *
 * The app's reduced-motion setting globally stops animations via `:root[data-theme-motion="reduced"]`, but defaults to
 * Standard and does not read the system preference; the previous 36 `prefers-reduced-motion` queries only covered individual components.
 * jsdom does not perform matchMedia layout calculations, so lock the source facts here: a **top-level**
 * `@media (prefers-reduced-motion: reduce)` block zeroes animations and transitions for `*` and pseudo-elements.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "globals.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function reducedMotionBlocks(): string[] {
  const out: string[] = [];
  const opener = /@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{/g;
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

describe("globals.css 跟随系统减少动态效果", () => {
  it("有一个对全部元素清零动画与过渡的全局块", () => {
    const global = reducedMotionBlocks().find((body) =>
      /(^|[\s,}])\*\s*,\s*\*::before\s*,\s*\*::after\s*\{/.test(body),
    );
    expect(global).toBeDefined();
    expect(global).toMatch(/animation-duration:\s*0\.001ms\s*!important/);
    expect(global).toMatch(/transition-duration:\s*0\.001ms\s*!important/);
    expect(global).toMatch(/:root\s*\{\s*--ease-v2:\s*linear/);
  });
});
