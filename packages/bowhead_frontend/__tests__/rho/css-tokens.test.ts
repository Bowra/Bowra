/**
 * Sprint ρ Phase C/D — CSS token ↔ AgentRole key contract.
 *
 * Loads `app/globals.css` as string and asserts that every AgentRole has a
 * matching `--ag-<role>` token defined in both `:root` (light) and
 * `[data-theme="dark"]` blocks. Prevents the regression where
 * `--ag-kb` / `--ag-writer` / `--ag-spec` diverged from role keys
 * `kb-analyst` / `canvas-writer` / `specialist`.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const ROLES = ["lead", "compute", "kb-analyst", "canvas-writer", "specialist"] as const;

const CSS = readFileSync(
  resolve(__dirname, "../../app/globals.css"),
  "utf8",
);

function extractBlock(css: string, selector: string): string {
  // Grab the first `{ ... }` body following the selector.
  const re = new RegExp(`${selector.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&")}\\s*\\{([\\s\\S]*?)\\}`);
  const m = css.match(re);
  if (!m) throw new Error(`Selector not found: ${selector}`);
  return m[1];
}

describe("rho CSS token ↔ AgentRole contract", () => {
  const lightBlock = extractBlock(CSS, ":root");
  const darkBlock = extractBlock(CSS, '[data-theme="dark"]');

  for (const role of ROLES) {
    it(`defines --ag-${role} in :root (light)`, () => {
      const re = new RegExp(`--ag-${role}\\s*:`);
      expect(re.test(lightBlock)).toBe(true);
    });
    it(`defines --ag-${role} in [data-theme="dark"]`, () => {
      const re = new RegExp(`--ag-${role}\\s*:`);
      expect(re.test(darkBlock)).toBe(true);
    });
  }

  it("τ: real design tokens (short names) + long-name aliases both present", () => {
    // Sprint τ ports the real styles-chat-overhaul.css which canonical-names
    // are short (--ag-kb / --ag-writer / --ag-spec) per Claude Design.
    // Long-name aliases point at the short tokens for rho components keyed
    // by role-with-dash (kb-analyst, canvas-writer, specialist).
    for (const expected of [
      "--ag-kb:", "--ag-writer:", "--ag-spec:",
      "--ag-kb-analyst:", "--ag-canvas-writer:", "--ag-specialist:",
    ]) {
      expect(CSS.includes(expected)).toBe(true);
    }
  });
});
