/**
 * Matrix run menu (P2 batch 5 accessibility fix): a shared menu on body with menu semantics, arrow keys and Esc back
 * to the caret. Items that open a confirmation end with "…".
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixSnapshot } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ estimateRun: vi.fn(), reload: vi.fn() }));

vi.mock("@/lib/kb-matrix/run-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-matrix/run-api")>();
  return { ...actual, estimateRun: (...args: unknown[]) => mocks.estimateRun(...args) };
});
vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => false }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./matrix-grid", () => ({ MatrixGrid: () => <div data-testid="matrix-grid" /> }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));

import { MatrixViewM2 } from "./matrix-view-m2";

const snapshot: MatrixSnapshot = {
  matrixId: "mx_test", title: "Contract matrix", readOnly: false, columns: [], rows: [], seq: 1, canEdit: true, runs: [],
};
const m = messagesEn.kbMatrix;

function renderView() {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snapshot));
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.estimateRun.mockReturnValue(new Promise(() => {}));
});
afterEach(() => {
  cleanup();
  setMatrixSnapshot(null);
});

describe("matrix run menu", () => {
  it("is a shared menu with menu items, keyboard focus and Esc back to the caret", async () => {
    renderView();
    const caret = await screen.findByRole("button", { name: m.run_options });
    caret.focus();
    fireEvent.click(caret, { detail: 0 });
    const menu = await screen.findByTestId("kb-matrix-runmenu");
    expect(menu).toHaveAttribute("role", "menu");
    expect(screen.getByTestId("kb-matrix-app-m2").contains(menu)).toBe(false);
    const items = within(menu).getAllByRole("menuitem", { hidden: true });
    expect(items.map((i) => i.querySelector("span")?.textContent)).toEqual([`${m.runmenu_all}…`, `${m.runmenu_stale}…`, `${m.runmenu_force}…`]);
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement).toBe(items[1]));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("kb-matrix-runmenu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(caret));
  });

  it("opens the run confirmation when an item is picked", async () => {
    renderView();
    fireEvent.click(await screen.findByRole("button", { name: m.run_options }), { detail: 0 });
    const menu = await screen.findByTestId("kb-matrix-runmenu");
    fireEvent.click(within(menu).getAllByRole("menuitem", { hidden: true })[2]);
    await waitFor(() => expect(mocks.estimateRun).toHaveBeenCalledWith("mx_test", { kind: "force_all" }));
  });
});
