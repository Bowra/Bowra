/**
 * Verify UUID generation when plain HTTP makes crypto.randomUUID unavailable.
 * The fallback must produce a valid v4 UUID without the native implementation.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { randomUuid } from "./random-id";

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Simulate an insecure origin with getRandomValues but no randomUUID. */
function stubInsecureContext(): void {
  const real = globalThis.crypto;
  vi.stubGlobal("crypto", {
    getRandomValues: real.getRandomValues.bind(real),
  });
}

describe("randomUuid", () => {
  it("在 secure context 下用原生实现", () => {
    const native = vi.fn(() => "11111111-2222-4333-8444-555555555555");
    vi.stubGlobal("crypto", { randomUUID: native });

    expect(randomUuid()).toBe("11111111-2222-4333-8444-555555555555");
    expect(native).toHaveBeenCalledTimes(1);
  });

  it("randomUUID 缺席时不抛，且仍产出合法 v4 UUID", () => {
    stubInsecureContext();

    // Direct invocation throws TypeError; assert both no throw and valid format—checking only no throw would
    // also pass when returning undefined / empty string.
    expect(() => randomUuid()).not.toThrow();
    expect(randomUuid()).toMatch(UUID_V4);
  });

  it("randomUUID 缺席时每次都不同（幂等键不能撞车）", () => {
    stubInsecureContext();

    const seen = new Set(Array.from({ length: 200 }, () => randomUuid()));
    expect(seen.size).toBe(200);
  });

  it("连 WebCrypto 都没有时仍然出合法 UUID", () => {
    vi.stubGlobal("crypto", undefined);

    expect(randomUuid()).toMatch(UUID_V4);
  });
});
