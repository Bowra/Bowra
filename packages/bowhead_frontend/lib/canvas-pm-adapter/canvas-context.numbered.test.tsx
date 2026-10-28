/**
 * numberedSources debt 1 regression lock (citation-audit M0-1):
 * [N] popover/panel data must resolve to the current report's sources section, skipping
 * sources in superseded (folded historical) reports, using the same rule as report-view's
 * `numberedSourcesLength` decoration gate. Otherwise, on a canvas with multiple reports,
 * clicking the new report's [3] opens the old report's third source (wrong references are worse than no display).
 */

import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

afterEach(cleanup);
import { CanvasContextProvider, useCanvasContext } from "./canvas-context";

import type { ReportSection } from "@/lib/types";

function Probe() {
  const { numberedSources } = useCanvasContext();
  const first = numberedSources[0] as { label?: string } | undefined;
  return (
    <div data-testid="probe">
      {first?.label ?? "none"}·{numberedSources.length}
    </div>
  );
}

describe("numberedSources (M0-1 债1)", () => {
  it("skips superseded (prior-report) sources sections", () => {
    const sections = [
      {
        id: "srcs_old",
        kind: "sources",
        label: "数据来源",
        superseded: true,
        entries: [{ label: "OLD", artifact_id: "art_old" }],
      },
      {
        id: "srcs_cur",
        kind: "sources",
        label: "数据来源",
        entries: [
          { label: "CUR", artifact_id: "art_cur" },
          { label: "CUR2", artifact_id: "art_cur2" },
        ],
      },
    ] as unknown as ReportSection[];

    const { getByTestId } = renderIntl(
      <CanvasContextProvider sections={sections} sourceRefs={[]}>
        <Probe />
      </CanvasContextProvider>,
    );
    expect(getByTestId("probe").textContent).toBe("CUR·2");
  });

  it("still resolves a lone (non-superseded) sources section", () => {
    const sections = [
      {
        id: "srcs",
        kind: "sources",
        label: "数据来源",
        entries: [{ label: "ONLY", artifact_id: "art_1" }],
      },
    ] as unknown as ReportSection[];

    const { getByTestId } = renderIntl(
      <CanvasContextProvider sections={sections} sourceRefs={[]}>
        <Probe />
      </CanvasContextProvider>,
    );
    expect(getByTestId("probe").textContent).toBe("ONLY·1");
  });
});
