import { describe, expect, it } from "vitest";
import { agentChartDetailHref, agentChartFilters, agentChartsReturnHref } from "./agent-charts-navigation";

describe("agent chart navigation", () => {
  it("round trips category and chart through the existing detail route", () => {
    const detail = agentChartDetailHref("osa_finance", "trending", "finance");
    expect(detail).toContain("id=osa_finance");
    expect(agentChartsReturnHref(detail.split("?")[1])).toBe("/agents/charts/?chart=trending&domain=finance");
  });

  it("keeps arbitrary return URLs and unknown facets out of navigation", () => {
    expect(agentChartsReturnHref("return_page=https://outside.test/&chart=popular")).toBeNull();
    expect(agentChartsReturnHref("return_page=charts&chart=arbitrary&domain=private-secret")).toBe("/agents/charts/?chart=popular");
    expect(agentChartFilters("chart=updated&domain=finance")).toEqual({ view: "updated", domain: "finance" });
  });
});
