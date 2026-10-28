/**
 * **Specificity** guard for chart-table-ux batch 1 §2.1 figure-caption attachment.
 *
 * Context (measured on real 116 v1.0.97, not inferred): caption layout rules
 * **never worked for even a day** since batch 1—`.canvas-report-article .rc-fig-caption`
 * has specificity (0,2,0), while the general paragraph rule above in this file
 * `.canvas-report-article p:not(.v2-report-deck)` has (0,2,1), adding precisely the `p`
 * element selector. Measurements: caption/body font-size/color/font-weight identical
 * (14.5px / rgb(63,55,40) / 400); `margin-top:-12px` lost to paragraph `margin`
 * shorthand ⇒ caption 20px from figure, 14px from following text, grouped by proximity with **text below**,
 * exactly the isolated line described by the criterion.
 *
 * Unit tests/typecheck/build all passed; only real-browser computed values exposed it—so enforce
 * **relative specificity** as source fact. jsdom cannot arbitrate cascade or measure computed values,
 * but selector-specificity comparison is deterministic.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The caption and paragraph font sizes live in the interface stylesheet (the base declarations in report-canvas.css
// that it always overrode were deleted); both rules there share the :root:root prefix, so the comparison is unchanged.
const CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "ui-refresh-content.css"),
  "utf8",
);

type Specificity = [number, number, number];

/**
 * Calculate selector (id, class, element) specificity.
 *
 * Cover only forms used here: classes, elements, `:not(...)` (maximum inner specificity per CSS specification).
 * Count pseudo-elements as elements. Sufficient for the comparison below, not a general CSS engine.
 */
function specificity(selector: string): Specificity {
  let rest = selector;
  const acc: Specificity = [0, 0, 0];
  // Count maximum inner :not(...) specificity, not :not itself.
  rest = rest.replace(/:not\(([^)]*)\)/g, (_all, inner: string) => {
    const [i, c, e] = specificity(inner);
    acc[0] += i;
    acc[1] += c;
    acc[2] += e;
    return " ";
  });
  acc[0] += (rest.match(/#[\w-]+/g) ?? []).length;
  acc[1] += (rest.match(/\.[\w-]+/g) ?? []).length;
  acc[1] += (rest.match(/\[[^\]]+\]/g) ?? []).length;
  // Element names: bare identifiers at line start or after whitespace/combinators.
  acc[2] += (rest.match(/(?:^|[\s>+~])([a-zA-Z][\w-]*)/g) ?? []).length;
  acc[2] += (rest.match(/::[\w-]+/g) ?? []).length;
  return acc;
}

function compare(left: Specificity, right: Specificity): number {
  for (let i = 0; i < 3; i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return 0;
}

/** Find selector of the rule declaring `prop` whose selector contains `needle`. */
function selectorDeclaring(needle: string, prop: string): string {
  const stripped = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  for (const match of stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = match[1].trim();
    if (!selector.includes(needle)) continue;
    if (new RegExp(`(^|[;\\s])${prop}\\s*:`).test(match[2])) return selector;
  }
  throw new Error(`没找到声明 ${prop} 且含 ${needle} 的规则——CSS 形态变了就更新本测试`);
}

describe("图注版式必须压过通用段落规则", () => {
  const captionSelector = selectorDeclaring(".rc-fig-caption", "font-size");
  const paragraphSelector = selectorDeclaring(
    ".canvas-report-article p:not(.v2-report-deck)",
    "font-size",
  );

  it("★ 图注选择器的特异性严格大于通用段落选择器", () => {
    const caption = specificity(captionSelector);
    const paragraph = specificity(paragraphSelector);
    expect(compare(caption, paragraph)).toBeGreaterThan(0);
  });

  it("★ 图注规则必须匹配 p 元素本身（decoration 打在 paragraph 节点上）", () => {
    // figure-caption.ts Decoration.node attaches to paragraph ⇒ renders as
    // <p class="rc-fig-caption">. Omitting `p` from selector caused the original cascade loss.
    expect(captionSelector).toMatch(/p\.rc-fig-caption/);
  });

  it("positive control：旧写法确实会输——否则这套特异性算法是死的", () => {
    // Removing the fix must fail. Hardcode old selector as control so the two assertions above are not always true.
    const old = specificity(".canvas-report-article .rc-fig-caption");
    const paragraph = specificity(paragraphSelector);
    expect(old).toEqual([0, 2, 0]);
    expect(paragraph).toEqual([0, 2, 1]);
    expect(compare(old, paragraph)).toBeLessThan(0);
  });
});
