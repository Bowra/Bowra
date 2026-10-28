import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "../..");
const GLOBALS_CSS = readFileSync(path.join(FRONTEND_ROOT, "app/globals.css"), "utf-8");

describe("Settings layout contract", () => {
  it("keeps the shell as the only page-level width authority for every settings scope", () => {
    expect(GLOBALS_CSS).toContain(
      '.settings-shell[data-settings-scope="personal"] .settings-content-inner,\n.settings-shell[data-settings-scope="feature"] .settings-content-inner,\n.settings-shell[data-settings-scope="team"] .settings-content-inner,\n.settings-shell[data-settings-scope="space"] .settings-content-inner {\n  width: min(900px, 100%);',
    );
    expect(GLOBALS_CSS).toContain(
      '.settings-shell[data-settings-scope="personal"] .settings-content-inner > *,\n.settings-shell[data-settings-scope="feature"] .settings-content-inner > *,\n.settings-shell[data-settings-scope="team"] .settings-content-inner > *,\n.settings-shell[data-settings-scope="space"] .settings-content-inner > * {\n  inline-size: 100% !important;\n  max-inline-size: none !important;',
    );
  });
});
