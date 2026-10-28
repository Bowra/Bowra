/**
 * Sankey honesty (readability audit item 1). Same cases as `test_chart_infer.py` `_not_a_flow_reason`, so the two
 * runtimes agree on which rows are a flow.
 */
import { describe, expect, test } from "vitest";

import { notAFlowReason } from "./flow-shape";
import type { ChartSpec } from "./types";

function sankey(columns: ChartSpec["columns"], rows: NonNullable<ChartSpec["rows"]>): ChartSpec {
  return { kind: "sankey", title: "t", row_count: rows.length, columns, rows };
}

const LINK_COLS: ChartSpec["columns"] = [
  { key: "from", header: "from", type: "categorical" },
  { key: "to", header: "to", type: "categorical" },
  { key: "v", header: "v", type: "numeric" },
];

describe("notAFlowReason", () => {
  test("a synthetic funnel: three unconnected stage -> label bands mixing visits and yuan", () => {
    const spec = sankey(
      [
        { key: "stage", header: "stage", type: "categorical" },
        { key: "value", header: "value", type: "numeric" },
        { key: "label", header: "label", type: "categorical" },
      ],
      [
        { stage: "客流量 (Traffic)", value: 1000000, label: "100%" },
        { stage: "会员客流 (Member Traffic)", value: 20000, label: "2%" },
        { stage: "会员营业额 Member Revenue (¥)", value: 300000000.25, label: "50% of total" },
      ],
    );
    expect(notAFlowReason(spec)).toBe("disjoint_links");
  });

  test("a user path with unmodelled entries (a node emitting more than it receives) is still a flow", () => {
    const spec = sankey(LINK_COLS, [
      { from: "首页", to: "商品页", v: 1000 },
      { from: "商品页", to: "加购", v: 2600 },
      { from: "商品页", to: "离开", v: 900 },
    ]);
    expect(notAFlowReason(spec)).toBeNull();
  });

  test("negative control: a conserving funnel is a flow", () => {
    const spec = sankey(LINK_COLS, [
      { from: "访问", to: "加购", v: 1000 },
      { from: "访问", to: "流失", v: 9000 },
      { from: "加购", to: "下单", v: 600 },
      { from: "加购", to: "放弃", v: 400 },
    ]);
    expect(notAFlowReason(spec)).toBeNull();
  });
});
