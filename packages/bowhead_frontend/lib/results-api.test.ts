import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  parseResultsQueryResponse,
  queryResults,
  ResultsApiError,
} from "./results-api";

const { appFetchMock } = vi.hoisted(() => ({ appFetchMock: vi.fn() }));

vi.mock("@/lib/base-path", () => ({ appFetch: appFetchMock }));

function artifactItem() {
  return {
    key: "artifact:art_101",
    object_type: "artifact",
    title: "deep.xlsx",
    primary_group: "evidence",
    secondary_group: "data",
    viewer_profile: "table",
    status: "ready",
    route: {
      type: "artifact",
      artifact_id: "art_101",
      default_mode: "preview",
    },
    capabilities: ["download", "view_details", "view_raw"],
    relation_roles: ["report_detail_table"],
    execution: null,
    display_context: {
      source_role: "data_query",
      output_role: "report_detail_table",
      execution_label: null,
      occurred_at: "2026-08-25T00:00:00+00:00",
      duplicate_ordinal: null,
      duplicate_count: null,
    },
    position: 101,
    version: null,
    artifact_metadata: {
      mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      size: 4096,
      source: "bi_fetch_rows",
      kind: "tool_data",
      origin: "query",
      trust: "trusted",
      created_at: "2026-08-25T00:00:00+00:00",
      producer_kind: "bi_fetch_rows",
    },
  };
}

function responseBody() {
  return {
    dto_version: 1,
    capability: { complete_list: true, scale_nav: false },
    snapshot_token: "snapshot",
    items: [artifactItem()],
    authorized_total: 135,
    matched_total: 135,
    group_counts: {
      deliverables: 35,
      evidence: 100,
      data: 70,
      code: 10,
      input: 20,
    },
    loaded_count: 1,
    page_start: 100,
    next_cursor: null,
    previous_cursor: "previous",
    has_more: false,
    projection_status: {
      report_relations: "ready",
      execution_relations: "unavailable",
      export_relations: "degraded",
    },
    projection_revision: "revision",
    new_results: { count: null, projection_changed: false },
    resolved_current_key: null,
  };
}

describe("results-api", () => {
  beforeEach(() => appFetchMock.mockReset());

  it("posts an owner query without putting the search in the URL", async () => {
    appFetchMock.mockResolvedValue(
      new Response(JSON.stringify(responseBody()), { status: 200 }),
    );

    const response = await queryResults({
      sessionId: "s/id",
      scope: "session",
      request: { surface: "explorer", query: "客户 secret", page_size: 50 },
    });

    expect(response.items[0]?.artifact_metadata?.size).toBe(4096);
    const [path, init] = appFetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/sessions/s%2Fid/results/query");
    expect(path).not.toContain("secret");
    expect(JSON.parse(String(init.body))).toMatchObject({
      dto_version: 1,
      surface: "explorer",
      query: "客户 secret",
    });
  });

  it("forwards the exact Explorer filters", async () => {
    appFetchMock.mockResolvedValue(
      new Response(JSON.stringify(responseBody()), { status: 200 }),
    );

    await queryResults({
      sessionId: "s_1",
      scope: "session",
      request: {
        surface: "explorer",
        filters: {
          lens: "current_report",
          primary: "evidence",
          secondary: "input",
          technical_only: false,
        },
      },
    });

    expect(JSON.parse(String(appFetchMock.mock.calls[0]?.[1]?.body))).toMatchObject({
      filters: {
        lens: "current_report",
        primary: "evidence",
        secondary: "input",
        technical_only: false,
      },
    });
  });

  it("uses the report closure route and forwards AbortSignal", async () => {
    appFetchMock.mockResolvedValue(
      new Response(JSON.stringify(responseBody()), { status: 200 }),
    );
    const controller = new AbortController();

    await queryResults({
      sessionId: "s_report",
      scope: "report",
      request: { surface: "locate", anchor_key: "artifact:art_101" },
      signal: controller.signal,
    });

    expect(appFetchMock.mock.calls[0]?.[0]).toBe(
      "/api/v1/canvas/by-session/s_report/results/query",
    );
    expect(appFetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
  });

  it("rejects an old or renderer-incomplete artifact DTO", () => {
    const body: unknown = {
      ...responseBody(),
      items: [{ ...artifactItem(), artifact_metadata: null }],
    };

    expect(() => parseResultsQueryResponse(body)).toThrowError(
      expect.objectContaining({ code: "results_response_invalid" }),
    );
  });

  it("rejects a result identity that cannot safely enter routes or DOM ids", () => {
    const body: unknown = {
      ...responseBody(),
      items: [{ ...artifactItem(), key: "artifact:unsafe key" }],
    };

    expect(() => parseResultsQueryResponse(body)).toThrowError(
      expect.objectContaining({ code: "results_response_invalid" }),
    );
  });

  it("rejects missing or internally inconsistent group counts", () => {
    const missing = { ...responseBody(), group_counts: undefined };
    const inconsistent = {
      ...responseBody(),
      group_counts: { deliverables: 35, evidence: 100, data: 70, code: 10, input: 19 },
    };

    expect(() => parseResultsQueryResponse(missing)).toThrowError(
      expect.objectContaining({ code: "results_response_invalid" }),
    );
    expect(() => parseResultsQueryResponse(inconsistent)).toThrowError(
      expect.objectContaining({ code: "results_response_invalid" }),
    );
  });

  it("exposes only finite server errors", async () => {
    appFetchMock.mockResolvedValue(new Response(JSON.stringify({
      detail: { error: { code: "results_snapshot_stale", secret: "do not expose" } },
    }), { status: 409 }));

    await expect(queryResults({
      sessionId: "s_1",
      scope: "session",
      request: { surface: "explorer" },
    })).rejects.toEqual(expect.objectContaining({
      code: "results_snapshot_stale",
      status: 409,
    } satisfies Partial<ResultsApiError>));
  });

  it("purges auth failures but lets an unknown 404 use old-backend fallback", async () => {
    appFetchMock
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(null, { status: 404 }));

    const first = queryResults({
      sessionId: "s_1",
      scope: "session",
      request: { surface: "explorer" },
    });
    await expect(first).rejects.toEqual(expect.objectContaining({
      code: "results_scope_unavailable",
      purgesScope: true,
    }));
    const second = queryResults({
      sessionId: "s_1",
      scope: "session",
      request: { surface: "explorer" },
    });
    await expect(second).rejects.toEqual(expect.objectContaining({
      code: "results_request_failed",
      allowsLegacyFallback: true,
    }));
  });
});
