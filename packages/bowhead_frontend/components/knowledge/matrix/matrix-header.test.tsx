/**
 * Matrix page header (document matrix step 1, S2): breadcrumb, inline rename and the ⋯ menu.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";

import { MatrixHeader } from "./matrix-header";

const m = messagesEn.kbMatrix;

function renderHeader(over: Partial<Parameters<typeof MatrixHeader>[0]> = {}) {
  const props = {
    title: "Supplier contracts",
    meta: "Selected files · 6 files · 6 columns",
    canEdit: true,
    canManage: true,
    onHome: vi.fn(),
    onBack: vi.fn(),
    onRename: vi.fn(async () => true),
    onDelete: vi.fn(),
    ...over,
  };
  render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <MatrixHeader {...props}>
        <button type="button">Run</button>
      </MatrixHeader>
    </NextIntlClientProvider>,
  );
  return props;
}

afterEach(cleanup);

describe("matrix header", () => {
  it("leads with the breadcrumb back to the knowledge base and the matrix list", () => {
    const props = renderHeader();
    const crumb = screen.getByRole("navigation", { name: m.crumb_label });
    fireEvent.click(within(crumb).getByRole("button", { name: m.crumb_kb }));
    fireEvent.click(within(crumb).getByRole("button", { name: m.crumb_matrices }));
    expect(props.onHome).toHaveBeenCalledTimes(1);
    expect(props.onBack).toHaveBeenCalledTimes(1);
  });

  it("renames inline: Enter saves the trimmed name, Esc keeps the current one", async () => {
    const props = renderHeader();
    fireEvent.click(screen.getByRole("button", { name: "Rename Supplier contracts" }));
    const input = screen.getByRole("textbox", { name: m.rename_input });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "  Vendor contracts  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(props.onRename).toHaveBeenCalledWith("Vendor contracts"));
    await waitFor(() => expect(screen.queryByRole("textbox", { name: m.rename_input })).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Rename Supplier contracts" }));
    const again = screen.getByRole("textbox", { name: m.rename_input });
    fireEvent.change(again, { target: { value: "Something else" } });
    fireEvent.keyDown(again, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: m.rename_input })).toBeNull();
    expect(props.onRename).toHaveBeenCalledTimes(1);
  });

  it("keeps an empty name in the field with the reason instead of saving it", () => {
    const props = renderHeader();
    fireEvent.click(screen.getByRole("button", { name: "Rename Supplier contracts" }));
    const input = screen.getByRole("textbox", { name: m.rename_input });
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByRole("alert")).toHaveTextContent(m.rename_empty);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(props.onRename).not.toHaveBeenCalled();
  });

  it("offers Rename and Delete Matrix… in ⋯, with delete explained when the viewer may not delete", async () => {
    const props = renderHeader({ canManage: false });
    fireEvent.click(screen.getByRole("button", { name: m.matrix_more }));
    const del = await screen.findByRole("menuitem", { name: `${m.menu_delete_matrix}…` });
    expect(del).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText(m.delete_matrix_reason)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("menuitem", { name: m.menu_rename }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: m.rename_input })).toHaveFocus());
    expect(props.onDelete).not.toHaveBeenCalled();
  });

  it("is read-only without edit rights (phones and viewers)", () => {
    renderHeader({ canEdit: false });
    expect(screen.queryByRole("button", { name: "Rename Supplier contracts" })).toBeNull();
    expect(screen.queryByRole("button", { name: m.matrix_more })).toBeNull();
    expect(screen.getByRole("heading", { name: "Supplier contracts" })).toBeInTheDocument();
  });

  it("shows the page actions the caller passes without edit rights, and none when it passes none (S5)", () => {
    renderHeader({ canEdit: false });
    // The caller decides what a viewer may use (export); ⋯ stays hidden.
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    cleanup();
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <MatrixHeader title="T" meta="" canEdit={false} canManage={false} onHome={vi.fn()} onBack={vi.fn()} onRename={vi.fn(async () => true)} onDelete={vi.fn()}>
          {false}
        </MatrixHeader>
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("kb-matrix-head").querySelector(".acts")).toBeNull();
  });

  it("badges an only-me matrix and offers its creator the visibility change after Rename (S8)", async () => {
    const props = renderHeader({ visibility: "private", canChangeVisibility: true, onChangeVisibility: vi.fn() });
    expect(screen.getByTestId("kb-matrix-private-badge")).toHaveTextContent(m.visibility_badge_private);
    fireEvent.click(screen.getByRole("button", { name: m.matrix_more }));
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((i) => i.textContent)).toEqual([m.menu_rename, m.menu_visibility_space, `${m.menu_delete_matrix}…`]);
    fireEvent.click(screen.getByRole("menuitem", { name: m.menu_visibility_space }));
    expect(props.onChangeVisibility).toHaveBeenCalledTimes(1);
    cleanup();

    renderHeader({ visibility: "space", canChangeVisibility: false, onChangeVisibility: vi.fn() });
    expect(screen.queryByTestId("kb-matrix-private-badge")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: m.matrix_more }));
    await screen.findByRole("menuitem", { name: m.menu_rename });
    expect(screen.queryByRole("menuitem", { name: m.menu_visibility_private })).toBeNull();
  });
});
