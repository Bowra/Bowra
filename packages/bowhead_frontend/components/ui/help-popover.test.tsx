import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

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
