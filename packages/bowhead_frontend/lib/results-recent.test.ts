import { beforeEach, describe, expect, it } from "vitest";

import {
  clearRecentResults,
  readRecentResults,
  recordRecentResult,
  removeRecentResult,
  resetRecentResultsForTests,
  resultsRecentScopeKey,
} from "./results-recent";

beforeEach(resetRecentResultsForTests);

describe("Results recent tab memory", () => {
  it("keeps three unique most-recent keys", () => {
    const scopeKey = resultsRecentScopeKey({
      principalId: "u_1",
      sessionId: "s_1",
      scope: "session",
    });
    recordRecentResult(scopeKey, "artifact:a");
    recordRecentResult(scopeKey, "artifact:b");
    recordRecentResult(scopeKey, "artifact:c");
    expect(recordRecentResult(scopeKey, "artifact:a")).toEqual([
      "artifact:a",
      "artifact:c",
      "artifact:b",
    ]);
    expect(recordRecentResult(scopeKey, "artifact:d")).toEqual([
      "artifact:d",
      "artifact:a",
      "artifact:c",
    ]);
  });

  it("separates principal, session, and report scope", () => {
    const owner = resultsRecentScopeKey({
      principalId: "u_1",
      sessionId: "s_1",
      scope: "session",
    });
    const viewer = resultsRecentScopeKey({
      principalId: "u_2",
      sessionId: "s_1",
      scope: "report",
    });
    recordRecentResult(owner, "artifact:owner");
    recordRecentResult(viewer, "artifact:viewer");

    expect(readRecentResults(owner)).toEqual(["artifact:owner"]);
    expect(readRecentResults(viewer)).toEqual(["artifact:viewer"]);
  });

  it("removes failed objects and purges only the requested scope", () => {
    recordRecentResult("scope:a", "artifact:a");
    recordRecentResult("scope:a", "artifact:b");
    recordRecentResult("scope:b", "artifact:c");

    expect(removeRecentResult("scope:a", "artifact:b")).toEqual(["artifact:a"]);
    clearRecentResults("scope:a");
    expect(readRecentResults("scope:a")).toEqual([]);
    expect(readRecentResults("scope:b")).toEqual(["artifact:c"]);
  });

  it("ignores malformed identities", () => {
    expect(recordRecentResult("scope:a", "unsafe key")).toEqual([]);
    expect(recordRecentResult("scope:a", "x".repeat(257))).toEqual([]);
  });
});
