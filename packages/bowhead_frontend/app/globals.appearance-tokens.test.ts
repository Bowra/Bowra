/**
 * Component appearance-section tokens (docs/design/组件统一-技术设计.md, “外观段 token 对照”).
 *
 * Lock two facts: ① new color tokens derive from existing --c-ink / --c-accent / --c-err — literal hex values would keep
 * these colors from following team-theme and light/dark changes; ② the dark block overrides the values that vary with brightness.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const CSS = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "globals.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);

function tokenValues(name: string): string[] {
  return Array.from(CSS.matchAll(new RegExp(`${name}\\s*:\\s*([^;]+);`, "g")), (m) => m[1].trim());
}

describe("globals.css 外观段 token", () => {
  it.each([
    ["--c-fill-1", "--c-ink"],
    ["--c-fill-2", "--c-ink"],
    ["--c-fill-3", "--c-ink"],
    ["--c-separator", "--c-ink"],
    ["--c-control-line", "--c-ink"],
    ["--c-accent-press", "--c-accent"],
    ["--c-err-hover", "--c-err"],
  ])("%s 由 %s 派生，跟随团队主题", (token, source) => {
    const values = tokenValues(token);
    expect(values.length).toBeGreaterThanOrEqual(1);
    for (const value of values) expect(value).toContain(`var(${source})`);
  });

  it("深色下覆盖随明度变化的 token", () => {
    for (const token of ["--c-fill-1", "--c-separator", "--c-control-line", "--c-scrim"]) {
      expect(tokenValues(token).length, token).toBe(2);
    }
  });
});
