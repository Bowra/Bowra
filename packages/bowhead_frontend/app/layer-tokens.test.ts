/**
 * Layer-token guard (docs/design/组件统一-技术设计.md “图层”).
 *
 * Lock three facts:
 *  1. `--z-*` order (menus, Toasts and tips above any dialog layer; Toast above top-level confirmation).
 *  2. Shared overlays use tokens: top-level DialogShell, menus, Toast container, ordinary-dialog backdrop.
 *  3. Every hardcoded z-index in the repository is below `--z-menu`, or menus and
 *     Toasts opened inside a custom overlay get covered again (as with pre-refactor Toast 9999 < confirmation 10000).
 *
 * jsdom cannot measure stacking itself; e2e/layer-stacking.spec.ts verifies it in real browsers.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const GLOBALS = readFileSync(join(FRONTEND, "app/globals.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

function token(name: string): number {
  const match = new RegExp(`--z-${name}\\s*:\\s*(-?\\d+)\\s*;`).exec(GLOBALS);
  if (!match) throw new Error(`--z-${name} missing`);
  return Number(match[1]);
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name === "node_modules" || name.startsWith(".")) continue;
      walk(path, out);
    } else if (/\.(css|tsx?)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      out.push(path);
    }
  }
  return out;
}

/** Hardcoded source z-index numbers: CSS `z-index: N`, inline `zIndex: N` and each ternary-branch number. */
function hardCodedZIndexes(): Array<{ file: string; value: number }> {
  const hits: Array<{ file: string; value: number }> = [];
  for (const file of ["app", "components", "lib", "styles"].flatMap((dir) => walk(join(FRONTEND, dir)))) {
    const source = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    const rel = relative(FRONTEND, file);
    for (const m of source.matchAll(/z-index\s*:\s*(-?\d+)/g)) hits.push({ file: rel, value: Number(m[1]) });
    for (const m of source.matchAll(/zIndex\s*:\s*([^,}\n]+)/g)) {
      for (const n of m[1].matchAll(/(?<![\w.-])(\d+)(?![\w.])/g)) hits.push({ file: rel, value: Number(n[1]) });
    }
  }
  return hits;
}

describe("图层 token", () => {
  it("按层级从低到高排列", () => {
    const order = ["sticky", "dropdown", "drawer", "dialog", "dialog-top", "menu", "toast", "tooltip"].map(token);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(new Set(order).size).toBe(order.length);
  });

  it("共享浮层引用 token", () => {
    const read = (path: string) => readFileSync(join(FRONTEND, path), "utf8");
    expect(read("components/ui/dialog.tsx")).toMatch(/zIndex:\s*"var\(--z-dialog-top\)"/);
    // Menus default to --z-menu (overlay, bottom sheet and backdrop share one layer value).
    expect(read("components/ui/menu.tsx")).toMatch(/zIndex\s*=\s*liftZ \?\? "var\(--z-menu\)"/);
    // UI-refresh batch three moved Toast-container inline styles into styles/ui-controls-legacy.css .legacy-toast-container.
    expect(read("components/chat/toast-container.tsx")).toContain('className="legacy-toast-container"');
    expect(read("styles/ui-controls-legacy.css")).toMatch(/\.legacy-toast-container\s*\{[^}]*z-index:\s*var\(--z-toast\)/);
    expect(GLOBALS).toMatch(/\.settings-dialog-backdrop\s*\{[^}]*z-index:\s*var\(--z-dialog\)/);
  });

  it("没有写死的 z-index 高过菜单层", () => {
    const menu = token("menu");
    const tooHigh = hardCodedZIndexes().filter((hit) => hit.value >= menu);
    expect(tooHigh).toEqual([]);
  });
});
