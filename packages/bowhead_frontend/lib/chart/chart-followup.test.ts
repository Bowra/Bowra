/**
 * Batch 2 §3.4 · Ask About This Chart event contract.
 * Test sender and subscriber halves: a mismatch means clicking does nothing (the silent failure this sprint must eliminate).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { CHART_FOLLOWUP_EVENT, chartFollowupReference, onChartFollowup, requestChartFollowup } from "./chart-followup";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("authorized chart reference metadata", () => {
  const details = { id: "art_x", filename: "monthly-results.xlsx", mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", size: 1532, source: "bi_fetch_rows", created_at: "2026-10-09" };
  it("uses the existing details endpoint and preserves exact filename, MIME and byte size", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ representation: "details", revision: "r1", details }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const signal = new AbortController().signal;
    expect(await chartFollowupReference("s_1", "art_x", signal)).toEqual({ fileId: "art_x", filename: details.filename, mime: details.mime, size: 1532, referenceKind: "result" });
    expect(fetch).toHaveBeenCalledWith("/api/v1/files/art_x/representations/details", expect.objectContaining({ signal, cache: "no-store" }));
  });
  it.each([{ id: "another" }, { filename: "" }, { filename: "x".repeat(513) }, { mime: "" }, { mime: "x".repeat(201) }, { size: -1 }, { size: null }])("rejects incompatible metadata instead of fabricating a reference: %j", async (patch) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ representation: "details", revision: "r1", details: { ...details, ...patch } }), { status: 200 })));
    await expect(chartFollowupReference("s_1", "art_x", new AbortController().signal)).rejects.toThrow("unavailable");
  });
  it("preserves an authorization rejection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 403 })));
    await expect(chartFollowupReference("s_1", "art_x", new AbortController().signal)).rejects.toThrow("unavailable");
  });
});

describe("chart-followup 事件", () => {
  it("★发出去的 detail 带齐 composer 需要的东西（指代 + 底层数据 + 图注）", () => {
    const seen: unknown[] = [];
    const stop = onChartFollowup((d) => seen.push(d));
    try {
      requestChartFollowup({
        title: "各品类营业额",
        dataArtifactId: "art_x",
        kind: "bar",
        captionText: "女装贡献超三分之一",
      });
    } finally {
      stop();
    }
    expect(seen).toEqual([{
      title: "各品类营业额",
      dataArtifactId: "art_x",
      kind: "bar",
      captionText: "女装贡献超三分之一",
    }]);
  });

  it("取消订阅后不再收到（层关掉了还收事件 = 幽灵预填）", () => {
    const handler = vi.fn();
    onChartFollowup(handler)();
    requestChartFollowup({ title: "x" });
    expect(handler).not.toHaveBeenCalled();
  });

  it("★形状不对的事件被忽略（别的代码往同名事件上发东西也不该炸 composer）", () => {
    const handler = vi.fn();
    const stop = onChartFollowup(handler);
    try {
      window.dispatchEvent(new CustomEvent(CHART_FOLLOWUP_EVENT, { detail: { nope: 1 } }));
      window.dispatchEvent(new CustomEvent(CHART_FOLLOWUP_EVENT));
    } finally {
      stop();
    }
    expect(handler).not.toHaveBeenCalled();
  });

  it("没有 artifact 的图也能追问（只是不附数据）", () => {
    const seen: Array<{ dataArtifactId?: string | null }> = [];
    const stop = onChartFollowup((d) => seen.push(d));
    try {
      requestChartFollowup({ title: "无 artifact", dataArtifactId: null });
    } finally {
      stop();
    }
    expect(seen[0]!.dataArtifactId).toBeNull();
  });
});
