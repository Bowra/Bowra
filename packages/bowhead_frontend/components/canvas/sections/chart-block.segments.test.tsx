/**
 * Long horizontal bar charts draw in segments on screen (chart display range, 2026-11-23).
 *
 * Real ChartBlock; only the react-chartjs-2 canvas is stubbed (jsdom has no 2d context) and records what it was given.
 */

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChartSpec } from "@/lib/chart/types";

vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations: () => (key: string, values?: Record<string, unknown>) => {
    if (key === "segment_range") return `第 ${values!.start}–${values!.end} 项，共 ${values!.total} 项`;
    if (key === "segment_previous") return "上一段";
    if (key === "segment_next") return "下一段";
    return key;
  },
}));

const drawn = vi.hoisted(() => ({ labels: [] as unknown[] }));

vi.mock("react-chartjs-2", async () => {
  const { forwardRef } = await import("react");
  return {
    Chart: forwardRef(function ChartStub({ data }: { data: { labels: unknown[] } }) {
      drawn.labels = data.labels;
      return <div data-testid="chart-stub" />;
    }),
  };
});

import { ChartBlock } from "./chart-block";

function ranking(n: number, prefix = "B"): ChartSpec {
  return {
    kind: "hbar",
    title: "品牌营业额",
    row_count: n,
    columns: [
      { key: "brand", header: "品牌", type: "categorical" },
      { key: "revenue", header: "营业额", type: "numeric" },
    ],
    rows: Array.from({ length: n }, (_, index) => ({ brand: `${prefix}${index}`, revenue: 1000 - index })),
    explicit_presentation: { version: 1, mode: "plot", axes: { x: "brand", y: ["revenue"], x_scale: "category" }, group_by: [] },
  };
}

afterEach(() => cleanup());

describe("ChartBlock segments of a long horizontal bar chart", () => {
  it("steps through every category with previous and next", () => {
    render(<ChartBlock spec={ranking(120)} surface="chat" />);
    const previous = screen.getByRole("button", { name: "上一段" });
    const next = screen.getByRole("button", { name: "下一段" });
    expect(screen.getByText("第 1–50 项，共 120 项")).toBeInTheDocument();
    expect(drawn.labels).toHaveLength(50);
    expect(drawn.labels[0]).toBe("B0");
    expect(previous).toBeDisabled();

    fireEvent.click(next);
    expect(screen.getByText("第 51–100 项，共 120 项")).toBeInTheDocument();
    expect(drawn.labels[0]).toBe("B50");

    fireEvent.click(next);
    expect(screen.getByText("第 101–120 项，共 120 项")).toBeInTheDocument();
    expect(drawn.labels).toHaveLength(20);
    expect(next).toBeDisabled();

    fireEvent.click(previous);
    expect(screen.getByText("第 51–100 项，共 120 项")).toBeInTheDocument();
  });

  it("starts a new chart at its first segment", () => {
    const { rerender } = render(<ChartBlock spec={ranking(120)} surface="chat" />);
    fireEvent.click(screen.getByRole("button", { name: "下一段" }));
    expect(drawn.labels[0]).toBe("B50");

    rerender(<ChartBlock spec={ranking(130, "C")} surface="chat" />);
    expect(screen.getByText("第 1–50 项，共 130 项")).toBeInTheDocument();
    expect(drawn.labels[0]).toBe("C0");
  });

  it("keeps the position when the same chart re-renders as a new object", () => {
    const { rerender } = render(<ChartBlock spec={ranking(120)} surface="chat" />);
    fireEvent.click(screen.getByRole("button", { name: "下一段" }));
    rerender(<ChartBlock spec={{ ...ranking(120) }} surface="chat" />);
    expect(screen.getByText("第 51–100 项，共 120 项")).toBeInTheDocument();
  });

  it("draws a chart of up to 50 bars whole, without controls", () => {
    render(<ChartBlock spec={ranking(50)} surface="chat" />);
    expect(drawn.labels).toHaveLength(50);
    expect(screen.queryByRole("button", { name: "下一段" })).not.toBeInTheDocument();
  });
});
