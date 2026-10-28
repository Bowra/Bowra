/**
 * Dark ink-ladder contrast guard: feedback-triage-2026-08-24 batch 5.
 *
 * Separate from `css-tokens.test.ts` because that enforces **literal values**
 * (`--c-accent` must equal `#b04a2a`), changing tests for each palette change; this enforces **properties**
 * (monotonic ladder, AA body text). Hex changes satisfying properties should pass. Mixing criteria
 * suggests color changes require test changes, risking making actual readability regressions green too.
 *
 * Criteria come from device measurements, not design: customer “深色下自己输入的内容看不清”;
 * `--c-ink-4` on `--c-bg` measured 2.19:1 (10.5px signature). Batch 1 changed just
 * that element to ink-3, leaving token imbalance until this batch.
 *
 * ⚠ Use three backgrounds, not one: ink passing `--c-bg` may fail on `--c-surface-2` (inside cards),
 * as old ink-3 did (bg 4.38 / surface-2 3.85); bg-only measurement misses it.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const GLOBALS_CSS = readFileSync(path.resolve(__dirname, "../app/globals.css"), "utf-8");

/**
 * Extract first `[data-theme="dark"] { ... }` block, defining `--c-*` ladder.
 *
 * ⚠ File has **two** `[data-theme="dark"]` blocks: first canonical `--c-*` ladder,
 * second (~350 lines later) `--text-*` / `--color-ice-*` alias system. Wrong block makes
 * every assertion read undefined and fail for missing tokens rather than
 * insufficient contrast, so explicitly assert block contains --c-ink.
 */
function darkBlock(css: string): string {
  const re = /\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/g;
  for (const m of css.matchAll(re)) {
    if (m[1].includes("--c-ink:")) return m[1];
  }
  throw new Error("css-tokens-dark-contrast: 没找到含 --c-ink 的 [data-theme=dark] 块");
}

function readHex(block: string, name: string): string {
  const m = block.match(new RegExp(`${name}\\s*:\\s*(#[0-9a-fA-F]{6})\\s*;`));
  if (!m) throw new Error(`css-tokens-dark-contrast: ${name} 不是字面 hex（或缺失）`);
  return m[1];
}

// ── WCAG 2.1 relative luminance / contrast ────────────────────────────────
function channel(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => channel(parseInt(h.slice(i, i + 2), 16) / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(fg: string, bg: string): number {
  const a = luminance(fg);
  const b = luminance(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

const DARK = darkBlock(GLOBALS_CSS);

/** Same dark-mode text can use these three backgrounds; criterion takes **worst**. */
const BACKDROPS = ["--c-bg", "--c-surface", "--c-surface-2"] as const;

function worstContrast(tokenName: string): { ratio: number; backdrop: string } {
  const fg = readHex(DARK, tokenName);
  let worst = { ratio: Infinity, backdrop: "" };
  for (const b of BACKDROPS) {
    const r = contrast(fg, readHex(DARK, b));
    if (r < worst.ratio) worst = { ratio: r, backdrop: b };
  }
  return worst;
}

describe("深色 ink 阶梯 — 对比度", () => {
  // Three body-text levels. WCAG AA threshold 4.5:1.
  for (const token of ["--c-ink", "--c-ink-2", "--c-ink-3"]) {
    it(`${token} 在三种底上都过 WCAG-AA 正文 4.5:1`, () => {
      const { ratio, backdrop } = worstContrast(token);
      expect(
        ratio,
        `${token}=${readHex(DARK, token)} 在 ${backdrop} 上只有 ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5);
    });
  }

  it("--c-ink-4（弱化标签档）在三种底上都过 AA-large / UI 组件 3.0:1", () => {
    // Design excludes this level from body text (light counterpart 3.66:1), but it carries 10–10.5px metadata,
    // placeholders/table footers; below 3:1, something is visible but unreadable.
    const { ratio, backdrop } = worstContrast("--c-ink-4");
    expect(
      ratio,
      `--c-ink-4=${readHex(DARK, "--c-ink-4")} 在 ${backdrop} 上只有 ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(3.0);
  });

  it("--c-ink-5（装饰档）不承诺正文对比度，但不得低于 2.0:1", () => {
    // Old #3F3C37 on --c-surface-2 was 1.50:1, effectively invisible rather than merely weak.
    const { ratio, backdrop } = worstContrast("--c-ink-5");
    expect(
      ratio,
      `--c-ink-5=${readHex(DARK, "--c-ink-5")} 在 ${backdrop} 上只有 ${ratio.toFixed(2)}:1`,
    ).toBeGreaterThanOrEqual(2.0);
  });

  it("阶梯在 --c-bg 上严格单调递减，且相邻档差 ≥ 1.15×", () => {
    // Prevent brightening multiple middle levels to pass AA until two look identical:
    // tests pass but visual hierarchy vanishes.
    const bg = readHex(DARK, "--c-bg");
    const ladder = ["--c-ink", "--c-ink-2", "--c-ink-3", "--c-ink-4", "--c-ink-5"];
    const ratios = ladder.map((t) => contrast(readHex(DARK, t), bg));
    for (let i = 0; i + 1 < ratios.length; i += 1) {
      expect(
        ratios[i] / ratios[i + 1],
        `${ladder[i]}(${ratios[i].toFixed(2)}) 与 ${ladder[i + 1]}(${ratios[i + 1].toFixed(2)}) 挤在一起`,
      ).toBeGreaterThanOrEqual(1.15);
    }
  });
});

describe("深色 --text-* 别名不得与 ink 阶梯各调各的", () => {
  it("--text-subtle 在深色下引用 --c-ink-4，而不是自带一个 hex", () => {
    // Guard second implementation of same concept: --text-subtle has 49 uses,
    // dark mode previously used --color-ice-500 (#56503f)=2.43:1, same problem and magnitude as --c-ink-4,
    // but separate token system would escape ink-ladder fix.
    const re = /\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/g;
    const blocks = [...GLOBALS_CSS.matchAll(re)].map((m) => m[1]);
    const aliasBlock = blocks.find((b) => b.includes("--text-subtle"));
    expect(aliasBlock, "没找到定义 --text-subtle 的深色块").toBeDefined();
    const m = aliasBlock!.match(/--text-subtle\s*:\s*([^;]+);/);
    expect(m?.[1].trim()).toBe("var(--c-ink-4)");
  });
});

describe("装饰档不得承载正文", () => {
  it("--c-ink-5 不再用于 .code .lang / .tbl-foot / .srow-chev", () => {
    // Three sites: 9.5px language label, 10px footer, clickable chevron. On ink-5,
    // measured dark contrast 1.5–1.7:1. Anchor literal strings, not shifting line numbers.
    // Since 2ea41ac3a, globals.css uses Prettier spacing (`.code .lang {`, space before brace);
    // anchor line start to match standalone rule only, not compounds like `.bh-step-list .ag-trace .code .lang,`.
    for (const selector of [".code .lang {", ".tbl-foot {", ".srow-chev {"]) {
      const idx = GLOBALS_CSS.indexOf(`\n${selector}\n`);
      expect(idx, `选择器 ${selector} 不在 globals.css 里了——判据已失效，请更新本测试`).toBeGreaterThan(-1);
      const rule = GLOBALS_CSS.slice(idx, GLOBALS_CSS.indexOf("}", idx));
      expect(rule, `${selector} 又回到了装饰档 --c-ink-5`).not.toMatch(/color:\s*var\(--c-ink-5\)/);
    }
  });
});
