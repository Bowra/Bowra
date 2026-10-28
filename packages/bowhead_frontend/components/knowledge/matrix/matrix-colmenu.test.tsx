/**
 * Column-header menu (P2 batch 5 accessibility fix, both appearances): the caret is a real, named button that opens the
 * shared menu from the keyboard, and Esc returns focus to it instead of dropping it on <body>.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ reload: vi.fn() }));
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));

import { MatrixViewM2 } from "./matrix-view-m2";

const snapshot = {
  matrixId: "mx_test", title: "Contracts", readOnly: false, seq: 1, canEdit: true, runs: [],
  columns: [{ columnId: "c_date", name: "Delivery date", type: "question", question: "When is delivery?" }],
  rows: [{ fileId: "kbf_1", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "r1", approved: false, cells: {} }],
} as unknown as MatrixSnapshot;

afterEach(() => {
  cleanup();
  setMatrixSnapshot(null);
});

describe("column-header menu", () => {
  it("is a named button that opens the menu from the keyboard and gets focus back on Esc", async () => {
    mocks.reload.mockImplementation(async () => setMatrixSnapshot(snapshot));
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
      </NextIntlClientProvider>,
    );
    const caret = await screen.findByRole("button", { name: "Actions for column Delivery date" });
    expect(caret).toHaveAttribute("aria-haspopup", "menu");
    expect(caret).toHaveAttribute("type", "button");
    caret.focus();
    fireEvent.click(caret, { detail: 0 });
    const menu = await screen.findByTestId("kb-matrix-colmenu");
    const items = within(menu).getAllByRole("menuitem", { hidden: true });
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(items[0], { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-colmenu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(caret));
  });
});
