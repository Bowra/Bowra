import { describe, expect, it } from "vitest";

import { formatByteSize } from "./format";

describe("formatByteSize", () => {
  it("writes readable units with a space (116 Skills page showed 566K / 944B)", () => {
    expect(formatByteSize(944, "en-US")).toBe("944 B");
    expect(formatByteSize(566 * 1024 + 300, "en-US")).toBe("566 KB");
    expect(formatByteSize(2048, "zh-CN")).toBe("2 KB");
    expect(formatByteSize(1.25 * 1024 * 1024, "en-US")).toBe("1.3 MB");
    expect(formatByteSize(0, "en-US")).toBe("");
  });

  it("follows the UI language's number format and units", () => {
    expect(formatByteSize(1.25 * 1024 * 1024, "de-DE")).toBe("1,3 MB");
    expect(formatByteSize(1.25 * 1024 * 1024, "fr-FR")).toBe("1,3 Mo");
    expect(formatByteSize(944, "fr-FR")).toBe("944 o");
    expect(formatByteSize(100 * 1024 * 1024, "en-US")).toBe("100 MB");
  });
});
