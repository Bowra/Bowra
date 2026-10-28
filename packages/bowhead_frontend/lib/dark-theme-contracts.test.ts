import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return readFileSync(path.join(FRONTEND_ROOT, relativePath), "utf-8");
}

describe("dark theme semantic color contracts", () => {
  const globals = source("app/globals.css");

  it("defines theme-derived status foreground and border tokens", () => {
    for (const role of ["pos", "warn", "err", "info"]) {
      expect(globals).toMatch(
        new RegExp(
          `--c-${role}-ink\\s*:\\s*color-mix\\([^;]+var\\(--c-${role}\\)[^;]+var\\(--c-ink\\)`,
        ),
      );
      expect(globals).toMatch(
        new RegExp(
          `--c-${role}-line\\s*:\\s*color-mix\\([^;]+var\\(--c-${role}\\)[^;]+transparent`,
        ),
      );
    }
  });

  it("defines dark semantic status pairs in the legacy runtime theme", () => {
    const darkBlock = globals.match(
      /\[data-theme="dark"\]\s*\{([\s\S]*?)\n\}/,
    )?.[1];

    expect(darkBlock).toBeDefined();
    for (const [token, value] of Object.entries({
      pos: "#62C491",
      "pos-soft": "#10291F",
      warn: "#E5B65A",
      "warn-soft": "#30240D",
      err: "#F58B82",
      "err-soft": "#351714",
      info: "#8BB7FF",
      "info-soft": "#14243E",
    })) {
      expect(darkBlock).toMatch(
        new RegExp(`--c-${token}\\s*:\\s*${value}`, "i"),
      );
    }
  });

  it("styles attachment chips through theme tokens, not a legacy dark class", () => {
    // Chat Cards Spec D10: chips have no coloured type blocks, so they need no dark-only overrides.
    expect(globals).not.toContain(".theme-dark .v2-chip");
    expect(globals).not.toContain(".v2-chip__badge");
  });

  it("keeps knowledge status surfaces free of light-only color pairs", () => {
    expect(source("components/knowledge/kb-status-pill.tsx")).not.toMatch(
      /#2b556f|#3e5436/i,
    );
    expect(source("styles/kb-management.css")).not.toMatch(
      /#fbf1d9|#f8ebcc|#f4d58a/i,
    );
    expect(source("components/knowledge/matrix/matrix.css")).not.toMatch(
      /#f4d58a/i,
    );
    expect(source("styles/report-canvas.css")).not.toMatch(
      /#7d5e1d|#8a6820/i,
    );
  });

  it("keeps capability authoring cards on semantic surfaces", () => {
    for (const relativePath of [
      "components/canvas/sections/create-tool-card.tsx",
      "components/canvas/sections/create-subagent-card.tsx",
      "components/canvas/sections/create-workflow-card.tsx",
      "components/workflow-solidify/solidify-card.tsx",
      "components/workflow-solidify/wf-shared.tsx",
      "app/(app)/team/settings/subagents/page.tsx",
    ]) {
      expect(source(relativePath)).not.toMatch(
        /#fffdf8|#eaf1ef|#cdd8d6/i,
      );
    }
  });

  it("keeps source-panel and scheduled states free of light-only palettes", () => {
    expect(source("components/canvas/source-panel.tsx")).not.toMatch(
      /#f9fafb|#fee2e2|#fef3c7|#dbeafe|#fef2f2/i,
    );
    expect(source("components/settings/scheduled.tsx")).not.toMatch(
      /#eef2f3|#d8e2e5|#7a2a1c|#8a4133/i,
    );
  });

  it("keeps rich-text tables and chart citations on semantic surfaces", () => {
    expect(globals).toMatch(
      /\.citation-chart-thumbnail\s*\{[\s\S]*?background:\s*var\(--c-info-soft\)/,
    );
    // Native chart tables are excluded from the Markdown grid rule (cfaff399c).
    expect(globals).toMatch(
      /\.ProseMirror table:not\(\.v2-chart-table-el\) th,[\s\S]*?background:\s*var\(--c-surface-2\)/,
    );
  });
});
