import { afterEach, describe, expect, it, vi } from "vitest";

import { sha256Canonical } from "./canvas-visual-identity";

// P0-1b (report-edit-audit dogfood): plain HTTP deployments (direct IP access to an internal host) lack
// crypto.subtle, so sha256Canonical uses pure JS fallback. Fallback and WebCrypto
// must be byte-identical for client-receipt/server-projection parity.

const BATTERY: unknown[] = [
  null,
  true,
  0,
  -0,
  1.5,
  123456789,
  "",
  "abc",
  "中文多字节 UTF-8 内容，含标点、换行\n与 emoji 😀🐋",
  Array.from({ length: 70 }, (_, i) => "x".repeat(i)), // Cover 64-byte block boundaries
  {
    effective_kind: "bar",
    locale: "zh-CN",
    payload: {
      spec: {
        kind: "bar",
        title: "各省 UV YoY",
        rows: [
          { province: "上海", uv_yoy: -1.5, revenue: 9997000000.12 },
          { province: "浙江", uv_yoy: -1.7, revenue: 1234567.89 },
        ],
      },
    },
    renderer_version: "1",
    section_kind: "chart",
    theme_hash: null,
    typed_data_sha256: null,
  },
];

describe("sha256Canonical fallback parity", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("produces identical digests with and without crypto.subtle", async () => {
    const viaSubtle = await Promise.all(BATTERY.map((v) => sha256Canonical(v)));
    vi.stubGlobal("crypto", {});
    const viaFallback = await Promise.all(BATTERY.map((v) => sha256Canonical(v)));
    expect(viaFallback).toEqual(viaSubtle);
    for (const digest of viaFallback) {
      expect(digest).toMatch(/^sha256:[0-9a-f]{64}$/);
    }
  });

  it("matches a known SHA-256 vector through the canonical encoding", async () => {
    // canonicalJson("abc") === '["str","abc"]'; vector independently computed by Python hashlib,
    // preventing the same error in both paths.
    vi.stubGlobal("crypto", {});
    await expect(sha256Canonical("abc")).resolves.toBe(
      "sha256:c2f50fe77360fa3641602212de2704a47fe545edd3ed6659b0553d337efc5f8a",
    );
  });
});
