/**
 * Tabs interaction contract (component unification P1, step 6, docs/design/组件统一-技术设计.md).
 *
 * Arrow-key focus movement uses floating-ui Composite; Home/End, auto-activation only during keyboard navigation, and the current roving item
 * following the selected value are added by composite-selection.tsx itself. Lock each behavior here.
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Tab, TabList, TabPanel, Tabs } from "./tabs";

afterEach(() => {
  cleanup();
});

function Harness({
  initial = "a",
  disabled = [] as string[],
  onChange,
  withPanels = true,
}: {
  initial?: string;
  disabled?: string[];
  onChange?: (value: string) => void;
  withPanels?: boolean;
}) {
  const [value, setValue] = useState(initial);
  return (
    <Tabs
      value={value}
      idBase="t"
      onValueChange={(next) => {
        onChange?.(next);
        setValue(next);
      }}
    >
      <TabList aria-label="Views" className="my-tabs">
        {["a", "b", "c", "d"].map((v) => (
          <Tab key={v} value={v} disabled={disabled.includes(v)} className={value === v ? "on" : ""}>
            {v.toUpperCase()}
          </Tab>
        ))}
      </TabList>
      {withPanels ? (
        <>
          <TabPanel value="a">Panel A</TabPanel>
          <TabPanel value="b">Panel B</TabPanel>
          <TabPanel value="c">Panel C</TabPanel>
          <TabPanel value="d">Panel D</TabPanel>
        </>
      ) : null}
    </Tabs>
  );
}

const tab = (name: string) => screen.getByRole("tab", { name });

describe("Tabs", () => {
  it("renders tablist/tab/tabpanel roles and links tab to panel", () => {
    render(<Harness />);
    const list = screen.getByRole("tablist", { name: "Views" });
    expect(list.className).toBe("my-tabs");
    expect(tab("A").getAttribute("aria-selected")).toBe("true");
    expect(tab("B").getAttribute("aria-selected")).toBe("false");
    expect(tab("A").className).toBe("on");
    const panel = screen.getByRole("tabpanel");
    expect(panel.textContent).toBe("Panel A");
    expect(tab("A").getAttribute("aria-controls")).toBe(panel.id);
    expect(panel.getAttribute("aria-labelledby")).toBe(tab("A").id);
  });

  it("omits aria-controls when no panel is mounted", () => {
    render(<Harness withPanels={false} />);
    expect(tab("A").hasAttribute("aria-controls")).toBe(false);
  });

  it("keeps a single tab stop on the selected tab", () => {
    render(<Harness initial="c" />);
    expect(tab("C").tabIndex).toBe(0);
    expect(tab("A").tabIndex).toBe(-1);
    expect(tab("B").tabIndex).toBe(-1);
  });

  it("ArrowRight / ArrowLeft move focus and activate, looping at the ends", () => {
    render(<Harness />);
    tab("A").focus();
    fireEvent.keyDown(tab("A"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab("B"));
    expect(tab("B").getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("tabpanel").textContent).toBe("Panel B");
    expect(tab("B").tabIndex).toBe(0);
    fireEvent.keyDown(tab("B"), { key: "ArrowLeft" });
    fireEvent.keyDown(tab("A"), { key: "ArrowLeft" });
    expect(document.activeElement).toBe(tab("D"));
    expect(tab("D").getAttribute("aria-selected")).toBe("true");
  });

  it("Home / End jump to the first / last enabled tab", () => {
    render(<Harness initial="b" disabled={["d"]} />);
    tab("B").focus();
    fireEvent.keyDown(tab("B"), { key: "End" });
    expect(document.activeElement).toBe(tab("C"));
    expect(tab("C").getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(tab("C"), { key: "Home" });
    expect(document.activeElement).toBe(tab("A"));
    expect(tab("A").getAttribute("aria-selected")).toBe("true");
  });

  it("skips disabled tabs", () => {
    render(<Harness disabled={["b"]} />);
    tab("A").focus();
    fireEvent.keyDown(tab("A"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(tab("C"));
    expect(tab("C").getAttribute("aria-selected")).toBe("true");
  });

  it("does not activate on plain focus, only on keyboard navigation or click", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    tab("C").focus();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(tab("C"));
    expect(onChange).toHaveBeenCalledWith("c");
  });

  it("clicking the selected tab still reports it, like the onClick it replaces", () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    fireEvent.click(tab("A"));
    expect(onChange).toHaveBeenCalledWith("a");
  });

  it("moves the tab stop when the value changes from outside", () => {
    function External() {
      const [value, setValue] = useState("a");
      return (
        <>
          <button type="button" onClick={() => setValue("c")}>
            Go C
          </button>
          <Tabs value={value} onValueChange={setValue}>
            <TabList>
              <Tab value="a">A</Tab>
              <Tab value="b">B</Tab>
              <Tab value="c">C</Tab>
            </TabList>
          </Tabs>
        </>
      );
    }
    render(<External />);
    fireEvent.click(screen.getByRole("button", { name: "Go C" }));
    expect(tab("C").tabIndex).toBe(0);
    expect(tab("A").tabIndex).toBe(-1);
  });

  it("appearance adds the refresh base classes before the caller class; omitted adds none", () => {
    const { rerender } = render(
      <Tabs value="a" onValueChange={() => {}}>
        <TabList appearance="underline" className="x-tabs" aria-label="U">
          <Tab value="a">A</Tab>
        </TabList>
      </Tabs>,
    );
    expect(screen.getByRole("tablist", { name: "U" }).className).toBe("ui-tabs ui-tabs--underline x-tabs");
    rerender(
      <Tabs value="a" onValueChange={() => {}}>
        <TabList appearance="segmented" aria-label="U">
          <Tab value="a">A</Tab>
        </TabList>
      </Tabs>,
    );
    expect(screen.getByRole("tablist", { name: "U" }).className).toBe("ui-tabs ui-tabs--segmented");
    rerender(
      <Tabs value="a" onValueChange={() => {}}>
        <TabList className="x-tabs" aria-label="U">
          <Tab value="a">A</Tab>
        </TabList>
      </Tabs>,
    );
    expect(screen.getByRole("tablist", { name: "U" }).className).toBe("x-tabs");
  });
});
