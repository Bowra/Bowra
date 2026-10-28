/**
 * css-tokens — base palette verification.
 *
 * Asserts that the `app/globals.css` base blocks resolve the product default
 * palette (SaaS Blue since 1.5.0, compiled by the backend theme compiler).
 *
 * Approach: parse `globals.css` from filesystem via regex (vitest+jsdom does
 * NOT auto-apply CSS from imported `.css` files — `getComputedStyle` on a
 * jsdom-rendered div would return empty strings for CSS custom properties).
 * Filesystem parsing is deterministic and grep-stable.
 *
 * Scope:
 *  - the light `:root { ... }` base block and the dark base block.
 *  - DOES NOT touch component code; consumers auto-resolve via `var(--c-*)`.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

// Read globals.css once at module load — file is small (~7600 lines) and we
// only need the :root block. The light-mode block is between `:root {` and
// the next `}` (before `[data-theme="dark"]`).
const GLOBALS_CSS_PATH = path.resolve(__dirname, "../app/globals.css");
const GLOBALS_CSS = readFileSync(GLOBALS_CSS_PATH, "utf-8");

/**
 * Extract the light-mode `:root { ... }` block (excluding `[data-theme=...]`).
 * Returns the inner content string for regex token extraction.
 */
function extractRootBlock(css: string): string {
  // Match `:root {` followed by content up to the closing `}` BEFORE
  // `[data-theme=`. Lazy match across newlines via `[\s\S]`.
  const match = css.match(/:root\s*\{([\s\S]*?)\n\}/);
  if (!match) {
    throw new Error("css-tokens.test: could not locate :root { ... } block");
  }
  return match[1];
}

/**
 * Extract a CSS custom property value from a block of CSS text.
 * Returns the trimmed value (without trailing `;`), or undefined if missing.
 */
function readToken(block: string, name: string): string | undefined {
  // `--c-accent: #b04a2a;` — capture value between colon and semicolon, allow
  // inline `/* comment */` after the value (e.g. `#a09786;              /* ... */`).
  const re = new RegExp(`${name.replace(/-/g, "\\-")}\\s*:\\s*([^;]+);`);
  const m = block.match(re);
  return m ? m[1].trim() : undefined;
}

/**
 * C3 global appearance migration (ca1f324c9) makes legacy names one-hop aliases: `--c-cool: var(--c-info)`.
 * Enforce both alias-contract halves separately: ① target canonical token (`readToken` reads source);
 * ② final color resolved within the same block (this function, one hop only; multiple hops violate the bridge and return undefined).
 */
function resolveAlias(block: string, name: string): string | undefined {
  const raw = readToken(block, name);
  const alias = raw?.match(/^var\((--[a-z0-9-]+)\)$/);
  if (!alias) return raw;
  const target = readToken(block, alias[1]);
  return target && !target.startsWith("var(") ? target : undefined;
}

describe("globals.css base — product default palette (SaaS Blue, 1.5.0)", () => {
  // Literal copies of theme_compiler.product_default_compiled_scheme(); the backend test
  // test_theme_product_default_palette.py enforces all 24 tokens against the compiler.
  const root = extractRootBlock(GLOBALS_CSS);
  const dark = GLOBALS_CSS.match(/\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/)![1];

  describe("light neutrals", () => {
    it.each([
      ["--c-bg", "#f4f7fb"],
      ["--c-surface", "#ffffff"],
      ["--c-surface-2", "#e5efff"],
      ["--c-surface-3", "#d5e6ff"],
      ["--c-line", "#77879f"],
      ["--c-line-2", "#a8b9d1"],
      ["--c-ink", "#061223"],
      ["--c-ink-2", "#2b394d"],
      ["--c-ink-3", "#4f5f74"],
      ["--c-ink-4", "#63728a"],
    ])("%s resolves to %s", (name, value) => {
      expect(readToken(root, name)).toBe(value);
    });
  });

  describe("light accent — blue", () => {
    it.each([
      ["--c-accent", "#1754db"],
      ["--c-accent-hover", "#003abc"],
      ["--c-accent-ink", "#ffffff"],
      ["--c-accent-soft", "#e1ecff"],
      ["--c-accent-tint", "#f5f9ff"],
      ["--c-focus", "#1754db"],
    ])("%s resolves to %s", (name, value) => {
      expect(readToken(root, name)).toBe(value);
    });
    it("--c-accent-2 is REMOVED (v8 doesn't layer accent)", () => {
      expect(readToken(root, "--c-accent-2")).toBeUndefined();
    });
  });

  describe("secondary + neutrals — C3 compatibility bridge (ca1f324c9)", () => {
    // C3 makes v8 names one-hop aliases to runtime-owned canonical tokens.
    it("--c-cool aliases --c-info and resolves to #175cd3", () => {
      expect(readToken(root, "--c-cool")).toBe("var(--c-info)");
      expect(resolveAlias(root, "--c-cool")).toBe("#175cd3");
    });
    it("--c-mute aliases --c-ink-3 and resolves to #4f5f74", () => {
      expect(readToken(root, "--c-mute")).toBe("var(--c-ink-3)");
      expect(resolveAlias(root, "--c-mute")).toBe("#4f5f74");
    });
    it("--c-mute-2 aliases --c-ink-4 and resolves to #63728a (text AA)", () => {
      expect(readToken(root, "--c-mute-2")).toBe("var(--c-ink-4)");
      expect(resolveAlias(root, "--c-mute-2")).toBe("#63728a");
    });
  });

  describe("light status", () => {
    it.each([
      ["--c-warn", "#8a5a00"],
      ["--c-warn-soft", "#fff0c2"],
      ["--c-err", "#b42318"],
      ["--c-err-soft", "#fee4e2"],
      ["--c-pos", "#16794a"],
    ])("%s resolves to %s", (name, value) => {
      expect(readToken(root, name)).toBe(value);
    });
    it("--c-ok-soft aliases --c-pos-soft and resolves to #ddf5e8 (C3 bridge)", () => {
      expect(readToken(root, "--c-ok-soft")).toBe("var(--c-pos-soft)");
      expect(resolveAlias(root, "--c-ok-soft")).toBe("#ddf5e8");
    });
    it("--c-neg aliases --c-err and resolves to #b42318 (C3 bridge: follows err)", () => {
      expect(readToken(root, "--c-neg")).toBe("var(--c-err)");
      expect(resolveAlias(root, "--c-neg")).toBe("#b42318");
    });
  });

  describe("dark block", () => {
    it.each([
      ["--c-bg", "#0f172a"],
      ["--c-surface", "#172033"],
      ["--c-accent", "#5f94ff"],
      ["--c-accent-hover", "#91b7ff"],
      ["--c-accent-ink", "#000103"],
      ["--c-accent-soft", "#0b1936"],
      ["--c-accent-tint", "#081123"],
      ["--c-focus", "#5f94ff"],
    ])("%s resolves to %s", (name, value) => {
      expect(readToken(dark, name)).toBe(value);
    });
  });
});
