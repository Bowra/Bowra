/**
 * Internal error_kind → plain language (116 dogfood 2026-08-14).
 *
 * Users actually saw `已受阻 · transient` and `已受阻 · upstream_failed`: raw
 * internal enum values. The adjacent comment already noted that users do not know needs_user_input,
 * but only that one kind was special-cased, leaving all others exposed.
 */
import { describe, expect, it } from "vitest";
import { blockedKindLabel } from "./PlanCard";

// Stub t to echo keys, so assertions check which key was looked up rather than a particular translation.
const t = ((k: string) => `T:${k}`) as unknown as Parameters<typeof blockedKindLabel>[1];

describe("blockedKindLabel", () => {
  it.each([
    ["transient", "T:blocked_kind_transient"],
    ["tool_failure", "T:blocked_kind_tool_failure"],
    ["domain_mismatch", "T:blocked_kind_domain_mismatch"],
    ["upstream_failed", "T:blocked_kind_upstream_failed"],
    ["empty_output", "T:blocked_kind_empty_output"],
    ["peer_bail", "T:blocked_kind_peer_bail"],
  ])("已知 kind %s 走 i18n", (kind, expected) => {
    // Mutation: empty the mapping → all these fail.
    expect(blockedKindLabel(kind, t)).toBe(expected);
  });

  it("Timeout 的两种拼法都认（后端两条路各写一种）", () => {
    expect(blockedKindLabel("Timeout", t)).toBe("T:blocked_kind_timeout");
    expect(blockedKindLabel("TimeoutError", t)).toBe("T:blocked_kind_timeout");
  });

  it("★认不出的 kind 回落原串，不许编", () => {
    // The backend generic branch uses **exception class names** as kind (`type(e).__name__`), an open set.
    // Prefer exposing English to a Chinese statement that might be untrue.
    expect(blockedKindLabel("ManifestLookupError", t)).toBe("ManifestLookupError");
    expect(blockedKindLabel("SomeBrandNewError", t)).toBe("SomeBrandNewError");
  });
});
