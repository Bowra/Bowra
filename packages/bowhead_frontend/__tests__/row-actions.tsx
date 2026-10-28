// Shared helper for the row "⋯" menus (components/ui/row-actions-menu.tsx).
import { fireEvent, screen, within } from "@testing-library/react";
import { expect } from "vitest";

/** Open the row menu whose trigger is named `menuName` and return the menu element. */
export async function openRowMenu(menuName: string | RegExp): Promise<HTMLElement> {
  // Use the first when desktop and mobile layouts each render a copy of the same row
  fireEvent.click((await screen.findAllByRole("button", { name: menuName }))[0]);
  return screen.getByRole("menu", { name: menuName });
}

/** Menu item names in display order (danger items last, after the separator). */
export function menuItemNames(menu: HTMLElement): string[] {
  return within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent?.trim() ?? "");
}

/** Names of the danger items (below the separator, red) in display order. */
export function menuDangerNames(menu: HTMLElement): string[] {
  return within(menu)
    .getAllByRole("menuitem")
    .filter((item) => item.className.includes("is-danger"))
    .map((item) => item.textContent?.trim() ?? "");
}

/** Open the row menu and choose `itemName`; asserts the item's danger marker when `danger` is given. */
export async function chooseRowAction(
  menuName: string | RegExp,
  itemName: string | RegExp,
  opts: { danger?: boolean } = {},
): Promise<void> {
  const menu = await openRowMenu(menuName);
  const item = within(menu).getByRole("menuitem", { name: itemName });
  if (opts.danger !== undefined) expect(item.className.includes("is-danger"), `${String(itemName)} danger`).toBe(opts.danger);
  fireEvent.click(item);
}

/**
 * The confirm gate for a destructive action: the dialog appears, the action has not run,
 * cancel closes it without running; `reopen` then opens it again and confirm runs the action once.
 */
export async function expectConfirmGate({
  reopen,
  action,
  dialogName,
  confirmName,
  cancelName = "取消",
}: {
  reopen: () => unknown;
  /** Confirmation-dialog title (accessible name): must identify that confirmation, not another dialog or drawer. */
  dialogName: string | RegExp;
  action: { mock: { calls: unknown[][] } };
  confirmName: string | RegExp;
  cancelName?: string | RegExp;
}): Promise<void> {
  let dialog = await screen.findByRole("dialog", { name: dialogName });
  expect(action.mock.calls.length, "action must wait for the confirm dialog").toBe(0);
  fireEvent.click(within(dialog).getByRole("button", { name: cancelName }));
  expect(screen.queryByRole("dialog", { name: dialogName })).toBeNull();
  expect(action.mock.calls.length, "cancel must not run the action").toBe(0);
  await reopen();
  dialog = await screen.findByRole("dialog", { name: dialogName });
  fireEvent.click(within(dialog).getByRole("button", { name: confirmName }));
}
