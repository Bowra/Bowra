/**
 * Closing chart “⋯ → Inspect” returns focus to “⋯” (component unification, step 4).
 *
 * The inspection layer records the focused element when Inspect is clicked (chart-block.tsx). The old `⋯` menu performed
 * the action before unmounting items, recording an item that immediately disappeared and leaving focus on body after closing. With
 * useMenu, selection orders focus back to `⋯` → close menu → perform action, so it records `⋯`.
 *
 * Use real ChartBlock, TableActions, ChartInspect and DialogShell; replace only the react-chartjs-2 canvas
 * with a stub (jsdom has no 2d context; real drawing throws, replacing the block with a fallback table and never reaching the Inspect chart header).
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string) => {
    const labels: Record<string, string> = {
      open: "Inspect",
      close: "Close",
      more_actions: "More actions",
      download_png: "PNG",
      download_csv: "CSV",
      "kind.bar": "Bar",
    };
    return labels[key] ?? key;
  },
}));

vi.mock("react-chartjs-2", async () => {
  const { forwardRef } = await import("react");
  return { Chart: forwardRef(function ChartStub() { return <div data-testid="chart-stub" />; }) };
});

import { ChartBlock } from "./chart-block";

const spec: ChartSpec = {
  kind: "bar",
  title: "渠道营业额",
  row_count: 2,
  columns: [
    { key: "c", header: "渠道", type: "categorical" },
    { key: "v", header: "营业额", type: "numeric" },
  ],
  rows: [
    { c: "A", v: 1 },
    { c: "B", v: 2 },
  ],
};

afterEach(() => cleanup());

describe("ChartBlock ⋯ → inspect", () => {
  it("returns focus to `⋯` after the inspect layer closes", async () => {
    render(<ChartBlock spec={spec} />);
    const more = screen.getByRole("button", { name: "More actions" });
    fireEvent.click(more, { detail: 0 });
    const inspect = await screen.findByRole("menuitem", { name: "Inspect" });
    // Keyboard user presses Enter on the menu item: focus is on that item when the click occurs.
    inspect.focus();
    fireEvent.click(inspect);

    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(more));
  });
});
