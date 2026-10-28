import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchReports, fetchReportsPage } from "./api";

describe("fetchReports", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("serializes include_empty=false instead of dropping the false value", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(
      JSON.stringify({ items: [] }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    await fetchReports({ scope: "shared", limit: 100, include_empty: false });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/canvas?limit=100&scope=shared&include_empty=false",
    );
  });
});

describe("fetchReportsPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the cursor and returns total and next_cursor", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(
      JSON.stringify({ items: [{ id: "cv_1" }], total: 240, next_cursor: "abc" }),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
    vi.stubGlobal("fetch", fetchMock);

    const page = await fetchReportsPage({ sort: "title_asc", limit: 100, cursor: "prev" });

    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/canvas?sort=title_asc&limit=100&cursor=prev",
    );
    expect(page.total).toBe(240);
    expect(page.next_cursor).toBe("abc");
    expect(page.items.map((item) => item.id)).toEqual(["cv_1"]);
  });

  it("returns total=null for cursor pages", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ items: [{ id: "cv_2" }], total: null, next_cursor: null, has_more: false }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));

    const page = await fetchReportsPage({ cursor: "c1" });

    expect(page.total).toBeNull();
    expect(page.has_more).toBe(false);
  });

  it("treats a response without pagination fields as the last page", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ items: [{ id: "cv_1" }, { id: "cv_2" }] }),
      { status: 200, headers: { "content-type": "application/json" } },
    )));

    const page = await fetchReportsPage();

    expect(page.total).toBe(2);
    expect(page.next_cursor).toBeNull();
  });
});
