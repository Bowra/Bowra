import { describe, expect, it } from "vitest";

import { endpointGutterWidth, endpointName, lastFiniteObservation, lineEndpointLabelsPlugin } from "./line-endpoint-labels";

describe("native line endpoint identity", () => {
  it("uses the last actual observation including zero and discloses an earlier period", () => {
    const index = lastFiniteObservation([1234.56, null, 0, null, NaN]);
    expect(index).toBe(2);
    expect(endpointName("Revenue", index, ["Jan", "Feb", "Mar", "Apr", "May"]))
      .toBe("Revenue · Mar");
    expect(endpointName("Cost", 4, ["Jan", "Feb", "Mar", "Apr", "May"])).toBe("Cost");
    expect(lastFiniteObservation([null, undefined, Infinity])).toBe(-1);
  });

  it("reserves measured name space on a phone without shrinking the plot below its recipe floor", () => {
    expect(endpointGutterWidth(344, 150, 24)).toBe(46);
    expect(endpointGutterWidth(344, 150, 120)).toBe(0);
    expect(endpointGutterWidth(720, 300, 60, 700)).toBe(0);
    expect(endpointGutterWidth(720, 300, 60)).toBe(82);
  });
});


it("hides the endpoint before dataset metadata exists, including after replacing data", () => {
  const dataset = { label: "Revenue", data: [12, 21, null], _endpointLabelStyle: { font: "12px sans", ink: "#000", minimumPlotWidth: 160, priority: 0 }, datalabels: { display: true as unknown } };
  const chart = { data: { datasets: [dataset] }, getSortedVisibleDatasetMetas: () => [] };
  const plugin = lineEndpointLabelsPlugin();
  const update = () => (plugin.beforeUpdate as Function)(chart);
  const display = (dataIndex: number) => (dataset.datalabels.display as Function)({ dataIndex });
  update();
  expect(display(0)).toBe(true);
  expect(display(1)).toBe(false);
  dataset.data = [12, 21, 32];
  update();
  expect(display(1)).toBe(true);
  expect(display(2)).toBe(false);
});
