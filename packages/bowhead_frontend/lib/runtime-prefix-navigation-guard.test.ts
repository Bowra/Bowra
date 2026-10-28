import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE_ROOTS = [
  join(__dirname, "..", "app"),
  join(__dirname, "..", "components"),
  join(__dirname, "..", "lib"),
];

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    if (
      !/\.(?:ts|tsx)$/.test(entry.name)
      || entry.name.endsWith(".d.ts")
      || entry.name.includes(".test.")
    ) {
      return [];
    }
    return [path];
  });
}

describe("runtime path-prefix navigation guard", () => {
  it("keeps browser navigation on useAppRouter or appPath", () => {
    const violations: string[] = [];

    for (const path of SOURCE_ROOTS.flatMap(sourceFiles)) {
      const source = readFileSync(path, "utf8");
      const relativePath = path.replace(`${join(__dirname, "..")}\/`, "");

      if (
        relativePath !== "lib/use-app-router.ts"
        && /from\s+["']next\/navigation["']/.test(source)
        && /\buseRouter\b/.test(source)
        && /\brouter\.(?:push|replace)\s*\(/.test(source)
      ) {
        violations.push(`${relativePath}: direct Next router navigation`);
      }
      if (/<(?:Link|a)\b[^>]*\bhref\s*=\s*(?:["']\/(?!\/)|\{\s*["'`]\/(?!\/))/s.test(source)) {
        violations.push(`${relativePath}: unprefixed internal anchor`);
      }
      if (/window\.location\.(?:href\s*=|assign\s*\(|replace\s*\()\s*["']\/(?!\/)/.test(source)) {
        violations.push(`${relativePath}: unprefixed window navigation`);
      }
      if (/window\.open\(\s*["'`]\/(?!\/)/.test(source)) {
        violations.push(`${relativePath}: unprefixed window.open navigation`);
      }
      if (/history\.replaceState\([^\n]*,\s*["']\/(?!\/)/.test(source)) {
        violations.push(`${relativePath}: unprefixed history replacement`);
      }
    }

    expect(violations).toEqual([]);
  });
});
