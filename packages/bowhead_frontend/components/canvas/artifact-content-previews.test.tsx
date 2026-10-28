import "@testing-library/jest-dom/vitest";

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArtifactRowsPreview } from "./artifact-content-previews";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

afterEach(() => cleanup());

const preview = {
  family: "table",
  mode: "rows",
  columns: ["market", "revenue"],
  rows: [
    { market: "华东", revenue: 120 },
    { market: "华南", revenue: 90 },
  ],
  shown_rows: 2,
  total_rows: 2,
};

describe("ArtifactRowsPreview row details", () => {
  it("moves focus into details, navigates rows, and restores the source row", async () => {
    const { container } = render(<ArtifactRowsPreview preview={preview} />);
    const rows = Array.from(container.querySelectorAll<HTMLTableRowElement>("tbody tr"));

    fireEvent.click(rows[0]);
    const detail = await screen.findByRole("dialog", { name: /table_row_details/ });
    expect(detail).toHaveAttribute("aria-modal", "false");
    await waitFor(() => expect(screen.getByRole("button", { name: "close" })).toHaveFocus());
    expect(detail).toHaveTextContent("华东");

    fireEvent.click(screen.getByRole("button", { name: "table_next_row" }));
    expect(detail).toHaveTextContent("华南");
    fireEvent.click(screen.getByRole("button", { name: "close" }));
    await waitFor(() => expect(rows[1]).toHaveFocus());
  });

  it("closes row details with Escape and restores focus", async () => {
    const { container } = render(<ArtifactRowsPreview preview={preview} />);
    const firstRow = container.querySelector<HTMLTableRowElement>("tbody tr");
    expect(firstRow).not.toBeNull();

    fireEvent.keyDown(firstRow!, { key: "Enter" });
    await screen.findByRole("dialog", { name: /table_row_details/ });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: /table_row_details/ })).toBeNull();
    await waitFor(() => expect(firstRow).toHaveFocus());
  });

  it("does not close details when a true modal owns Escape", async () => {
    const { container } = render(
      <>
        <ArtifactRowsPreview preview={preview} />
        <div role="dialog" aria-modal="true" aria-label="modal owner" />
      </>,
    );
    const firstRow = container.querySelector<HTMLTableRowElement>("tbody tr");
    fireEvent.click(firstRow!);
    await screen.findByRole("dialog", { name: /table_row_details/ });

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: /table_row_details/ })).toBeInTheDocument();
  });
});
