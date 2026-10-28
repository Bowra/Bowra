import { describe, expect, it } from "vitest";

import { lineDiff } from "./line-diff";

describe("lineDiff", () => {
  it("marks a replaced line as del+add and keeps context verbatim", () => {
    const out = lineDiff("a\nb\nc", "a\nB\nc");
    expect(out).toEqual([
      { kind: "ctx", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "B" },
      { kind: "ctx", text: "c" },
    ]);
  });

  it("reports no changes for identical text", () => {
    expect(lineDiff("x\ny", "x\ny").every((l) => l.kind === "ctx")).toBe(true);
  });

  it("never truncates: oversized inputs degrade to whole del/add", () => {
    const big = Array.from({ length: 600 }, (_, i) => `l${i}`).join("\n");
    const out = lineDiff(big, big + "\nextra");
    expect(out.filter((l) => l.kind === "del")).toHaveLength(600);
    expect(out.filter((l) => l.kind === "add")).toHaveLength(601);
  });
});
