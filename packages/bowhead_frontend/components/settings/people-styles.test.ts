import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const FRONTEND_ROOT = path.resolve(__dirname, "../..");
const GLOBALS_CSS = readFileSync(path.join(FRONTEND_ROOT, "app/globals.css"), "utf-8");
const PEOPLE_SOURCE = readFileSync(path.join(__dirname, "people.tsx"), "utf-8");
const MEMBERS_SOURCES = [
  PEOPLE_SOURCE,
  readFileSync(path.join(__dirname, "../admin/members/members-table.tsx"), "utf-8"),
  readFileSync(path.join(__dirname, "../admin/members/promote-modal.tsx"), "utf-8"),
];

function hasCssClass(css: string, className: string): boolean {
  const escapedClassName = className.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\.${escapedClassName}(?![a-zA-Z0-9_-])`).test(css);
}

describe("People settings style contract", () => {
  it("does not treat a longer class name as the referenced class", () => {
    expect(hasCssClass(".team-members-table-wrap {}", "team-members-table")).toBe(false);
  });

  it("defines every referenced team member class in globals.css", () => {
    const classNames = new Set(
      MEMBERS_SOURCES.flatMap((source) =>
        source.match(/(?<=["\s])team-(?:members|role)[a-z0-9_-]*\b/g) ?? [],
      ),
    );

    expect(classNames.size).toBeGreaterThan(0);
    for (const className of classNames) {
      expect(
        hasCssClass(GLOBALS_CSS, className),
        `missing CSS selector for ${className}`,
      ).toBe(true);
    }
  });

  it("keeps member actions compact and the desktop table within its content area", () => {
    expect(GLOBALS_CSS).toContain(".team-members-table {\n  border-collapse: separate;\n  border-spacing: 0;\n  min-width: 0;\n  table-layout: fixed;");
    expect(GLOBALS_CSS).toContain(".team-members-table__tier-activity-column {\n  width: 140px;");
    expect(GLOBALS_CSS).toContain(".team-members-table__actions-column {\n  width: 210px;");
  });
});
