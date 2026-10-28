import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { CanvasObjectFooter } from "./footer";

afterEach(cleanup);

it("writes out why the primary action is disabled and links it to the button", () => {
  renderIntl(<CanvasObjectFooter primary={{ label: "Create", onClick: () => {}, disabled: true, disabledReason: "Missing: instructions" }} />);
  const button = screen.getByRole("button", { name: "Create" });
  // Focusable and announced as unavailable, with its reason; activation does nothing.
  expect(button).toHaveAttribute("aria-disabled", "true");
  expect(button).not.toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("Missing: instructions");
  expect(button).toHaveAccessibleDescription("Missing: instructions");
});

it("places the reason beside the primary button when a leading control is present", () => {
  const confirm = vi.fn();
  const { container } = renderIntl(
    <CanvasObjectFooter
      leading={<label><input type="checkbox" />Reviewed</label>}
      message="3 changed pages left"
      tone="warning"
      primary={{ label: "Confirm and publish", onClick: confirm, disabled: true, disabledReason: "3 changed pages left" }}
    />,
  );
  const status = screen.getByRole("status");
  const button = screen.getByRole("button", { name: "Confirm and publish" });
  // The reason and the button share the action group; the checkbox stays on the leading side.
  expect(status.parentElement).toBe(button.parentElement);
  expect(screen.getByRole("checkbox").closest("span")?.contains(status)).toBe(false);
  expect(container.querySelectorAll('[role="status"]')).toHaveLength(1);
});

it("runs secondary and primary actions", () => {
  const discard = vi.fn();
  const save = vi.fn();
  renderIntl(<CanvasObjectFooter message="Unsaved changes" secondary={[{ label: "Discard", onClick: discard }]} primary={{ label: "Save", onClick: save }} />);
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(discard).toHaveBeenCalledOnce();
  expect(save).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Save" })).not.toHaveAttribute("aria-describedby");
});

it("ignores activation of a blocked primary action", () => {
  const create = vi.fn();
  renderIntl(<CanvasObjectFooter primary={{ label: "Create", onClick: create, disabled: true, disabledReason: "Missing: instructions" }} />);
  fireEvent.click(screen.getByRole("button", { name: "Create" }));
  expect(create).not.toHaveBeenCalled();
});
