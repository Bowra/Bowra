import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { readApiError } from "@/lib/api-client";
import {
  findResultPin,
  pinResult,
  resetResultPinsForTests,
  resultPinFailure,
  resultPinLimit,
  resultPinRoute,
  resultPinTitle,
  unpinResult,
  type ResultPin,
} from "@/lib/result-pins";

function filePin(overrides: Partial<ResultPin> = {}): ResultPin {
  return {
    kind: "file",
    pin_id: "art_root",
    artifact_id: "art_v3",
    title: "季度复盘.pptx",
    mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    version_no: 3,
    session_id: "s_1",
    opens: "session",
    changed_at: "2026-11-24T10:00:00+00:00",
    pinned_at: "2026-11-24T09:00:00+00:00",
    ...overrides,
  };
}

function reportPin(overrides: Partial<ResultPin> = {}): ResultPin {
  return {
    kind: "report",
    pin_id: "cv_1",
    title: "",
    first_section_title: "门店经营三视角",
    created_at: "2026-11-20T08:00:00+00:00",
    session_id: "s_2",
    opens: "session",
    changed_at: "2026-11-24T08:00:00+00:00",
    pinned_at: "2026-11-24T08:01:00+00:00",
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("resultPinRoute", () => {
  it("opens a file in its conversation's Results on the newest version", () => {
    expect(resultPinRoute(filePin())).toBe("/?session=s_1&result=art_v3&mode=preview");
  });

  it("opens an own file without a conversation in the Files drawer", () => {
    expect(resultPinRoute(filePin({ opens: "files", session_id: null }))).toBe("/files/?focus=art_v3");
  });

  it("opens a shared file in the shared viewer by the file's identity, not the version", () => {
    expect(resultPinRoute(filePin({ opens: "shared_view", session_id: null }))).toBe("/files/view/?file=art_root");
  });

  it("opens a report in its conversation the way the Reports list does", () => {
    expect(resultPinRoute(reportPin())).toBe("/?session=s_2&report=1");
  });
});

describe("resultPinTitle", () => {
  it("names a file by its current name and an untitled report by its first section", () => {
    expect(resultPinTitle(filePin())).toBe("季度复盘.pptx");
    expect(resultPinTitle(reportPin())).toBe("门店经营三视角");
    expect(resultPinTitle(reportPin({ title: "Q3 review" }))).toBe("Q3 review");
  });
});

describe("refused pins", () => {
  it("classifies the server's refusals and reads the named limit", async () => {
    const limit = await readApiError(jsonResponse(409, { detail: { error: { code: "result_pin_limit", limit: 80 } } }));
    expect(resultPinFailure(limit)).toBe("limit");
    expect(resultPinLimit(limit)).toBe(80);
    const gone = await readApiError(jsonResponse(404, { detail: { error: { code: "result_pin_target_not_found" } } }));
    expect(resultPinFailure(gone)).toBe("gone");
    const unavailable = await readApiError(jsonResponse(503, { detail: { error: { code: "result_pins_unavailable" } } }));
    expect(resultPinFailure(unavailable)).toBe("unavailable");
    expect(resultPinFailure(new Error("network"))).toBe("other");
    expect(resultPinLimit(new Error("network"))).toBe(100);
  });
});

describe("pin and unpin requests", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    resetResultPinsForTests();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    resetResultPinsForTests();
  });

  it("pins any version and then recognizes that version as pinned", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, filePin()));
    const changed = vi.fn();
    window.addEventListener("bowhead:result-pins-changed", changed);

    const pin = await pinResult("file", "art_v1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/result-pins");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ kind: "file", id: "art_v1" });
    expect(changed).toHaveBeenCalledTimes(1);
    expect(findResultPin([pin], "file", "art_v1")?.pin_id).toBe("art_root");
    expect(findResultPin([pin], "file", "art_v3")?.pin_id).toBe("art_root");
    expect(findResultPin([pin], "report", "art_v3")).toBeUndefined();
    window.removeEventListener("bowhead:result-pins-changed", changed);
  });

  it("unpins by the pin's identity", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(204, null));
    await unpinResult("report", "cv_1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/result-pins/report/cv_1");
    expect(init.method).toBe("DELETE");
  });
});
