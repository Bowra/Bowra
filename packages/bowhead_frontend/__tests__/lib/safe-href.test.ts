import { describe, expect, it } from "vitest";

import { safeHref } from "@/lib/safe-href";

describe("safeHref (M1 RD §7 — XSS guard for external/LLM-supplied URLs)", () => {
  it("allows http and https", () => {
    expect(safeHref("https://example.com/a")).toBe("https://example.com/a");
    expect(safeHref("http://example.com")).toBe("http://example.com");
  });

  it("allows root-relative paths (our own download links)", () => {
    expect(safeHref("/api/v1/files/art_x")).toBe("/api/v1/files/art_x");
  });

  it("rejects javascript: / data: / other schemes", () => {
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(safeHref("data:text/html,<script>")).toBeUndefined();
    expect(safeHref("vbscript:msgbox(1)")).toBeUndefined();
    expect(safeHref("  JavaScript:alert(1)")).toBeUndefined();
  });

  it("rejects protocol-relative // and backslash bypass (review P2)", () => {
    expect(safeHref("//evil.com")).toBeUndefined();
    expect(safeHref("/\\evil.com")).toBeUndefined();
    expect(safeHref("/\\/evil.com")).toBeUndefined();
    // legitimate root-relative still allowed
    expect(safeHref("/api/v1/files/art_x")).toBe("/api/v1/files/art_x");
  });

  it("rejects empty / nullish", () => {
    expect(safeHref("")).toBeUndefined();
    expect(safeHref(null)).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });
});
