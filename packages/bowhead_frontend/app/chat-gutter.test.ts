/**
 * chat-ux-2026-08 batch F continuation—continuous left-pane gutter contract.
 *
 * Context (every item measured in real browsers, not inferred):
 * Design §3.2 proposed three gutter tiers (≥720→40 / 480–720→28 / <480→12) and
 * `DEFAULT_SPLIT_RATIO` 0.4→0.46 as **one package**. Batch E implemented only the tiers,
 * leaving the halves mismatched:
 *   - Default pane at 1440 viewport is **464px**, landing in `<480` and using the
 *     **12px emergency value** intended for dragging to minimum 360px—body hugs the edge.
 *   - Adding the ratio change **does not fix the root cause**: at 1280, 0.40=400 / 0.46=460,
 *     both <480 and use the emergency tier; above 1536, 0.40 already >480 and needs no change.
 *     Ratio helps only in a small range near 1440.
 *
 * Continuous values eliminate calibration entirely (moving a cliff still leaves a cliff).
 * jsdom does not run container queries or measure cqi, so enforce **source facts**—precisely the part most easily
 * reverted casually to three tiers. Actual measurements live in the CSS-comment table.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "globals.css"), "utf8");

/** Every declaration assigning --chat-gutter. */
function gutterAssignments(): string[] {
  const stripped = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  return Array.from(stripped.matchAll(/--chat-gutter\s*:\s*([^;}]+)/g)).map((m) => m[1].trim());
}

describe("左栏 gutter — 连续值，不是硬断点", () => {
  it("★ 只有**一条**赋值，且是 clamp（三档硬断点会有三条）", () => {
    const assignments = gutterAssignments();
    expect(assignments).toHaveLength(1);
    expect(assignments[0]).toMatch(/^clamp\(/);
  });

  it("★ 中间项用 cqi 而不是 vw —— 判据必须是**栏宽**", () => {
    // Dragging changes pane width while viewport stays fixed; vw would make dragging ineffective.
    // ⚠ Do **not** put `\b` before the unit: in `4.8cqi`, `8`→`c` are both word characters, with no boundary;
    // `\bcqi\b` never matches—creating a false test that always fails (or passes).
    expect(gutterAssignments()[0]).toMatch(/\d\s*cqi(?![A-Za-z])/);
    expect(gutterAssignments()[0]).not.toMatch(/\d\s*vw(?![A-Za-z])/);
  });

  it("★ 下界 12px：拖到最窄(360px 栏，= MIN_CHAT_PX)时仍留得住内容", () => {
    expect(gutterAssignments()[0]).toMatch(/clamp\(\s*12px/);
  });

  it("★ 上界 40px：宽屏观感与改前的宽档一致，不因连续化而变松", () => {
    expect(gutterAssignments()[0]).toMatch(/40px\s*\)/);
  });

  it("★ var() 的兜底仍是 40px —— 不支持容器查询的浏览器要落到最宽松一档", () => {
    // Unsupported cqi → entire declaration invalid → var() fallback.
    // Shrinking that fallback would suddenly crowd those browsers (batch E comment explains the chosen value).
    expect(CSS).toMatch(/var\(--chat-gutter,\s*40px\)/);
  });
});
