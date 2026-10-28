import { cleanup, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { EditModeToggle } from "./edit-mode-toggle";

afterEach(cleanup);

// Colors moved from inline styles to styles/ui-controls-legacy.css (UI appearance refresh batch one);
// lock which button has which appearance and that those rules still use theme tokens.
const LEGACY_CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "..", "..", "styles", "ui-controls-legacy.css"),
  "utf8",
);
function ruleBody(selector: string): string {
  const start = LEGACY_CSS.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  return LEGACY_CSS.slice(start, LEGACY_CSS.indexOf("}", start));
}

describe("EditModeToggle theme colors", () => {
  it("uses semantic theme tokens for read and edit actions", () => {
    renderIntl(
      <EditModeToggle isEditMode={false} onToggle={vi.fn()} />,
      { locale: "en-US" },
    );

    const readButton = screen.getByRole("button", {
      name: "Edit This Report",
    }) as HTMLButtonElement;
    expect(readButton.className).toBe("v2-edit-toggle-btn report-edit-btn");
    expect(ruleBody(".report-edit-btn")).toContain("background: var(--c-surface)");
    expect(ruleBody(".report-edit-btn")).toContain("border: 1px solid var(--c-line)");
    expect(ruleBody(".report-edit-btn")).toContain("color: var(--c-ink)");

    cleanup();
    renderIntl(
      <EditModeToggle
        isEditMode
        onCancelEdit={vi.fn()}
        onToggle={vi.fn()}
      />,
      { locale: "en-US" },
    );

    const cancelButton = screen.getByRole("button", {
      name: "Exit edit",
    }) as HTMLButtonElement;
    expect(cancelButton.className).toBe("v2-edit-toggle-btn report-edit-btn");

    const doneButton = screen.getByRole("button", {
      name: "Done",
    }) as HTMLButtonElement;
    expect(doneButton.classList.contains("report-edit-btn--active")).toBe(true);
    const active = ruleBody(".report-edit-btn--active");
    expect(active).toContain("background: var(--c-accent)");
    expect(active).toContain("border-color: var(--c-accent)");
    expect(active).toContain("color: var(--c-accent-ink)");
  });

  it("disabled read button dims the label; edit mode keeps the accent label", () => {
    renderIntl(
      <EditModeToggle isEditMode={false} disabled onToggle={vi.fn()} />,
      { locale: "en-US" },
    );
    const readButton = screen.getByRole("button", { name: "Edit This Report" });
    expect(readButton.classList.contains("report-edit-btn--unavailable")).toBe(true);
    expect(ruleBody(".report-edit-btn--unavailable")).toContain("color: var(--c-ink-4)");

    cleanup();
    renderIntl(
      <EditModeToggle isEditMode disabled onToggle={vi.fn()} />,
      { locale: "en-US" },
    );
    const doneButton = screen.getByRole("button", { name: "Done" });
    expect(doneButton.classList.contains("report-edit-btn--active")).toBe(true);
    expect(doneButton.classList.contains("report-edit-btn--unavailable")).toBe(false);
  });

  it("uses semantic error tokens for failed saves", () => {
    renderIntl(
      <EditModeToggle
        error="save failed"
        isEditMode
        onRetry={vi.fn()}
        onToggle={vi.fn()}
      />,
      { locale: "en-US" },
    );

    const status = screen.getByText("Save failed");
    // Error color lives in styles/report-canvas.css .legacy-edit-bar-status.is-error (original inline var(--c-err))
    expect(status.classList.contains("legacy-edit-bar-status")).toBe(true);
    expect(status.classList.contains("is-error")).toBe(true);

    const retryButton = screen.getByRole("button", {
      name: "Retry save",
    }) as HTMLButtonElement;
    expect(retryButton.classList.contains("report-edit-btn--retry")).toBe(true);
    const retry = ruleBody(".report-edit-btn--retry");
    expect(retry).toContain("background: var(--c-err-soft)");
    expect(retry).toContain("color: var(--c-err-ink)");
  });
});
