import { cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { makeCiFormat } from "./agent-ci-shared";
import { ComparisonOverviewLine } from "./agent-version-validation";

describe("ComparisonOverviewLine (eval credibility R15)", () => {
  afterEach(cleanup);

  it("states the cases run and each version's pass rate", () => {
    renderIntl(
      <ComparisonOverviewLine
        overview={{ cases: 12, candidate_pass_rate: 0.75, live_pass_rate: 0.8333, newly_failing: 2, newly_passing: 1 }}
        live={3}
        format={makeCiFormat("zh-CN")}
      />,
    );
    expect(screen.getByText("运行了 12 条用例")).toBeTruthy();
    expect(screen.getByText("这次修改 通过率 75%")).toBeTruthy();
    expect(screen.getByText("v3 通过率 83%")).toBeTruthy();
  });

  it("says a side has no verdict instead of showing 0%", () => {
    renderIntl(
      <ComparisonOverviewLine
        overview={{ cases: 3, candidate_pass_rate: 1, live_pass_rate: null, newly_failing: 0, newly_passing: 0 }}
        live={3}
        format={makeCiFormat("zh-CN")}
      />,
    );
    expect(screen.getByText("v3：暂无结论")).toBeTruthy();
    expect(screen.queryByText("v3 通过率 0%")).toBeNull();
    expect(screen.getByText("这次修改 通过率 100%")).toBeTruthy();
  });

  it("pluralizes case counts and says how many cases are left out of the pass rates", () => {
    const line = (cases: number, undecided: number) => renderIntl(
      <ComparisonOverviewLine
        overview={{ cases, candidate_pass_rate: 1, live_pass_rate: 1, newly_failing: 0, newly_passing: 0 }}
        live={3}
        format={makeCiFormat("en-US")}
        undecided={undecided}
      />,
      { locale: "en-US" },
    );
    line(1, 1);
    expect(screen.getByText("1 case run")).toBeTruthy();
    expect(screen.getByText("1 case has no result and is not counted in pass rates")).toBeTruthy();
    cleanup();
    line(12, 0);
    expect(screen.getByText("12 cases run")).toBeTruthy();
    expect(screen.queryByText(/no result/)).toBeNull();
  });
});
