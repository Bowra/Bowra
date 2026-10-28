/**
 * chat-redesign p0-followup F4 — `[data:art_*]` artifact token detector
 * + scroll helper.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { findArtifactTokens, scrollToArtifact } from "./artifact-chip";

describe("findArtifactTokens", () => {
  it("returns [] for empty / token-free text", () => {
    expect(findArtifactTokens("")).toEqual([]);
    expect(findArtifactTokens("plain prose")).toEqual([]);
    expect(findArtifactTokens("**bold** text only")).toEqual([]);
  });

  it("matches a single [data:art_*] token", () => {
    const out = findArtifactTokens("总盘子 [data:art_0fa494fbf141] ¥123 亿");
    expect(out).toHaveLength(1);
    expect(out[0].artifactId).toBe("art_0fa494fbf141");
    expect(out[0].start).toBe(4);
    expect(out[0].end).toBe(4 + "[data:art_0fa494fbf141]".length);
  });

  it("matches multiple tokens in source order", () => {
    const out = findArtifactTokens(
      "GMV [data:art_aaa111] 同比 [data:art_bbb222] 增长 8%",
    );
    expect(out.map((m) => m.artifactId)).toEqual(["art_aaa111", "art_bbb222"]);
  });

  it("does NOT match generic [N] citation chips (number-only payload)", () => {
    // N15 deprecated chat citations; the regex must not catch [1] [12] etc.
    expect(findArtifactTokens("增长 8% [1] 江苏 [12]")).toEqual([]);
  });

  it("does NOT match [data:something_else] non-art prefixes", () => {
    expect(findArtifactTokens("[data:src_abc] [data:kbart_123]")).toEqual([]);
  });

  it("is case-insensitive on the prefix", () => {
    // Tolerate `[Data:Art_...]` shape from a flaky backend serializer.
    const out = findArtifactTokens("X [DATA:ART_ABC123]");
    expect(out).toHaveLength(1);
    // Casing of the id is preserved from the source.
    expect(out[0].artifactId).toBe("ART_ABC123");
  });

  it("regex lastIndex doesn't bleed between calls", () => {
    findArtifactTokens("[data:art_abc111]");
    const second = findArtifactTokens("[data:art_def222]");
    expect(second).toHaveLength(1);
    expect(second[0].artifactId).toBe("art_def222");
  });
});

describe("scrollToArtifact", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("returns false when no matching DOM node exists", () => {
    expect(scrollToArtifact("art_missing")).toBe(false);
  });

  it("returns true and calls scrollIntoView when data-artifact-id matches", () => {
    const target = document.createElement("div");
    target.setAttribute("data-artifact-id", "art_hit");
    const spy = vi.fn();
    target.scrollIntoView = spy;
    document.body.appendChild(target);

    expect(scrollToArtifact("art_hit")).toBe(true);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("falls back to data-section-id when no data-artifact-id", () => {
    const target = document.createElement("div");
    target.setAttribute("data-section-id", "art_section");
    const spy = vi.fn();
    target.scrollIntoView = spy;
    document.body.appendChild(target);

    expect(scrollToArtifact("art_section")).toBe(true);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});
