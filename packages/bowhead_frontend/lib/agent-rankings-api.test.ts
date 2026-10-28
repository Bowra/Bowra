import { beforeEach, describe, expect, it, vi } from "vitest";
import { chartResult } from "@/__tests__/fixtures/agent-charts";
import { apiFetch, ApiError, readApiError } from "./api-client";
import { listAgentRankings } from "./agent-rankings-api";

vi.mock("./api-client", async importOriginal => ({ ...await importOriginal<typeof import("./api-client")>(), apiFetch: vi.fn(), readApiError: vi.fn() }));

function response(body: unknown) {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
}

describe("agent ranking read boundary", () => {
  beforeEach(() => vi.resetAllMocks());

  it("reads a complete snapshot without constructing a client ranking", async () => {
    const snapshot = chartResult();
    response(snapshot);
    expect(await listAgentRankings({ rankingKind: "popular" })).toEqual(snapshot);
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/agents/rankings?ranking_kind=popular", expect.objectContaining({ cache: "no-store" }));
  });

  it.each([404, 501])("treats an unsupported producer (%s) as unavailable", async status => {
    vi.mocked(apiFetch).mockResolvedValue(new Response(null, { status }));
    expect(await listAgentRankings({ rankingKind: "popular" })).toBeNull();
  });

  it("preserves forbidden errors instead of disguising them as an empty chart", async () => {
    const error = new ApiError(403, "Forbidden");
    vi.mocked(apiFetch).mockResolvedValue(new Response(null, { status: 403 }));
    vi.mocked(readApiError).mockResolvedValue(error);
    await expect(listAgentRankings({ rankingKind: "popular" })).rejects.toBe(error);
  });

  it.each(["stale", "privacy_suppressed", "insufficient_sample"] as const)("discards counts in a %s snapshot", async state => {
    response({ ...chartResult(), state });
    expect(await listAgentRankings({ rankingKind: "popular" })).toMatchObject({ state, rows: [] });
  });

  it("rejects a row whose adoption count falls below the privacy floor", async () => {
    const snapshot = chartResult();
    snapshot.rows[0].hire_members_current = 4;
    response(snapshot);
    expect(await listAgentRankings({ rankingKind: "popular" })).toMatchObject({ state: "unavailable", rows: [] });
  });

  it("rejects trending counts with an insufficient comparison sample", async () => {
    const snapshot = chartResult("trending");
    snapshot.rows[0].active_members_previous_7d = 4;
    response(snapshot);
    expect(await listAgentRankings({ rankingKind: "trending" })).toMatchObject({ state: "unavailable", rows: [] });
  });

  it("rejects a partial popular window", async () => {
    response({ ...chartResult(), window_start: "2026-09-22T00:00:00Z" });
    expect(await listAgentRankings({ rankingKind: "popular" })).toMatchObject({ state: "unavailable", rows: [] });
  });

  it("requires adjacent trending comparison windows", async () => {
    response({ ...chartResult("trending"), comparison_window_start: "2026-09-14T00:00:00Z", comparison_window_end: "2026-09-21T00:00:00Z" });
    expect(await listAgentRankings({ rankingKind: "trending" })).toMatchObject({ state: "unavailable", rows: [] });
  });

  it("rejects a response for another category", async () => {
    response(chartResult("popular", "general"));
    await expect(listAgentRankings({ rankingKind: "popular", domain: "finance" })).rejects.toThrow("Invalid agent ranking response");
  });
});
