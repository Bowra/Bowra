/**
 * Guard: CSS comments must not close prematurely.
 *
 * Real incident (2026-08-01, blocked ship 1.0.48): in a `styles/config-copilot.css` comment, the author used
 * asterisks as wildcard prefixes for several adjacent class names (such as `dr-asterisk` and `badge-asterisk`). An asterisk immediately followed by a slash is
 * a comment terminator; the comment closed early, turning the remaining sentence into CSS text. cssnano reported `Unexpected '/'`,
 * failing the entire frontend build.
 *
 * Why a dedicated guard is needed: this fails **only in production builds**. Neither `npm run dev` nor
 * `make typecheck-frontend` runs CSS minification; postcss is permissive enough to parse the bad sample
 * (postcss.parse did not throw in testing), leaving checks green until shipping blocked the release.
 *
 * Criterion: if neither whitespace nor another asterisk precedes the terminator, someone likely placed a `foo-asterisk` pattern
 * against it. Measured across all 24 CSS files with zero false positives.
 *
 * ⚠ Never write that character combination in this file's comments, even in examples, or this guard itself will not compile
 * (the first version hit exactly this). See the string literal in the final case for the real bad sample.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..");
const SKIP = new Set(["node_modules", "out", ".next", ".git"]);
/** Neither whitespace nor another asterisk before the terminator ⇒ likely a `foo-asterisk` pattern pressed against it. */
const PREMATURE_TERMINATOR = /(?<![\s*])\*\//g;

function cssFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP.has(entry.name) || entry.name.startsWith(".next")) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) cssFiles(full, out);
    else if (entry.name.endsWith(".css")) out.push(full);
  }
  return out;
}

describe("CSS 注释终止符", () => {
  it("找得到 CSS 文件（防止扫描器空转变成假绿）", () => {
    expect(cssFiles(ROOT).length).toBeGreaterThan(5);
  });

  it("没有任何注释被 `xxx-*/` 提前闭合", () => {
    const offenders: string[] = [];
    for (const file of cssFiles(ROOT)) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, i) => {
          PREMATURE_TERMINATOR.lastIndex = 0;
          if (PREMATURE_TERMINATOR.test(line)) {
            offenders.push(`${file.slice(ROOT.length + 1)}:${i + 1}: ${line.trim().slice(0, 120)}`);
          }
        });
    }
    expect(offenders).toEqual([]);
  });

  it("判据本身有效：坏样本必须被逮到", () => {
    const bad = "/* 设计稿 verbatim：drawer/dr-*/badge-*/receipt */";
    PREMATURE_TERMINATOR.lastIndex = 0;
    expect(PREMATURE_TERMINATOR.test(bad)).toBe(true);
    PREMATURE_TERMINATOR.lastIndex = 0;
    expect(PREMATURE_TERMINATOR.test("/* 正常注释 */")).toBe(false);
  });
});
