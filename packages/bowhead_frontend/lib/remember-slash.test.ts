/**
 * V3 PRD-B · /remember slash command parser tests.
 *
 * Covers ~12 cases:
 *   - default kind=preference
 *   - explicit kind prefix
 *   - JSON value parse
 *   - string fallback
 *   - empty body
 *   - missing equals
 *   - empty key
 *   - unicode key (Chinese)
 *   - bare /remember
 *   - non-/remember input
 *   - kind:key without equals
 *   - dispatch ok / dispatch error wrap
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { dispatchRememberLine, parseRememberLine } from "./remember-slash";

describe("parseRememberLine", () => {
  it("defaults kind to preference, JSON parse value", () => {
    const r = parseRememberLine("/remember favorite_chart=\"bar\"");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.kind).toBe("preference");
    expect(r.parsed.key).toBe("favorite_chart");
    expect(r.parsed.value).toBe("bar");
    expect(r.parsed.valueIsJson).toBe(true);
  });

  it("accepts explicit kind prefix", () => {
    const r = parseRememberLine("/remember constraint:report_scope=team");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.kind).toBe("constraint");
    expect(r.parsed.key).toBe("report_scope");
    expect(r.parsed.value).toBe("team");
    expect(r.parsed.valueIsJson).toBe(false);
  });

  it("parses array JSON value", () => {
    const r = parseRememberLine("/remember preferred_metrics=[\"GMV\",\"DAU\"]");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.value).toEqual(["GMV", "DAU"]);
    expect(r.parsed.valueIsJson).toBe(true);
  });

  it("falls back to string for unparseable JSON", () => {
    const r = parseRememberLine("/remember chart_style=clean and minimal");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.value).toBe("clean and minimal");
    expect(r.parsed.valueIsJson).toBe(false);
  });

  it("rejects bare /remember", () => {
    expect(parseRememberLine("/remember")).toMatchObject({ ok: false });
  });

  it("rejects /remember with no equals", () => {
    expect(parseRememberLine("/remember key value")).toMatchObject({ ok: false });
  });

  it("rejects /remember with empty key", () => {
    expect(parseRememberLine("/remember =val")).toMatchObject({ ok: false });
  });

  it("supports unicode (Chinese) key", () => {
    const r = parseRememberLine("/remember 偏好货币=\"CNY\"");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.key).toBe("偏好货币");
    expect(r.parsed.value).toBe("CNY");
  });

  it("rejects non-/remember input", () => {
    expect(parseRememberLine("hello world")).toMatchObject({ ok: false });
  });

  it("invalid kind prefix falls back to default kind, treats whole as key=val", () => {
    const r = parseRememberLine("/remember bogus_kind:k=v");
    // colonIdx > 0, eqIdx > colonIdx but kind is invalid → keep kind=preference, rest=body
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.kind).toBe("preference");
    expect(r.parsed.key).toBe("bogus_kind:k");
  });

  it("kind:key with no equals after = error", () => {
    expect(parseRememberLine("/remember preference:onlykey")).toMatchObject({ ok: false });
  });

  it("constraint kind accepted", () => {
    const r = parseRememberLine("/remember constraint:no_pii=true");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.parsed.kind).toBe("constraint");
    expect(r.parsed.value).toBe(true);
  });
});

describe("dispatchRememberLine", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

  it("returns ok with key on successful POST", async () => {
    fetchMock().mockResolvedValueOnce(new Response(
      JSON.stringify({ id: "mem_x", kind: "preference", key: "k", value: "v" }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    const r = await dispatchRememberLine("/remember k=v");
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.key).toBe("k");
  });

  it("returns ok=false with a failure reason and the raw error (for diagnostics) when API fails", async () => {
    fetchMock().mockResolvedValueOnce(new Response("err", { status: 500 }));
    const r = await dispatchRememberLine("/remember k=v");
    expect(r.ok).toBe(false);
    if (r.ok) return;
    expect(r.reason).toBe("failed");
    expect(r.error).toMatchObject({ status: 500 });
  });

  it("returns ok=false on parse error before any fetch", async () => {
    const r = await dispatchRememberLine("/remember badsyntax");
    expect(r.ok).toBe(false);
    expect(fetchMock()).not.toHaveBeenCalled();
  });
});
