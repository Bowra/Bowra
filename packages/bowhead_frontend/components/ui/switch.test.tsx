/**
 * Switch contract (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 * Native buttons turn Space / Enter into click, which jsdom does not simulate; lock only the click path and aria here.
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Switch } from "./switch";

afterEach(() => {
  cleanup();
});

function Harness({ disabled = false }: { disabled?: boolean }) {
  const [on, setOn] = useState(false);
  return (
    <Switch checked={on} onCheckedChange={setOn} disabled={disabled} className="toggle" aria-label="Notify">
      <span className="knob" />
    </Switch>
  );
}

describe("Switch", () => {
  it("is a type=button role=switch with aria-checked and caller markup", () => {
    render(<Harness />);
    const sw = screen.getByRole("switch", { name: "Notify" });
    expect(sw.tagName).toBe("BUTTON");
    expect(sw.getAttribute("type")).toBe("button");
    expect(sw.getAttribute("aria-checked")).toBe("false");
    // The ui-switch base class precedes caller classes
    expect(sw.className).toBe("ui-switch toggle");
    expect(sw.querySelector(".knob")).not.toBeNull();
  });

  it("toggles aria-checked on click", () => {
    render(<Harness />);
    const sw = screen.getByRole("switch");
    fireEvent.click(sw);
    expect(sw.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(sw);
    expect(sw.getAttribute("aria-checked")).toBe("false");
  });

  it("does not toggle when disabled", () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={onChange} disabled aria-label="x" />);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("caller onClick can cancel the toggle with preventDefault", () => {
    const onChange = vi.fn();
    render(<Switch checked={false} onCheckedChange={onChange} onClick={(e) => e.preventDefault()} aria-label="x" />);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("busy keeps the switch focusable but ignores presses (no focus drop while saving)", () => {
    const onCheckedChange = vi.fn();
    render(<Switch checked busy onCheckedChange={onCheckedChange} aria-label="Learn" />);
    const sw = screen.getByRole("switch", { name: "Learn" });
    sw.focus();
    fireEvent.click(sw);
    expect(onCheckedChange).not.toHaveBeenCalled();
    expect((sw as HTMLButtonElement).disabled).toBe(false);
    expect(sw.getAttribute("aria-busy")).toBe("true");
    expect(sw.getAttribute("aria-disabled")).toBe("true");
    expect(document.activeElement).toBe(sw);
  });

  it("appearance=\"none\" leaves the caller class alone (card-shaped switches keep their own look)", () => {
    render(<Switch checked onCheckedChange={() => {}} appearance="none" className="card" aria-label="Learn" />);
    expect(screen.getByRole("switch").className).toBe("card");
  });

  it("without a caller class the button still carries ui-switch", () => {
    render(<Switch checked={false} onCheckedChange={() => {}} aria-label="Bare" />);
    expect(screen.getByRole("switch").className).toBe("ui-switch");
  });
});
