import { describe, expect, it } from "vitest";

import { canonicalEntityTag, readCanonicalEtag } from "./entity-tag";

const STRONG = `"personal-v3:${"a".repeat(64)}"`;

describe("canonicalEntityTag", () => {
  it("strips the weak-validator prefix a compressing CDN adds", () => {
    expect(canonicalEntityTag(`W/${STRONG}`)).toBe(STRONG);
  });

  it("leaves an already-strong tag untouched", () => {
    expect(canonicalEntityTag(STRONG)).toBe(STRONG);
  });

  it("passes through null and undefined so callers keep their own missing-ETag error", () => {
    expect(canonicalEntityTag(null)).toBeNull();
    expect(canonicalEntityTag(undefined)).toBeUndefined();
  });

  it("normalizes only, never repairs: W/ without a quoted tag is left alone", () => {
    expect(canonicalEntityTag("W/not-a-tag")).toBe("W/not-a-tag");
    expect(canonicalEntityTag("*")).toBe("*");
  });
});

describe("readCanonicalEtag", () => {
  it("reads the header case-insensitively and canonicalizes it", () => {
    const response = new Response(null, { headers: { ETag: `W/${STRONG}` } });

    expect(readCanonicalEtag(response)).toBe(STRONG);
  });

  it("returns null when the response carries no ETag", () => {
    expect(readCanonicalEtag(new Response(null))).toBeNull();
  });
});
