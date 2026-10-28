import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import type { ChartSpec } from "@/lib/chart/types";
import { AgentDashboardChart } from "./agent-dashboard-chart";

vi.mock("@/components/canvas/sections/chart-block", () => ({ ChartBlock: (props: { spec: ChartSpec }) => <pre data-testid="native-chart">{JSON.stringify(props)}</pre> }));

describe("Agent overview native chart integration", () => {
  it("passes exact management aggregates to the shared renderer in its embed frame", async () => {
    renderIntl(<AgentDashboardChart title="Domain coverage" titleId="ad-domains-title" rows={[{ id: "finance", label: "Finance", count: 4 }, { id: "people", label: "People & organization", chartLabel: "People & org.", count: 2 }]} />);
    const props = JSON.parse((await screen.findByTestId("native-chart")).textContent!);
    expect(props.surface).toBe("canvas");
    // The panel owns the title; the chart draws no title row and renders the exact-value disclosure itself.
    expect(props.chrome).toBe("embed");
    expect(props.labelledBy).toBe("ad-domains-title");
    expect(props.spec).toMatchObject({ kind: "hbar", show_data_labels: true, row_count: 2, rows: [{ category: "Finance", entries: 4 }, { category: "People & org.", entries: 2 }] });
    // Bars use short names; the exact-value table gets the full ones, keyed by the short name.
    expect(props.exactValueLabels).toEqual({ Finance: "Finance", "People & org.": "People & organization" });
    expect(screen.queryByText("查看精确数值")).toBeNull();
  });
});
