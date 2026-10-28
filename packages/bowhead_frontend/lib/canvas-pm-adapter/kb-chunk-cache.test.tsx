/** Citation reads reauthorize on reopening and isolate pending requests by space. */
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearKbChunkCache, useKbChunkContent } from "./kb-chunk-cache";

const chunk = (id: string, content: string) => ({ chunk_id: id, content, page: null, section_title: null, role: "body", chunk_index: 1 });

function stubFetch(body: unknown) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
const urls = (m: ReturnType<typeof vi.fn>) => m.mock.calls.map((c) => String(c[0]));

async function readSingle(fileId = "kbf_1", chunkId = "c2") {
  const { result, unmount } = renderHook(() => useKbChunkContent(fileId, chunkId, "ws_kb", { single: true }));
  await waitFor(() => expect(result.current.status).not.toBe("loading"));
  const state = result.current;
  unmount();
  return state;
}

beforeEach(() => clearKbChunkCache());
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useKbChunkContent · 单片段（对话来源卡）", () => {
  it("带 chunk_id 只取这一段；新后端只回这一段", async () => {
    const fetchMock = stubFetch({ chunks: [chunk("c2", "第二段原文")], total: 1 });
    const state = await readSingle();
    expect(state).toMatchObject({ status: "ready", content: "第二段原文" });
    expect(urls(fetchMock)).toEqual(["/api/v1/kb/files/kbf_1/chunks?chunk_id=c2"]);
  });

  it("旧后端忽略参数回整份列表：按 id 找到那一段；找不到时是「原文已更新」而不是拿别的段冒充", async () => {
    stubFetch({ chunks: [chunk("c1", "第一段"), chunk("c2", "第二段原文"), chunk("c3", "第三段")], total: 3 });
    expect(await readSingle()).toMatchObject({ status: "ready", content: "第二段原文" });
    expect(await readSingle("kbf_1", "c_gone")).toEqual({ status: "empty" });
  });

  it("重新打开引用时重新验权，撤权后不复用先前的原文", async () => {
    const fetchMock = stubFetch({ chunks: [chunk("c2", "第二段原文")], total: 1 });
    expect(await readSingle()).toMatchObject({ status: "ready" });
    fetchMock.mockImplementation(async () => new Response("{}", { status: 403 }));
    expect(await readSingle()).toEqual({ status: "error" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("并发读取同一引用时，不跨空间共享正在进行的请求", async () => {
    const fetchMock = stubFetch({ chunks: [chunk("c2", "内容")], total: 1 });
    const a = renderHook(() => useKbChunkContent("kbf_1", "c2", "ws_a", { single: true }));
    const b = renderHook(() => useKbChunkContent("kbf_1", "c2", "ws_b", { single: true }));
    await waitFor(() => expect(a.result.current.status).toBe("ready"));
    await waitFor(() => expect(b.result.current.status).toBe("ready"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const spaces = fetchMock.mock.calls.map(call => new Headers((call as unknown as [string, RequestInit])[1].headers).get("X-Space-Id"));
    expect(spaces.sort()).toEqual(["ws_a", "ws_b"]);
  });

});

describe("useKbChunkContent · 文件级（矩阵等）", () => {
  it("不带 chunk_id 取整份列表，重新打开其他片段也会重新检查", async () => {
    const fetchMock = stubFetch({ chunks: [chunk("c1", "第一段"), chunk("c2", "第二段原文")], total: 2 });
    const a = renderHook(() => useKbChunkContent("kbf_1", "c1"));
    await waitFor(() => expect(a.result.current).toMatchObject({ status: "ready", content: "第一段" }));
    const b = renderHook(() => useKbChunkContent("kbf_1", "c2"));
    await waitFor(() => expect(b.result.current).toMatchObject({ status: "ready", content: "第二段原文" }));
    expect(urls(fetchMock)).toEqual(["/api/v1/kb/files/kbf_1/chunks", "/api/v1/kb/files/kbf_1/chunks"]);
  });
});
