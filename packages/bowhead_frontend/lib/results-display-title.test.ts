/**
 * Results titles follow the interface language: the Gateway names the session's code result with fixed Chinese
 * text, so the client shows its own title for the structured `object_type: "code"`.
 */
import { describe, expect, it } from "vitest";

import { resultDisplayTitle } from "./results-workspace-model";

describe("resultDisplayTitle", () => {
  it("replaces the Gateway's Chinese code-result title with the interface-language title", () => {
    expect(resultDisplayTitle({ object_type: "code", title: "代码与运行" }, "Code and runs")).toBe("Code and runs");
  });

  it("keeps report and artifact titles as authored", () => {
    expect(resultDisplayTitle({ object_type: "report", title: "华东 Q3 下滑归因" }, "Code and runs")).toBe("华东 Q3 下滑归因");
    expect(resultDisplayTitle({ object_type: "artifact", title: "sales.xlsx" }, "Code and runs")).toBe("sales.xlsx");
  });
});
