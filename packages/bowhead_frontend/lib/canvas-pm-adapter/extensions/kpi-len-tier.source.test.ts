/**
 * Source drift gate: KPI value tiers must be wired into **every** `.v2-kpi-cell .val` rendering implementation.
 *
 * The device defect in feedback-triage-2026-08-26 had this shape: `kpiValueSizeTier` and all three
 * CSS tiers (globals.css + report-canvas.css) existed and matched the production bundle, but the **canvas**
 * render was a second hand-written JSX implementation in `atomic.tsx`, without `data-len` —
 * making chat's "建材用品 139.15%" a 17px single line, while the same canvas value wrapped into 2 lines at 32px.
 *
 * Lesson: verifying effect requires **attributes/behavior**, beyond stylesheets. Complete CSS can misleadingly suggest a fix.
 * Unit tests miss this (both implementations render correctly individually), so lock it at the source level.
 *
 * ⚠ Require every file rendering `className="val"` to contain data-len={kpiValueSizeTier(,
 * rather than listing the two known files; automatically catch a future third copy.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..", "..", "..");
const SKIP_DIRS = new Set(["node_modules", ".next", "out", "coverage", "e2e"]);

function walk(dir: string, acc: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    // Skip locally generated dot directories to avoid scanning static output or broken symlinks.
    if (SKIP_DIRS.has(name) || name.startsWith(".")) continue;
    const full = join(dir, name);
    let isDir = false;
    try {
      isDir = statSync(full).isDirectory();
    } catch {
      continue; // Broken symlink/permission failure → skip, without breaking the gate itself
    }
    if (isDir) walk(full, acc);
    else if (/\.tsx$/.test(name) && !/\.test\.tsx$/.test(name)) acc.push(full);
  }
  return acc;
}

describe("KPI 值分档漂移闸", () => {
  const files = walk(ROOT);

  it("★每一份渲 `.val` 的 KPI 实现都必须给出 data-len", () => {
    const offenders: string[] = [];
    for (const file of files) {
      const src = readFileSync(file, "utf8");
      // Only actual KPI-cell renderers: both mention v2-kpi-cell and render className="val"
      if (!src.includes("v2-kpi-cell")) continue;
      if (!/className="val"/.test(src)) continue;
      if (!src.includes("data-len={kpiValueSizeTier(")) {
        offenders.push(file.replace(ROOT, ""));
      }
    }
    expect(offenders).toEqual([]);
  });

  it("闸本身不是空转的：确实扫到了 KPI 渲染文件", () => {
    // Without this, the preceding test passes forever when no files match (a false safety net).
    const hits = files.filter((f) => {
      const src = readFileSync(f, "utf8");
      return src.includes("v2-kpi-cell") && /className="val"/.test(src);
    });
    expect(hits.length).toBeGreaterThanOrEqual(2);
  });
});
