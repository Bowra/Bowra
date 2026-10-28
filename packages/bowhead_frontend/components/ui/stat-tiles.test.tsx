import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatTiles } from "./stat-tiles";

afterEach(() => cleanup());

describe("StatTiles", () => {
  it("renders label / value / secondary as one definition list", () => {
    const { container } = render(
      <StatTiles
        aria-label="Overview"
        tiles={[
          { id: "agents", label: "管理条目", value: 31, secondary: "来自当前团队管理视图", secondaryKind: "note" },
          { id: "reviews", label: "待审批", value: 0, secondary: <a href="/queue">打开审核队列</a>, secondaryKind: "link" },
        ]}
      />,
    );
    const list = container.querySelector("dl")!;
    expect(list).toHaveAttribute("aria-label", "Overview");
    const agents = container.querySelector<HTMLElement>('[data-stat-tile="agents"]')!;
    expect(within(agents).getByText("管理条目").tagName).toBe("DT");
    expect(within(agents).getByText("31").tagName).toBe("DD");
    expect(within(agents).getByText("来自当前团队管理视图")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开审核队列" })).toHaveAttribute("href", "/queue");
  });

  it("shows an unread value as a dash, never zero", () => {
    const { container } = render(<StatTiles busy tiles={[{ id: "blocked", label: "上架检查受阻", value: null }]} />);
    expect(container.querySelector("dl")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText("0")).toBeNull();
  });

  it("follows the report KPI rule from one million: magnitude main line, every digit beneath", () => {
    const { container } = render(<StatTiles tiles={[{ id: "profit", label: "利润", value: 8_559_795 }]} />);
    const value = container.querySelector("[data-stat-swapped]")!;
    expect(value.textContent).toMatch(/^≈/);
    expect(screen.getByText("8,559,795")).toBeInTheDocument();
  });

  it("uses the host's number format when given", () => {
    render(<StatTiles tiles={[{ id: "n", label: "n", value: 1234, format: (v) => `#${v}` }]} />);
    expect(screen.getByText("#1234")).toBeInTheDocument();
  });
});
