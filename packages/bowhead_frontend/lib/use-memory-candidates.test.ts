/**
 * Parse write-gate rejection reasons (memory rebuild batch 2 review-fix round).
 *
 * This is the **only** place translating backend 422 into UI-usable information. If it silently returns undefined,
 * the card falls back to “操作失败，请稍后再试”, describing a designed policy rejection as a transient failure.
 * Lock every possible payload-shape variation here.
 */

import { describe, expect, it } from "vitest";

import { MemoryAPIError } from "@/lib/memory-api";
import { _gateRefusal } from "@/lib/use-memory-candidates";

function _gateError(detail: unknown, status = 422) {
  return new MemoryAPIError(status, "memory_write_gate_refused", { detail });
}

describe("_gateRefusal", () => {
  it("从 422 的 payload 里取出 reason", () => {
    const err = _gateError({
      code: "memory_write_gate_refused",
      reason: "one_off",
      message: "…",
    });
    expect(_gateRefusal(err)).toBe("one_off");
  });

  it("四个拒因都认得", () => {
    for (const reason of [
      "one_off",
      "language_is_a_setting",
      "no_attribution",
      "contains_secret",
    ]) {
      expect(
        _gateRefusal(_gateError({ code: "memory_write_gate_refused", reason })),
      ).toBe(reason);
    }
  });

  it("不是 gate 拒绝的 422 不认（别把别的 422 也说成策略拒绝）", () => {
    expect(
      _gateRefusal(
        _gateError({ code: "memory_scope_conflict", reason: "one_off" }),
      ),
    ).toBeUndefined();
  });

  it("状态码不是 422 就不认", () => {
    expect(
      _gateRefusal(
        _gateError(
          { code: "memory_write_gate_refused", reason: "one_off" },
          409,
        ),
      ),
    ).toBeUndefined();
  });

  it("未知 reason 不透传给 t() —— 否则 next-intl 会抛 MISSING_MESSAGE", () => {
    expect(
      _gateRefusal(
        _gateError({
          code: "memory_write_gate_refused",
          reason: "brand_new_rule",
        }),
      ),
    ).toBeUndefined();
  });

  it("payload 走样（缺 detail / detail 是字符串 / 根本不是 MemoryAPIError）一律回落", () => {
    expect(_gateRefusal(_gateError(undefined))).toBeUndefined();
    expect(_gateRefusal(_gateError("plain string detail"))).toBeUndefined();
    expect(_gateRefusal(new Error("boom"))).toBeUndefined();
    expect(_gateRefusal(undefined)).toBeUndefined();
  });
});
