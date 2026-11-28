/**
 * Side-panel history (document matrix step 1, S4): the latest change from the snapshot while the list loads, then
 * the list newest first with who and what (the AI, the system, a person, or "a member" when the name is unknown),
 * bulk changes marked, a retry on failure, a reload when the cell changes, and the history of failed cells too.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatrixCellEvent, MatrixCellVM, MatrixColumnVM, MatrixRowVM } from "@/lib/kb-matrix/types";
import messagesEn from "@/messages/en-US.json";

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  useKbChunkContent: () => ({ status: "idle" }),
}));

import { SidePanel } from "./side-panel";

const m = messagesEn.kbMatrix;
const ROW: MatrixRowVM = { fileId: "kbf_1", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} };
const COL: MatrixColumnVM = { columnId: "c1", name: "Amount", type: "question", question: "What is the contract amount, tax included?" };
const AT = new Date(Date.now() - 5 * 60_000).toISOString();
const CELL: MatrixCellVM = { status: "done", value: "1,280,000", variants: [], edited: true, verified: true, seq: 7 };

const ev = (over: Partial<MatrixCellEvent>): MatrixCellEvent => ({ eventId: `e_${Math.random()}`, kind: "edited", actorName: "Mei", at: AT, runId: null, batchId: null, ...over });

function renderPanel(loadHistory: () => Promise<{ items: MatrixCellEvent[]; more: boolean }>, cell: MatrixCellVM = CELL) {
  const props = { row: ROW, col: COL, canEdit: true, saving: false, onClose: vi.fn(), onSaveValue: vi.fn(), onMarkVerified: vi.fn(), loadHistory };
  const view = render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn} timeZone="UTC">
      <SidePanel {...props} cell={cell} />
    </NextIntlClientProvider>,
  );
  const rerender = (next: MatrixCellVM) =>
    view.rerender(
      <NextIntlClientProvider locale="en-US" messages={messagesEn} timeZone="UTC">
        <SidePanel {...props} cell={next} />
      </NextIntlClientProvider>,
    );
  return { rerender };
}

afterEach(cleanup);

describe("side panel history", () => {
  it("shows the full question, then lists who changed the cell, newest first", async () => {
    const load = vi.fn().mockResolvedValue({
      items: [
        ev({ kind: "verified", actorName: "Jun", batchId: "mxbat_1" }),
        ev({ kind: "edited", actorName: null }),
        ev({ kind: "edit_superseded", actorName: null, runId: "mxrun_2" }),
        ev({ kind: "auto_retried", actorName: null, runId: "mxrun_1" }),
        ev({ kind: "ai_value", actorName: null, runId: "mxrun_1" }),
      ],
      more: true,
    });
    renderPanel(load);
    expect(screen.getByText(COL.question as string)).toBeInTheDocument();
    const history = screen.getByTestId("kb-matrix-history");
    await waitFor(() => expect(within(history).getAllByRole("listitem")).toHaveLength(5));
    const lines = within(history).getAllByRole("listitem").map((li) => li.textContent?.split(" · ")[0]);
    expect(lines).toEqual([
      `Jun ${m.history_verified_bulk}`,
      `${m.history_someone} ${m.history_edited}`,
      `${m.history_ai} ${m.history_edit_superseded}`,
      `${m.history_system} ${m.history_auto_retried}`,
      `${m.history_ai} ${m.history_ai_value}`,
    ]);
    expect(within(history).getAllByRole("listitem")[0].querySelector("time")).toHaveAttribute("dateTime");
    expect(history).toHaveTextContent("Showing the latest 5 entries only.");
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("shows the snapshot's latest change while the list loads, and says when there is no history", async () => {
    let resolve: (v: { items: MatrixCellEvent[]; more: boolean }) => void = () => {};
    const load = vi.fn(() => new Promise<{ items: MatrixCellEvent[]; more: boolean }>((r) => (resolve = r)));
    renderPanel(load, { ...CELL, lastEvent: { kind: "verified", actorName: "Mei", at: AT } });
    const history = screen.getByTestId("kb-matrix-history");
    expect(within(history).getByRole("listitem")).toHaveTextContent(`Mei ${m.history_verified}`);
    resolve({ items: [], more: false });
    await waitFor(() => expect(history).toHaveTextContent(m.history_empty));
  });

  it("offers a retry when the list cannot be loaded, and reloads when the cell changes", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ items: [ev({ kind: "verified" })], more: false });
    const { rerender } = renderPanel(load);
    const history = screen.getByTestId("kb-matrix-history");
    await waitFor(() => expect(history).toHaveTextContent(m.history_error));
    fireEvent.click(within(history).getByRole("button", { name: m.history_retry }));
    await waitFor(() => expect(within(history).getByRole("listitem")).toHaveTextContent(`Mei ${m.history_verified}`));
    expect(load).toHaveBeenCalledTimes(2);
    rerender({ ...CELL, verified: false, seq: 8 });
    await waitFor(() => expect(load).toHaveBeenCalledTimes(3));
  });

  it("keeps the history of failed cells", async () => {
    const load = vi.fn().mockResolvedValue({ items: [ev({ kind: "auto_retried", actorName: null })], more: false });
    renderPanel(load, { status: "failed", value: "", variants: [], edited: false, verified: false, failReason: "timeout", seq: 3 });
    expect(screen.getByTestId("kb-matrix-panel-failed")).toBeInTheDocument();
    await waitFor(() => expect(within(screen.getByTestId("kb-matrix-history")).getByRole("listitem")).toHaveTextContent(m.history_auto_retried));
  });

  it("has no history section without a loader (template views)", () => {
    const { row, col } = { row: ROW, col: COL };
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <SidePanel row={row} col={col} cell={CELL} canEdit saving={false} onClose={vi.fn()} onSaveValue={vi.fn()} onMarkVerified={vi.fn()} />
      </NextIntlClientProvider>,
    );
    expect(screen.queryByTestId("kb-matrix-history")).toBeNull();
  });
});
