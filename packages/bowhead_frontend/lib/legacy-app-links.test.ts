import { afterEach, describe, expect, it } from "vitest";

import { normalizeAppActionUrl } from "./legacy-app-links";

afterEach(() => {
  delete window.__BOWHEAD_BASE_PATH__;
});

describe("normalizeAppActionUrl", () => {
  it.each([
    ["/memory", "/settings/memory/"],
    ["/memory/", "/settings/memory/"],
    ["/memory?item=mem_1", "/settings/memory/?item=mem_1"],
    ["/memory/?item=mem_1", "/settings/memory/?item=mem_1"],
    ["/memory/?item=mem_1#learned", "/settings/memory/?item=mem_1#learned"],
  ])("maps the removed %s page to Settings > Memory", (input, expected) => {
    expect(normalizeAppActionUrl(input)).toBe(expected);
  });

  it.each([
    "/settings/memory/",
    "/settings/memory/?item=mem_1",
    "/memory-old/",
    "/memory/trash/",
    "/files/?focus=memory",
    "/?session=s_1",
    "https://example.com/memory/",
    "//example.com/memory/",
  ])("leaves %s unchanged", (input) => {
    expect(normalizeAppActionUrl(input)).toBe(input);
  });

  it("maps a prefixed legacy link under the /bowhead public entry", () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    expect(normalizeAppActionUrl("/bowhead/memory/?item=mem_1")).toBe("/settings/memory/?item=mem_1");
    expect(normalizeAppActionUrl("/memory/?item=mem_1")).toBe("/settings/memory/?item=mem_1");
    expect(normalizeAppActionUrl("/bowhead/files/")).toBe("/bowhead/files/");
  });
});
