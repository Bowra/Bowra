import { afterEach, describe, expect, it, vi } from "vitest";
import { patchCanvasSection } from "./canvas-patch";

describe("patchCanvasSection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends expected_rev when the caller has a section revision", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      json: async () => ({ rev: 8 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await patchCanvasSection("cv_1", "sec_1", { markdown: "changed" }, 7);

    expect(res).toEqual({ ok: true, rev: 8 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init.body))).toEqual({
      patch: { markdown: "changed" },
      expected_rev: 7,
    });
  });

  it("omits expected_rev only when no revision is available", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      json: async () => ({ rev: 2 }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const res = await patchCanvasSection("cv_1", "sec_1", { markdown: "changed" });

    expect(res).toEqual({ ok: true, rev: 2 });
    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(init.body))).toEqual({
      patch: { markdown: "changed" },
    });
  });

  it.each([
    ["zh-CN", 500, "这次修改没有保存"],
    ["en-US", 500, "This change wasn't saved"],
    ["de-DE", 500, "Diese Änderung wurde nicht gespeichert"],
    ["zh-CN", 401, "登录已过期"],
    ["en-US", 403, "permission"],
  ] as const)("%s %s: a localized sentence without the status; the status rides on error for diagnostics", async (locale, status, text) => {
    document.documentElement.lang = locale;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status, headers: { "x-request-id": "0123456789abcdef0123456789abcdef" } })));

    const res = await patchCanvasSection("cv_1", "sec_1", { markdown: "changed" }, 7);

    document.documentElement.lang = "";
    if (res.ok || res.status !== "error") throw new Error("expected an error result");
    expect(res.message).toContain(text);
    expect(res.message).not.toMatch(/HTTP|\d{3}|sec_1/);
    expect(res.error).toMatchObject({ status, requestId: "0123456789abcdef0123456789abcdef" });
  });

  it("network failure asks to check the connection", async () => {
    document.documentElement.lang = "en-US";
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const res = await patchCanvasSection("cv_1", "sec_1", { markdown: "changed" });

    document.documentElement.lang = "";
    if (res.ok || res.status !== "error") throw new Error("expected an error result");
    expect(res.message).toContain("connection");
    expect(res.message).not.toContain("Failed to fetch");
  });
});
