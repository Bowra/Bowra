/**
 * SegmentedControl interaction contract (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Segment, SegmentedControl } from "./segmented-control";

afterEach(() => {
  cleanup();
});

function Harness({
  initial = "a" as string | null,
  disabled = [] as string[],
  onChange,
  activation,
  reject = false,
}: {
  initial?: string | null;
  disabled?: string[];
  onChange?: (value: string) => void;
  activation?: "automatic" | "manual";
  /** Caller rejects switching (e.g. Cancel in a leave-confirmation dialog): invoke the callback without changing the value. */
  reject?: boolean;
}) {
  const [value, setValue] = useState<string | null>(initial);
  return (
    <SegmentedControl
      value={value}
      activation={activation}
      aria-label="Mode"
      className="seg"
      onValueChange={(next) => {
        onChange?.(next);
        if (!reject) setValue(next);
      }}
    >
      {["a", "b", "c"].map((v) => (
        <Segment key={v} value={v} disabled={disabled.includes(v)} className={value === v ? "on" : ""}>
          {v.toUpperCase()}
        </Segment>
      ))}
    </SegmentedControl>
  );
}

const radio = (name: string) => screen.getByRole("radio", { name });

describe("SegmentedControl", () => {
  it("renders radiogroup/radio with aria-checked and caller classes", () => {
    render(<Harness initial="b" />);
    const group = screen.getByRole("radiogroup", { name: "Mode" });
    // The ui-segmented base class precedes caller classes.
    expect(group.className).toBe("ui-segmented seg");
    expect(radio("B").getAttribute("aria-checked")).toBe("true");
    expect(radio("A").getAttribute("aria-checked")).toBe("false");
    expect(radio("B").className).toBe("on");
    expect(radio("B").tabIndex).toBe(0);
    expect(radio("A").tabIndex).toBe(-1);
  });

  it("arrow keys move and select, looping", () => {
    render(<Harness />);
    radio("A").focus();
    fireEvent.keyDown(radio("A"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(radio("B"));
    expect(radio("B").getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(radio("B"), { key: "ArrowRight" });
    fireEvent.keyDown(radio("C"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(radio("A"));
    expect(radio("A").getAttribute("aria-checked")).toBe("true");
  });

  it("Home / End select the first / last enabled segment, skipping disabled", () => {
    render(<Harness initial="b" disabled={["a"]} />);
    radio("B").focus();
    fireEvent.keyDown(radio("B"), { key: "Home" });
    expect(document.activeElement).toBe(radio("B"));
    fireEvent.keyDown(radio("B"), { key: "End" });
    expect(document.activeElement).toBe(radio("C"));
    expect(radio("C").getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(radio("C"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(radio("B"));
    expect(radio("B").getAttribute("aria-checked")).toBe("true");
  });

  it('orientation="both" moves with all four arrow keys (card-style radios)', () => {
    function Both() {
      const [value, setValue] = useState("a");
      return (
        <SegmentedControl value={value} onValueChange={setValue} orientation="both" aria-label="Cards">
          <Segment value="a">A</Segment>
          <Segment value="b">B</Segment>
        </SegmentedControl>
      );
    }
    render(<Both />);
    radio("A").focus();
    fireEvent.keyDown(radio("A"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(radio("B"));
    expect(radio("B").getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(radio("B"), { key: "ArrowLeft" });
    expect(radio("A").getAttribute("aria-checked")).toBe("true");
  });

  it("with no selection, the first enabled segment is the tab stop but focus does not select it", () => {
    const onChange = vi.fn();
    render(<Harness initial={null} disabled={["a"]} onChange={onChange} />);
    expect(radio("B").tabIndex).toBe(0);
    radio("B").focus();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(radio("C"));
    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("busy ignores selection but keeps the segments focusable (no focus drop while saving)", () => {
    const onValueChange = vi.fn();
    render(
      <SegmentedControl value="a" onValueChange={onValueChange} busy aria-label="Audience">
        <Segment value="a">A</Segment>
        <Segment value="b">B</Segment>
      </SegmentedControl>,
    );
    const b = screen.getByRole("radio", { name: "B" });
    b.focus();
    fireEvent.click(b);
    expect(onValueChange).not.toHaveBeenCalled();
    expect((b as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole("radiogroup", { name: "Audience" }).getAttribute("aria-busy")).toBe("true");
    expect(document.activeElement).toBe(b);
  });

  it("appearance picks the refresh base class: cards for card radios, none adds nothing", () => {
    const { rerender } = render(
      <SegmentedControl value="a" onValueChange={() => {}} appearance="cards" className="grid" aria-label="Cards">
        <Segment value="a">A</Segment>
      </SegmentedControl>,
    );
    expect(screen.getByRole("radiogroup", { name: "Cards" }).className).toBe("ui-radio-cards grid");
    rerender(
      <SegmentedControl value="a" onValueChange={() => {}} appearance="none" className="grid" aria-label="Cards">
        <Segment value="a">A</Segment>
      </SegmentedControl>,
    );
    expect(screen.getByRole("radiogroup", { name: "Cards" }).className).toBe("grid");
  });

  it("manual activation: arrows only move focus; Space/Enter/click select", () => {
    const onChange = vi.fn();
    render(<Harness activation="manual" onChange={onChange} />);
    radio("A").focus();
    fireEvent.keyDown(radio("A"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(radio("B"));
    fireEvent.keyDown(radio("B"), { key: "End" });
    expect(document.activeElement).toBe(radio("C"));
    expect(onChange).not.toHaveBeenCalled();
    expect(radio("A").getAttribute("aria-checked")).toBe("true");
    fireEvent.click(radio("C"));
    expect(onChange).toHaveBeenCalledWith("c");
    expect(radio("C").getAttribute("aria-checked")).toBe("true");
  });

  it("the Tab stop returns to the selected item when focus leaves after a rejected change", () => {
    render(
      <>
        <Harness reject />
        <button type="button">After</button>
      </>,
    );
    radio("A").focus();
    fireEvent.keyDown(radio("A"), { key: "ArrowRight" });
    expect(radio("A").getAttribute("aria-checked")).toBe("true");
    fireEvent.blur(radio("B"), { relatedTarget: screen.getByRole("button", { name: "After" }) });
    expect(radio("A").tabIndex).toBe(0);
    expect(radio("B").tabIndex).toBe(-1);
  });

  it("Home selects the first item even when focus is already on it (no selection yet)", () => {
    const onChange = vi.fn();
    render(<Harness initial={null} onChange={onChange} />);
    radio("A").focus();
    fireEvent.keyDown(radio("A"), { key: "Home" });
    expect(onChange).toHaveBeenCalledWith("a");
  });
});
