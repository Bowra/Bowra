import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError, apiJson } from "./api-client";
import { useCanvasDocumentJobs } from "./use-canvas-document-jobs";
import fixture from "../__tests__/fixtures/document-draft-canvas.json";
import type { DocumentCanvasScope } from "./document-canvas-navigation";

vi.mock("./api-client", async (original) => ({
  ...(await original<typeof import("./api-client")>()),
  apiJson: vi.fn(),
}));
const scope: DocumentCanvasScope = fixture.context;
beforeEach(() => {
  vi.mocked(apiJson).mockReset();
});

it("reads one authorized page at a time and detects a repeated cursor", async () => {
  vi.mocked(apiJson)
    .mockResolvedValueOnce({ jobs: [fixture.review], next_cursor: "djob_next" })
    .mockResolvedValueOnce({ jobs: [], next_cursor: "djob_next" });
  const { result } = renderHook(() => useCanvasDocumentJobs(scope, false, false));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  expect(apiJson).toHaveBeenCalledTimes(1);
  expect(apiJson).toHaveBeenLastCalledWith(
    "/api/v1/sessions/s_doc/documents",
    expect.objectContaining({ headers: expect.objectContaining({ "X-Team-Id": "team_doc", "X-Space-Id": "ws_doc" }) }),
  );
  act(() => result.current.nextPage());
  await waitFor(() => expect(result.current.error).toBe(true));
  expect(result.current.jobs).toEqual([]);
  expect(apiJson).toHaveBeenLastCalledWith("/api/v1/sessions/s_doc/documents?after_id=djob_next", expect.anything());
});

it("a late old-scope list cannot populate a newly selected scope", async () => {
  let release!: (value: unknown) => void;
  vi.mocked(apiJson)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    )
    .mockResolvedValue({ jobs: [], next_cursor: null });
  const { result, rerender } = renderHook(({ current }) => useCanvasDocumentJobs(current, false, false), {
    initialProps: { current: scope },
  });
  rerender({ current: { ...scope, workspaceId: "ws_other", sessionId: "s_other" } });
  await waitFor(() => expect(result.current.loading).toBe(false));
  await act(async () => release({ jobs: [fixture.review] }));
  expect(result.current.jobs).toEqual([]);
  expect(apiJson).toHaveBeenLastCalledWith(
    "/api/v1/sessions/s_other/documents",
    expect.objectContaining({
      headers: expect.objectContaining({ "X-Team-Id": "team_doc", "X-Space-Id": "ws_other" }),
    }),
  );
});

it("revoked session access clears private names and requires explicit retry", async () => {
  vi.mocked(apiJson)
    .mockResolvedValueOnce({ jobs: [fixture.review] })
    .mockRejectedValue(new ApiError(403, "Unavailable"));
  const { result } = renderHook(() => useCanvasDocumentJobs(scope, false, false));
  await waitFor(() => expect(result.current.jobs).toHaveLength(1));
  await act(async () => {
    result.current.retry();
  });
  await waitFor(() => expect(result.current.denied).toBe(true));
  expect(result.current.jobs).toEqual([]);
  expect(result.current.next).toBeNull();
  expect(vi.mocked(apiJson).mock.calls.length).toBe(2);
});
