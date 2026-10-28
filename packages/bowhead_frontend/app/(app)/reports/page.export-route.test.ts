/**
 * Report-library exports must use the workbench (V2); no legacy direct-export path may remain.
 *
 * Background (116 v1.1.25/26 observations): both library entry points called `exportCtl.start(id, fmt)` **without
 * a v2 payload or `prepareCanvasExport`**, structurally hitting the legacy endpoint
 * `POST /canvas/{id}/export` (singular), without any fallback notice. Measured with identical content:
 *   · xlsx  legacy 3 sheets / 29 rows / 7.2 KB   vs  V2 9 sheets / 426 rows / 97.8 KB
 *   · pdf   legacy 1 page / 3.8 KB / 0 images    vs  V2 6 pages / 143 KB / actual images
 * V2 credentials require a mounted, fully drawn report; the library does not render it, so send users to the workbench to export.
 *
 * ⚠ Lock **source facts** here (jsdom cannot run the full library + workbench). Verify the actual flow on a device:
 * clicking library Download should navigate to `/?session=…&report=1&export=…`, with network traffic hitting
 * `POST /canvas/{id}/exports` (**plural**).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const RAW = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "page.tsx"), "utf8");
// Strip comments before assertions, or explanatory comments create false positives (this file's first version hit that).
const SRC = RAW.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("报告库导出路由", () => {
  it("不再有 legacy 直发（exportCtl.start）", () => {
    expect(SRC).not.toContain("exportCtl.start");
  });

  it("两处入口都走 reportExportHrefForSession", () => {
    // Card Download + drawer Regenerate: missing either leaves a second legacy path
    const hits = SRC.match(/reportExportHrefForSession\(/g) ?? [];
    expect(hits.length).toBeGreaterThanOrEqual(2);
  });

  it("跳转带 base path（appPath），否则带前缀部署会 404", () => {
    expect(SRC).toMatch(/appPath\(reportExportHrefForSession\(/);
  });

  it("显式显示的零 section 空壳不能提供下载", () => {
    expect(SRC).toContain("exportCtl.busy || r.sections_count === 0");
  });
});
