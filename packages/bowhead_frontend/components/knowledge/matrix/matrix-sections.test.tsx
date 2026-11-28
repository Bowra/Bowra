/**
 * Matrix list as a table (document matrix step 1, S2): one row per matrix, one primary action.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { MatrixListItem } from "@/lib/kb-matrix/snapshot-api";
import messagesEn from "@/messages/en-US.json";

import { MatrixSection } from "./matrix-sections";

const m = messagesEn.kbMatrix;

const ITEM: MatrixListItem = {
  matrixId: "mx_1",
  name: "Supplier contracts",
  templateDocTypeId: null,
  filesCount: 6,
  columnsCount: 4,
  statusSummary: { done: 20, failed: 1, needs_review: 0, running: 0, queued: 0 },
  lastRunAt: null,
  createdBy: "u_1",
  createdByName: "Chen Siyuan",
  createdAt: "2026-11-20T08:00:00Z",
};

afterEach(cleanup);

describe("matrix list", () => {
  it("shows name, row source, files × columns, last run and creator, and opens a matrix", () => {
    const onOpen = vi.fn();
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <MatrixSection matrices={[ITEM]} loading={false} canEdit onOpen={onOpen} onNew={vi.fn()} />
      </NextIntlClientProvider>,
    );
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      m.list_col_name,
      m.list_col_source,
      m.list_col_size,
      m.list_col_last_run,
      m.list_col_creator,
    ]);
    const row = screen.getByTestId("kb-matrix-mx-mx_1");
    expect(within(row).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "Supplier contracts1 failed",
      m.list_source_selected,
      "6 × 4",
      m.list_never_run,
      "Chen Siyuan",
    ]);
    fireEvent.click(within(row).getByRole("link", { name: "Supplier contracts" }));
    expect(onOpen).toHaveBeenCalledWith("mx_1");
    expect(screen.getAllByRole("button").filter((b) => b.className.includes("kb-btn-primary"))).toHaveLength(1);
    expect(screen.queryByTestId("kb-matrix-private-badge")).toBeNull();
  });

  it("marks only-me matrices with a badge (step 1 S8)", () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <MatrixSection
          matrices={[ITEM, { ...ITEM, matrixId: "mx_2", name: "My notes", visibility: "private" }]}
          loading={false}
          canEdit
          onOpen={vi.fn()}
          onNew={vi.fn()}
        />
      </NextIntlClientProvider>,
    );
    expect(within(screen.getByTestId("kb-matrix-mx-mx_1")).queryByTestId("kb-matrix-private-badge")).toBeNull();
    const badge = within(screen.getByTestId("kb-matrix-mx-mx_2")).getByTestId("kb-matrix-private-badge");
    expect(badge).toHaveTextContent(m.visibility_badge_private);
    expect(badge).toHaveAttribute("title", m.visibility_badge_private_desc);
  });
});
