// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ResultItemDTO } from "./results-api";
import {
  earlierFileVersionItem,
  isEarlierFileVersion,
  loadEarlierFileVersion,
} from "./results-file-versions";

const { appFetchMock } = vi.hoisted(() => ({ appFetchMock: vi.fn() }));
vi.mock("./base-path", () => ({ appFetch: appFetchMock }));

afterEach(() => appFetchMock.mockReset());

function revisedEntry(lineage: ResultItemDTO["lineage"] = { number: 3, count: 3 }): ResultItemDTO {
  return {
    key: "artifact:art_v1",
    object_type: "artifact",
    title: "report.html",
    primary_group: "deliverables",
    secondary_group: null,
    viewer_profile: "html",
    status: "ready",
    route: { type: "artifact", artifact_id: "art_v3", default_mode: "preview" },
    capabilities: ["download", "view_details", "reference_in_chat"],
    relation_roles: ["main_deliverable"],
    execution: null,
    display_context: {
      source_role: "sandbox",
      output_role: "main_deliverable",
      execution_label: null,
      occurred_at: "2026-11-13T00:03:00Z",
      duplicate_ordinal: null,
      duplicate_count: null,
    },
    position: 2,
    lineage,
    version: null,
    artifact_metadata: {
      mime: "text/html",
      size: 79_000,
      source: "python_sandbox",
      kind: "deliverable",
      origin: "compute",
      trust: "trusted",
      created_at: "2026-11-13T00:03:00Z",
      producer_kind: "python_sandbox",
    },
  };
}

const v2Details = {
  id: "art_v2",
  filename: "report.html",
  mime: "text/html",
  size: 67_000,
  source: "python_sandbox",
  created_at: "2026-11-13T00:02:00Z",
  kind: "deliverable",
  producer_kind: "python_sandbox",
};

describe("earlier versions of a revised file", () => {
  it("recognises an earlier version only through a revised file's entry", () => {
    expect(isEarlierFileVersion("art_v2", revisedEntry())).toBe(true);
    expect(isEarlierFileVersion("art_v3", revisedEntry())).toBe(false);
    expect(isEarlierFileVersion("art_old_export", revisedEntry(null))).toBe(false);
    expect(isEarlierFileVersion("art_v2", null)).toBe(false);
  });

  it("builds the viewer item from the version's own metadata", () => {
    expect(earlierFileVersionItem("art_v2", revisedEntry(), v2Details)).toEqual(expect.objectContaining({
      id: "art_v2",
      size: 67_000,
      created_at: "2026-11-13T00:02:00Z",
      origin: "compute",
      trust: "trusted",
      preview_available: true,
    }));
  });

  it("refuses details that belong to another file or format", () => {
    expect(earlierFileVersionItem("art_v2", revisedEntry(), { ...v2Details, id: "art_other" })).toBeNull();
    expect(earlierFileVersionItem("art_v2", revisedEntry(), { ...v2Details, mime: "text/plain" })).toBeNull();
    expect(earlierFileVersionItem("art_v2", revisedEntry(), undefined)).toBeNull();
  });

  it("reads the version's details under the current scope", async () => {
    appFetchMock.mockResolvedValue(new Response(JSON.stringify({
      family: "html", representation: "details", revision: "r1", details: v2Details,
    }), { status: 200 }));

    const version = await loadEarlierFileVersion({
      artifactId: "art_v2",
      entry: revisedEntry(),
      scope: "report",
      sessionId: "s_1",
      signal: new AbortController().signal,
    });

    expect(appFetchMock.mock.calls[0]?.[0]).toBe("/api/v1/canvas/by-session/s_1/artifacts/art_v2/representations/details");
    expect(version).toEqual({ sessionId: "s_1", entryKey: "artifact:art_v1", item: expect.objectContaining({ id: "art_v2" }) });
  });
});
