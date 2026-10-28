/**
 * Global keyboard focus ring (UI redesign P0-2, docs/design/界面改版-Apple设计方向.md).
 *
 * Previously, no global `:focus-visible` rule existed; 261 component-specific definitions scattered focus styles. Components without one fell back to the browser's
 * default ring, whose color ignored the theme. Wrap the global rule in `:where()` to keep specificity zero, otherwise it overrides components'
 * own focus styles (input focus-within outlines, menu-item backgrounds, etc.). jsdom does not compute the :focus-visible
 * cascade, so lock the source facts here.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "globals.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

function ruleBody(selectorPattern: RegExp): string | undefined {
  const m = selectorPattern.exec(CSS);
  if (!m) return undefined;
  const start = m.index + m[0].length;
  return CSS.slice(start, CSS.indexOf("}", start));
}

describe("globals.css 全局键盘焦点环", () => {
  it("零优先级的全局 :focus-visible 规则使用主题焦点色", () => {
    const body = ruleBody(
      /:where\(\s*a\s*,\s*button\s*,\s*input\s*,\s*select\s*,\s*textarea\s*,\s*summary\s*,\s*\[tabindex\]\s*\):focus-visible\s*\{/,
    );
    expect(body).toBeDefined();
    expect(body).toMatch(/outline:\s*2px solid var\(--c-focus,\s*var\(--c-accent\)\)/);
  });

  it("报告库搜索框去掉输入框描边后由外框显示焦点", () => {
    expect(ruleBody(/\.reports-search input\s*\{/)).toMatch(/outline:\s*0/);
    expect(ruleBody(/\.reports-search:focus-within\s*\{/)).toMatch(
      /border-color:\s*var\(--c-accent\)/,
    );
  });
});
