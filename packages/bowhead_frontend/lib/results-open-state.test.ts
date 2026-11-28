// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { RESULTS_OPEN_STORAGE_KEY, openForReader, readResultsOpen, writeResultsOpen } from "./results-open-state";

afterEach(() => window.localStorage.clear());

describe("Results open state per conversation (chat running state R2.7)", () => {
  it("has no record for a conversation the reader never opened or closed Results in", () => {
    expect(readResultsOpen("s_new")).toBeNull();
    expect(readResultsOpen(null)).toBeNull();
  });

  it("keeps each conversation's own choice", () => {
    writeResultsOpen("s_a", "open", 1);
    writeResultsOpen("s_b", "closed", 2);
    expect(readResultsOpen("s_a")).toBe("open");
    expect(readResultsOpen("s_b")).toBe("closed");
  });

  it("keeps the 40 most recent conversations", () => {
    for (let i = 0; i < 45; i += 1) writeResultsOpen(`s_${i}`, "open", i);
    expect(readResultsOpen("s_0")).toBeNull();
    expect(readResultsOpen("s_44")).toBe("open");
    expect(Object.keys(JSON.parse(window.localStorage.getItem(RESULTS_OPEN_STORAGE_KEY) ?? "{}"))).toHaveLength(40);
  });

  it("ignores a damaged record", () => {
    window.localStorage.setItem(RESULTS_OPEN_STORAGE_KEY, "{not json");
    expect(readResultsOpen("s_a")).toBeNull();
    writeResultsOpen("s_a", "closed", 1);
    expect(readResultsOpen("s_a")).toBe("closed");
  });
});

describe("openForReader", () => {
  it("reads the file Lead named and nothing else", () => {
    expect(openForReader({ open_for_reader: "art_page" })).toBe("art_page");
    expect(openForReader({ open_for_reader: "" })).toBeNull();
    expect(openForReader({})).toBeNull();
    expect(openForReader(undefined)).toBeNull();
  });
});
