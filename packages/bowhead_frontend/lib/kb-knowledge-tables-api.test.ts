// Knowledge base UX cleanup FR8: the template list seeds once only if the space is unseeded; concurrent requests for the same list are coalesced.
import { afterEach, describe, expect, it, vi } from "vitest";

import { listDocTypesWithStats, listKnowledgeTables } from "./kb-knowledge-tables-api";

type Call = { url: string; method: string };

function stubFetch(seededAfterSeed: boolean, initiallySeeded: boolean) {
  const calls: Call[] = [];
  let seeded = initiallySeeded;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ url, method });
      if (method === "POST") {
        seeded = seededAfterSeed;
        return new Response(JSON.stringify({ seeded: true }), { status: 200 });
      }
      await new Promise((r) => setTimeout(r, 5));
      return new Response(JSON.stringify({ items: [], stats: {}, templates_seeded: seeded }), { status: 200 });
    }),
  );
  return calls;
}

const gets = (calls: Call[]) => calls.filter((c) => c.method === "GET");
const posts = (calls: Call[]) => calls.filter((c) => c.method === "POST");

afterEach(() => vi.unstubAllGlobals());

describe("模板列表读取", () => {
  it("已播种的空间：只读一次列表，不发写请求", async () => {
    const calls = stubFetch(true, true);
    await listKnowledgeTables("ws_seeded");
    expect(gets(calls)).toHaveLength(1);
    expect(posts(calls)).toHaveLength(0);
  });

  it("未播种的空间：播种一次后重拉列表", async () => {
    const calls = stubFetch(true, false);
    await listDocTypesWithStats("ws_fresh");
    expect(posts(calls)).toHaveLength(1);
    expect(gets(calls)).toHaveLength(2);
  });

  it("播种被拒（仍报告未播种）：只重拉一次，不循环", async () => {
    const calls = stubFetch(false, false);
    await listKnowledgeTables("ws_archived");
    expect(posts(calls)).toHaveLength(1);
    expect(gets(calls)).toHaveLength(2);
  });

  it("同一列表的并发请求合并为一个", async () => {
    const calls = stubFetch(true, true);
    await Promise.all([listDocTypesWithStats("ws_shared"), listDocTypesWithStats("ws_shared"), listDocTypesWithStats("ws_shared")]);
    expect(gets(calls)).toHaveLength(1);
  });
});
