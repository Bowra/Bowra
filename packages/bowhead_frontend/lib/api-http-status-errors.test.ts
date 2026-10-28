/**
 * Handwritten fetch failures in lib/api.ts now throw HttpStatusError: keep message as “label: status”
 * (preserving callers' 404/503 checks), and attach status codes and request IDs so Copy Diagnostics can access them.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpStatusError } from "./api-client";
import { errorDiagnosticsFrom } from "./error-presentation";
import {
  clearCanvas,
  deleteCanvas,
  fetchCanvasBySession,
  fetchAdminTurnSnapshot,
  fetchSessions,
  markAllNotificationsRead,
  pinSession,
  setCanvasStatus,
  startExportJob,
} from "./api";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("admin turn snapshot response", () => {
  it("treats 204 as an absent snapshot and preserves a real 404", async () => {
    globalThis.fetch = vi.fn(async () => new Response(null, { status: 204 })) as typeof fetch;
    await expect(fetchAdminTurnSnapshot("t_known")).resolves.toBeNull();

    globalThis.fetch = vi.fn(async () => new Response("not found", { status: 404 })) as typeof fetch;
    await expect(fetchAdminTurnSnapshot("t_unknown")).rejects.toMatchObject({ status: 404 });
  });
});

describe("hand-written fetch failures carry status and request id", () => {
  it.each([
    ["sessions", () => fetchSessions()],
    ["pin", () => pinSession("s_1")],
    ["status", () => setCanvasStatus("cv_1", "archived")],
    ["notification read-all", () => markAllNotificationsRead()],
    ["clear canvas", () => clearCanvas("cv_1")],
    ["delete canvas", () => deleteCanvas("cv_1")],
    ["canvas", () => fetchCanvasBySession("s_1")],
    ["start export", () => startExportJob("cv_1", "pdf")],
  ] as const)("%s", async (label, call) => {
    globalThis.fetch = vi.fn(async () => new Response("oops", { status: 503, headers: { "x-request-id": REQUEST_ID } })) as typeof fetch;

    const error = await call().then(() => null, (e: unknown) => e);

    expect(error).toBeInstanceOf(HttpStatusError);
    expect((error as Error).message).toBe(`${label}: 503`);
    expect(errorDiagnosticsFrom(error)).toMatchObject({ status: 503, requestId: REQUEST_ID });
  });
});
