/**
 * Document matrix step 1 (S5): 导出 Excel in the matrix page header. A secondary action before Run for members,
 * the only header action for viewers, left out on phones; it downloads through the deployment prefix and a refusal
 * shows a toast that names the reason.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ToastContainer } from "@/components/chat/toast-container";
import { setMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixSnapshot } from "@/lib/kb-matrix/types";
import { _clearAllToasts } from "@/lib/use-toast";
import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({ reload: vi.fn(), mobile: false }));

vi.mock("@/lib/kb-matrix/use-is-mobile", () => ({ useIsMobile: () => mocks.mobile }));
vi.mock("@/lib/kb-matrix/use-matrix-poll", () => ({ useMatrixPolling: () => ({ reload: mocks.reload }) }));
vi.mock("./run-banner", () => ({ RunBanners: () => null }));
vi.mock("./matrix-grid", () => ({ MatrixGrid: () => <div data-testid="matrix-grid" /> }));
vi.mock("./matrix-mobile", () => ({ MatrixMobile: () => <div data-testid="matrix-mobile" /> }));

import { MatrixViewM2 } from "./matrix-view-m2";

const m = messagesEn.kbMatrix;

function snapshot(canEdit: boolean): MatrixSnapshot {
  return {
    matrixId: "mx_test",
    title: "Contracts",
    readOnly: false,
    seq: 1,
    canEdit,
    canManage: canEdit,
    runs: [],
    columns: [{ columnId: "c1", name: "Amount", type: "question" }],
    rows: [{ fileId: "kbf_a", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} }],
  };
}

function renderView(snap: MatrixSnapshot) {
  mocks.reload.mockImplementation(async () => setMatrixSnapshot(snap));
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixViewM2 matrixId="mx_test" onBack={vi.fn()} />
      <ToastContainer />
    </NextIntlClientProvider>,
  );
}

function headerButtons(): string[] {
  const head = screen.getByTestId("kb-matrix-head");
  return within(head)
    .getAllByRole("button")
    .map((b) => b.getAttribute("data-testid") ?? b.getAttribute("aria-label") ?? "")
    .filter(Boolean);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.mobile = false;
  window.localStorage.setItem("bowhead.space_id", "ws_a");
});
afterEach(() => {
  cleanup();
  _clearAllToasts();
  setMatrixSnapshot(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.__BOWHEAD_BASE_PATH__ = undefined;
  window.sessionStorage.clear();
  window.localStorage.clear();
});

describe("export to Excel", () => {
  it("sits before Run for members and downloads through the deployment prefix", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    const fetchMock = vi.fn(
      async () =>
        new Response(new Blob(["xlsx"]), { status: 200, headers: { "content-disposition": "attachment; filename*=UTF-8''Contracts.xlsx" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:matrix", revokeObjectURL: () => undefined }));
    const saved: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });
    renderView(snapshot(true));

    const button = await screen.findByRole("button", { name: m.export_excel });
    const order = headerButtons();
    expect(order.indexOf("kb-matrix-export-btn")).toBeGreaterThan(order.indexOf("kb-matrix-graduate-btn"));
    expect(order.indexOf("kb-matrix-export-btn")).toBeLessThan(order.indexOf("kb-matrix-run"));
    expect(button).toHaveClass("kb-btn-secondary");

    fireEvent.click(button);
    await waitFor(() => expect(saved).toEqual(["Contracts.xlsx"]));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/bowhead/api/v1/kb/matrices/mx_test/export.xlsx");
    expect(new Headers(init.headers).get("X-Space-Id")).toBe("ws_a");
    await waitFor(() => expect(screen.getByRole("button", { name: m.export_excel })).toBeEnabled());
  });

  it("is the viewer's only header action", async () => {
    renderView(snapshot(false));
    await screen.findByRole("button", { name: m.export_excel });
    expect(headerButtons().filter((id) => id.startsWith("kb-matrix-") && id !== "kb-matrix-crumb-list")).toEqual(["kb-matrix-export-btn"]);
    expect(screen.queryByRole("button", { name: m.matrix_more })).toBeNull();
    expect(screen.queryByTestId("kb-matrix-run")).toBeNull();
  });

  it("is left out on phones, which have no ⋯ menu", async () => {
    mocks.mobile = true;
    renderView(snapshot(true));
    await screen.findByTestId("matrix-mobile");
    expect(screen.queryByRole("button", { name: m.export_excel })).toBeNull();
    expect(screen.queryByRole("button", { name: m.matrix_more })).toBeNull();
  });

  it("says why the export failed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: { error: { code: "matrix_not_found" } } }), { status: 404 })),
    );
    renderView(snapshot(false));
    fireEvent.click(await screen.findByRole("button", { name: m.export_excel }));
    expect(await screen.findByText(`Couldn't export to Excel. ${m.err_mutation_matrix_changed}`)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("matrix_not_found");
  });
});
