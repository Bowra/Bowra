/**
 * Guard against theme-independent `:root` color overrides.
 *
 * `:root` and `[data-theme="dark"]` share specificity (both 0,1,0), so later wins. A late `:root { --x: literal-color }`
 * therefore overrides earlier `[data-theme="dark"]` values in dark mode. This happened when porting chat-v3 designs:
 * late globals.css declarations used light literals for `--c-err-soft` / `--c-accent-tint`, turning dark-mode error backgrounds pale pink,
 * with error-banner body contrast 1.09:1. Each individual block looked correct; only cascading the actual import order of both root layouts revealed it.
 *
 * Criterion: for every token with a dark declaration, the final effective dark-mode declaration in import order must not be a theme-independent `:root` literal color.
 * Aliases using `var(...)` / `color-mix(...)` follow the theme and are not overrides.
 */

import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "..");

type Decl = { kind: "root" | "dark"; value: string; where: string };

/** Non-color exceptions: document why; before adding one, consider whether it truly should be theme-independent. */
const ALLOWED_THEMELESS: Record<string, string> = {
  "--elev-composer": "输入框阴影，不是颜色 token；深色下阴影略浅不影响可读性",
};

function cssImports(layout: string): string[] {
  const source = readFileSync(path.join(ROOT, layout), "utf-8");
  const dir = path.dirname(path.join(ROOT, layout));
  return Array.from(source.matchAll(/^import "(\.[^"]+\.css)";/gm), (m) => path.resolve(dir, m[1]));
}

function cascade(files: string[]): Map<string, Decl[]> {
  const tokens = new Map<string, Decl[]>();
  for (const file of files) {
    const css = readFileSync(file, "utf-8").replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
    const stack: Array<{ selector: string; start: number }> = [];
    let last = 0;
    for (const match of css.matchAll(/[{}]/g)) {
      const index = match.index ?? 0;
      if (match[0] === "{") {
        stack.push({ selector: css.slice(last, index).trim(), start: index + 1 });
      } else {
        const block = stack.pop();
        if (block && stack.length === 0) {
          const selectors = block.selector.split(",").map((s) => s.trim());
          const kind = selectors.includes('[data-theme="dark"]')
            ? "dark"
            : selectors.length === 1 && selectors[0] === ":root"
              ? "root"
              : null;
          if (kind) {
            const body = css.slice(block.start, index);
            const line = css.slice(0, block.start).split("\n").length;
            for (const decl of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
              const list = tokens.get(decl[1]) ?? [];
              list.push({
                kind,
                value: decl[2].replace(/\s+/g, " ").trim(),
                where: `${path.relative(ROOT, file)}:${line}`,
              });
              tokens.set(decl[1], list);
            }
          }
        }
      }
      last = index + 1;
    }
  }
  return tokens;
}

function darkClobbers(files: string[]): string[] {
  const problems: string[] = [];
  for (const [token, decls] of cascade(files)) {
    if (!decls.some((d) => d.kind === "dark")) continue;
    const effective = decls[decls.length - 1];
    if (effective.kind !== "root") continue;
    if (/^(var|color-mix)\(/.test(effective.value)) continue;
    if (token in ALLOWED_THEMELESS) continue;
    problems.push(`${token} = ${effective.value} (${effective.where})`);
  }
  return problems;
}

function hexOf(value: string | undefined): [number, number, number] | null {
  const m = value?.match(/^#([0-9a-f]{6})$/i);
  if (!m) return null;
  const n = Number.parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const lum = (c: [number, number, number]) => {
    const [r, g, bl] = c.map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe.each([
  ["认证布局", "app/(app)/layout.tsx"],
  ["公开布局", "app/(public)/layout.tsx"],
])("%s的样式层叠", (_name, layout) => {
  const files = cssImports(layout);

  it("按引入顺序读到了 globals.css", () => {
    expect(files.some((file) => file.endsWith(path.join("app", "globals.css")))).toBe(true);
  });

  it("后段不分主题的 :root 不覆盖深色颜色值", () => {
    expect(darkClobbers(files)).toEqual([]);
  });

  it("深色下正文放在提示底色上仍有 4.5:1", () => {
    const tokens = cascade(files);
    const darkValue = (token: string) => {
      const decls = tokens.get(token) ?? [];
      return decls[decls.length - 1]?.value;
    };
    const ink = hexOf(darkValue("--c-ink"));
    expect(ink).not.toBeNull();
    for (const soft of ["--c-err-soft", "--c-warn-soft", "--c-pos-soft", "--c-info-soft", "--c-accent-tint"]) {
      const background = hexOf(darkValue(soft));
      expect(background, soft).not.toBeNull();
      expect(contrast(ink!, background!), soft).toBeGreaterThanOrEqual(4.5);
    }
  });
});
