import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { HelpPopover } from "./help-popover";

describe("HelpPopover (decision 21)", () => {
  afterEach(cleanup);

  it("opens a named, non-modal popover on press and closes with Escape back on the question mark", () => {
    render(<HelpPopover label="About held out" title="What held out means"><p>Coach can't see it.</p></HelpPopover>);
    const trigger = screen.getByRole("button", { name: "About held out" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(trigger.getAttribute("title")).toBeNull();

    fireEvent.click(trigger);
    const popover = screen.getByRole("dialog", { name: "What held out means" });
    expect(popover.getAttribute("aria-modal")).toBe("false");
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(trigger.getAttribute("aria-controls")).toBe(popover.id);
    expect(popover.textContent).toContain("Coach can't see it.");

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("closes on a press outside or on the question mark again, and keeps one open at a time", () => {
    render(<>
      <HelpPopover label="First">One</HelpPopover>
      <HelpPopover label="Second">Two</HelpPopover>
      <button type="button">Elsewhere</button>
    </>);
    fireEvent.click(screen.getByRole("button", { name: "First" }));
    expect(screen.getByRole("dialog", { name: "First" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Second" }));
    expect(screen.queryByRole("dialog", { name: "First" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Second" })).toBeTruthy();

    fireEvent.pointerDown(screen.getByRole("button", { name: "Elsewhere" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "First" }));
    fireEvent.click(screen.getByRole("button", { name: "First" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

it("keeps a focused preview pinned after pressing and does not reopen on Escape focus return", () => {
  const { unmount } = render(<HelpPopover preview label="Field">Meaning</HelpPopover>);
  const trigger = screen.getByRole("button", { name: "Field" });
  act(() => trigger.focus());
  expect(screen.getByRole("dialog")).toBeTruthy();
  fireEvent.click(trigger);
  fireEvent.pointerLeave(trigger);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(trigger);
  unmount();
});


it("keeps a keyboard preview open when only the pointer leaves", () => {
  vi.useFakeTimers();
  const view = render(<HelpPopover preview label="Keyboard field">Meaning</HelpPopover>);
  const trigger = screen.getByRole("button", { name: "Keyboard field" });
  act(() => trigger.focus());
  fireEvent.pointerLeave(trigger);
  act(() => vi.advanceTimersByTime(200));
  expect(screen.getByRole("dialog")).toBeTruthy();
  view.unmount();
  vi.useRealTimers();
});
