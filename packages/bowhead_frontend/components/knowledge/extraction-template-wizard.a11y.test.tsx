/**
 * Extraction-table wizard accessibility (P2 batch 5 fix, both appearances): it is a modal dialog named by its title,
 * focus starts inside and Tab stays inside, Esc closes it, and errors are announced.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

vi.mock("@/lib/use-space", () => ({
  useSpaceSelection: () => ({ activeId: "ws_1", loading: false, items: [], error: null, refresh: vi.fn() }),
}));
const { draft } = vi.hoisted(() => ({ draft: vi.fn() }));
vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, draftKnowledgeTableFields: (...args: unknown[]) => draft(...args) };
});

import { DraftWizard } from "./extraction-template-wizard";

afterEach(() => {
  cleanup();
  draft.mockReset();
});

const failDraft = () => draft.mockRejectedValue(new Error("boom"));

describe("DraftWizard dialog semantics", () => {
  it("is a modal dialog named by its step title, with focus inside", async () => {
    renderIntl(<DraftWizard mode="create" onClose={vi.fn()} onSaved={vi.fn()} />, { locale: "en-US" });
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("data-testid", "kt-wizard");
    expect(dialog).toHaveAccessibleName(/Describe/);
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    renderIntl(<DraftWizard mode="create" onClose={onClose} onSaved={vi.fn()} />, { locale: "en-US" });
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("wraps Tab from the last control back to the first", async () => {
    renderIntl(<DraftWizard mode="create" onClose={vi.fn()} onSaved={vi.fn()} />, { locale: "en-US" });
    fireEvent.change(screen.getByTestId("kt-describe"), { target: { value: "contracts" } });
    const draft = screen.getByTestId("kt-draft-btn");
    draft.focus();
    fireEvent.keyDown(draft, { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByTestId("kt-name"));
  });

  it("announces errors", async () => {
    failDraft();
    renderIntl(<DraftWizard mode="create" onClose={vi.fn()} onSaved={vi.fn()} />, { locale: "en-US" });
    fireEvent.change(screen.getByTestId("kt-describe"), { target: { value: "contracts" } });
    fireEvent.click(screen.getByTestId("kt-draft-btn"));
    expect(await screen.findByRole("alert")).toHaveAttribute("data-testid", "kt-wizard-error");
  });

  it("returns focus to the explicit opener (WebKit does not focus clicked buttons)", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    const ref = createRef<HTMLElement>();
    (ref as { current: HTMLElement | null }).current = opener;
    const onClose = vi.fn();
    const { unmount } = renderIntl(<DraftWizard mode="create" onClose={onClose} onSaved={vi.fn()} returnFocusRef={ref} />, { locale: "en-US" });
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    unmount();
    await waitFor(() => expect(document.activeElement).toBe(opener));
    opener.remove();
  });

  it("ignores Esc while drafting", async () => {
    draft.mockReturnValue(new Promise(() => {}));
    const onClose = vi.fn();
    renderIntl(<DraftWizard mode="create" onClose={onClose} onSaved={vi.fn()} />, { locale: "en-US" });
    fireEvent.change(screen.getByTestId("kt-describe"), { target: { value: "contracts" } });
    fireEvent.click(screen.getByTestId("kt-draft-btn"));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveAttribute("aria-busy", "true"));
    fireEvent.keyDown(screen.getByTestId("kt-describe"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("names the per-field controls", async () => {
    renderIntl(<DraftWizard mode="edit" existing={{
      doc_type_id: "dt_1", workspace_id: "ws_1", name: "Contracts", description: "", record_count: 0, extractor_model: "",
      cross_verify_enabled: false, created_by: "u", created_at: "", updated_at: "",
      fields: [{ name: "amount", type: "number", description: "", required: false, cross_verify: false, extraction_hint: "" }],
    }} onClose={vi.fn()} onSaved={vi.fn()} />, { locale: "en-US" });
    expect(screen.getByRole("combobox", { name: "Field type" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete field" })).toBeInTheDocument();
  });
});
