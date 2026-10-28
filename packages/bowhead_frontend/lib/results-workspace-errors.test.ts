import { describe, expect, it } from "vitest";

import {
  classifyResultsError,
  classifyResultsResponse,
  ResultsWorkspaceError,
} from "./results-workspace-errors";

describe("Results Workspace error contract", () => {
  it.each([
    [403, "unavailable"],
    [404, "unavailable"],
    [410, "unavailable"],
    [413, "too_large"],
    [415, "unsupported"],
    [422, "corrupt"],
    [503, "unknown"],
    [418, "unknown"],
  ] as const)("classifies HTTP %s as %s", (status, expected) => {
    expect(classifyResultsResponse(status)).toBe(expected);
  });

  it("does not expose arbitrary server or client error text", () => {
    expect(classifyResultsError(new ResultsWorkspaceError("unavailable")))
      .toBe("unavailable");
    expect(classifyResultsError(new Error("/private/storage/customer.csv")))
      .toBe("unknown");
    expect(classifyResultsError(new TypeError("Failed to fetch")))
      .toBe("network");
    expect(classifyResultsError(new SyntaxError("private response body")))
      .toBe("corrupt");
  });
});
