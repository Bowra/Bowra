/**
 * Focus-ring token contract.
 *
 * `outline: 2px solid var(--c-focus)` is invalid at computed-value time when `--c-focus` is undefined, so the whole
 * outline silently disappears. That happened under the default palette: only team/preset/personal brand schemes
 * injected `--c-focus`, and about twenty focus rules without a fallback never rendered a ring.
 *
 * Criteria:
 *   1. globals.css defines `--c-focus` in every block that defines `--c-accent` (base light, base dark and each color
 *      profile in both modes), equal to that block's accent, as legacy_theme_registry does for its focus role.
 *   2. Every `var(--c-focus)` in any stylesheet either carries a fallback or relies on criterion 1; with the
 *      definitions removed, every bare use is reported.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = resolve(__dirname, "..");
const GLOBALS = join(ROOT, "app/globals.css");
const STYLE_DIRS = ["app", "styles", "components"].map((dir) => join(ROOT, dir));

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "));
}

function cssFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (name === "node_modules" || name.startsWith(".")) continue;
    if (statSync(full).isDirectory()) out.push(...cssFiles(full));
    else if (name.endsWith(".css")) out.push(full);
  }
  return out;
}

type Block = { selector: string; body: string; start: number };

/** Top-level rule blocks (nested at-rules are skipped; globals defines tokens at top level). */
function topLevelBlocks(css: string): Block[] {
  const blocks: Block[] = [];
  const stack: Array<{ selector: string; start: number }> = [];
  let last = 0;
  for (const match of css.matchAll(/[{}]/g)) {
    const index = match.index ?? 0;
    if (match[0] === "{") {
      // Text after a top-level @import statement belongs to the next selector only.
      stack.push({ selector: css.slice(last, index).split(";").pop()!.trim(), start: index + 1 });
    } else {
      const block = stack.pop();
      if (block && stack.length === 0) {
        blocks.push({ selector: block.selector, body: css.slice(block.start, index), start: block.start });
      }
    }
    last = index + 1;
  }
  return blocks;
}

function declaration(body: string, name: string): string | null {
  const match = body.match(new RegExp(`(?:^|[;\\s])${name}\\s*:\\s*([^;]+);`));
  return match ? match[1].trim().toLowerCase() : null;
}

/** Blocks that define --c-accent but not a matching --c-focus. */
function focusDefinitionGaps(css: string): string[] {
  const lineOf = (offset: number) => css.slice(0, offset).split("\n").length;
  return topLevelBlocks(stripComments(css))
    .filter((block) => declaration(block.body, "--c-accent") !== null)
    .filter((block) => declaration(block.body, "--c-focus") !== declaration(block.body, "--c-accent"))
    .map((block) => `globals.css:${lineOf(block.start)} ${block.selector.replace(/\s+/g, " ")}`);
}

let bareFocusUsesCache: string[] | null = null;

function bareFocusUses(): string[] {
  if (bareFocusUsesCache) return bareFocusUsesCache;
  const out: string[] = [];
  for (const file of STYLE_DIRS.flatMap(cssFiles)) {
    const css = stripComments(readFileSync(file, "utf-8"));
    for (const match of css.matchAll(/var\(\s*--c-focus\s*\)/g)) {
      const line = css.slice(0, match.index ?? 0).split("\n").length;
      out.push(`${relative(ROOT, file)}:${line}`);
    }
  }
  bareFocusUsesCache = out;
  return out;
}

/** Uses that would render nothing: bare uses count only when the base definition is missing. */
function unresolvedFocusUses(globalsCss: string): string[] {
  const blocks = topLevelBlocks(stripComments(globalsCss));
  const base = blocks.find((block) => block.selector === ":root" && declaration(block.body, "--c-accent") !== null);
  const dark = blocks.find((block) => block.selector === '[data-theme="dark"]' && declaration(block.body, "--c-accent") !== null);
  const defined = Boolean(base && declaration(base.body, "--c-focus") && dark && declaration(dark.body, "--c-focus"));
  return defined ? [] : bareFocusUses();
}

describe("--c-focus token", () => {
  const globalsCss = readFileSync(GLOBALS, "utf-8");

  it("is defined beside --c-accent in every globals.css palette block, equal to the accent", () => {
    const accentBlocks = topLevelBlocks(stripComments(globalsCss)).filter((block) => declaration(block.body, "--c-accent") !== null);
    // Base light + base dark + four color profiles in both modes.
    expect(accentBlocks.length).toBeGreaterThanOrEqual(10);
    expect(focusDefinitionGaps(globalsCss)).toEqual([]);
  });

  it("leaves no var(--c-focus) without a definition or fallback", () => {
    expect(unresolvedFocusUses(globalsCss)).toEqual([]);
  });

  it("reports the bare uses and the gaps once the definitions are removed (negative control)", () => {
    const withoutFocus = globalsCss.replace(/^\s*--c-focus\s*:[^;]+;\s*$/gm, "");
    expect(bareFocusUses().length).toBeGreaterThan(0);
    expect(unresolvedFocusUses(withoutFocus)).toEqual(bareFocusUses());
    expect(focusDefinitionGaps(withoutFocus).length).toBeGreaterThanOrEqual(10);
  });
});
