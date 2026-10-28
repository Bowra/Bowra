import * as client from "./document-draft-client";
import type { DocumentJob } from "./document-drafts";
import fixture from "@/__tests__/fixtures/document-draft-canvas.json";
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useDocumentCanvasObjects } from "./use-document-canvas-objects";
import {
  readDocumentCanvasRoute,
  writeDocumentCanvasRoute,
  type DocumentCanvasScope,
} from "./document-canvas-navigation";

const scope: DocumentCanvasScope = {
  userId: "u_a",
  teamId: "team_a",
  workspaceId: "ws_a",
  sessionId: "s_a",
  authSessionGeneration: "auth_a",
};
const options = () => ({
  scope: scope as DocumentCanvasScope | null,
  params: new URLSearchParams(window.location.search),
});
afterEach(() => { window.history.replaceState(null, "", "/"); window.sessionStorage.clear(); vi.restoreAllMocks(); });

it("retains background instances and prevents a closed instance from updating its replacement", () => {
  window.history.replaceState(null, "", "/?session=s_a");
  const { result } = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  act(() => result.current.open("djob_a"));
  const original = result.current.objects[0].context;
  act(() => result.current.open("djob_b"));
  expect(result.current.isCurrent(original)).toBe(true);
  act(() => result.current.close(original));
  expect(result.current.objects).toHaveLength(1);
  expect(readDocumentCanvasRoute(new URLSearchParams(window.location.search))?.target.jobId).toBe("djob_b");
  act(() => result.current.open("djob_a"));
  expect(result.current.isCurrent(original)).toBe(false);
  const replacement = result.current.objects[1].context;
  expect(replacement.instanceId).not.toBe(original.instanceId);
  act(() =>
    result.current.update({
      context: original,
      title: "Old private name",
      status: "Done",
      objectStatus: "published",
      phase: "ready",
      state: "published",
      closePolicy: "detach",
    }),
  );
  expect(result.current.objects[1].update).toBeNull();
  act(() => result.current.close(original));
  expect(result.current.objects).toHaveLength(2);
});

it("drops readable content immediately when the authenticated scope changes", () => {
  const { result, rerender } = renderHook(({ scope, params }) => useDocumentCanvasObjects(scope, params, () => {}), {
    initialProps: options(),
  });
  act(() => result.current.open("djob_a"));
  const original = result.current.objects[0].context;
  rerender({ ...options(), scope: { ...scope, authSessionGeneration: "auth_new" } });
  expect(result.current.objects).toEqual([]);
  expect(result.current.isCurrent(original)).toBe(false);
  rerender({ ...options(), scope: null });
  act(() => result.current.open("djob_a"));
  expect(result.current.objects).toEqual([]);
});

it("restores exact pointers and Results without treating a stale URL echo as a reopen", () => {
  window.history.replaceState(null, "", "/bowhead/?session=s_a&report=r_a#retained");
  const onRoute = vi.fn();
  const view = renderHook(({ scope, params }) => useDocumentCanvasObjects(scope, params, onRoute), {
    initialProps: options(),
  });
  act(() => view.result.current.open("djob_a"));
  const original = view.result.current.objects[0].context;
  expect(window.location.pathname).toBe("/bowhead/");
  expect(window.location.hash).toBe("#retained");
  expect(new URLSearchParams(window.location.search).get("report")).toBe("r_a");
  act(() => view.result.current.deselect("results"));
  const saved = options();
  view.unmount();
  const restored = renderHook(({ scope, params }) => useDocumentCanvasObjects(scope, params, onRoute), {
    initialProps: saved,
  });
  expect(restored.result.current.objects[0].context.jobId).toBe("djob_a");
  expect(restored.result.current.objects[0].context.instanceId).not.toBe(original.instanceId);
  expect(restored.result.current.selectedKey).toBeNull();
  expect(onRoute).toHaveBeenLastCalledWith("results");
  act(() => restored.result.current.close(restored.result.current.objects[0].context));
  restored.rerender(saved);
  expect(restored.result.current.objects).toEqual([]);
});

it("rejects foreign route scopes and does not replay selection after unrelated query changes", () => {
  window.history.replaceState(null, "", "/?session=s_a");
  writeDocumentCanvasRoute({ ...scope, userId: "u_other", jobId: "djob_private" }, "editor");
  const onRoute = vi.fn();
  const { result, rerender } = renderHook(({ scope, params }) => useDocumentCanvasObjects(scope, params, onRoute), {
    initialProps: options(),
  });
  expect(result.current.objects).toEqual([]);
  act(() => result.current.open("djob_a"));
  const url = new URL(window.location.href);
  url.searchParams.set("result", "file");
  window.history.replaceState(null, "", url);
  rerender(options());
  expect(onRoute).not.toHaveBeenCalled();
  expect(result.current.objects).toHaveLength(1);
});

const toolPointer = { version: 1 as const, turn_id: "t_doc", session_id: scope.sessionId, user_id: scope.userId,
  team_id: scope.teamId, workspace_id: scope.workspaceId, job_id: "djob_tool", draft_digest: "a".repeat(64) };
const admission = () => ({ isCurrent: vi.fn(() => true), maySelect: vi.fn(() => true), onOpened: vi.fn() });

it("keeps a native review closed across remount and admits explicit reopening", async () => {
  window.history.replaceState(null, "", "/?session=s_a");
  const read = vi.spyOn(client, "readDocumentDraft").mockResolvedValue({ ...fixture.review, id: toolPointer.job_id } as DocumentJob);
  const mount = () => renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(window.location.search), () => {}));
  const first = mount();
  await act(() => first.result.current.openFromTool(toolPointer, admission()));
  act(() => first.result.current.close(first.result.current.objects[0].context));
  first.unmount();
  const restored = mount();
  await act(() => restored.result.current.openFromTool(toolPointer, admission()));
  expect(restored.result.current.objects).toEqual([]);
  expect(read).toHaveBeenCalledTimes(1);
  act(() => restored.result.current.open(toolPointer.job_id));
  expect(restored.result.current.objects).toHaveLength(1);
  restored.unmount();
  const explicit = mount();
  expect(explicit.result.current.objects).toHaveLength(1);
  await act(() => explicit.result.current.openFromTool(toolPointer, admission()));
  expect(read).toHaveBeenCalledTimes(2);
});

it.each(["job", "turn", "digest", "auth"])("does not dismiss a different %s identity after reload", async (change) => {
  window.history.replaceState(null, "", "/?session=s_a");
  const read = vi.spyOn(client, "readDocumentDraft").mockResolvedValue({ ...fixture.review, id: toolPointer.job_id } as DocumentJob);
  const first = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  await act(() => first.result.current.openFromTool(toolPointer, admission()));
  act(() => first.result.current.close(first.result.current.objects[0].context));
  first.unmount();
  const next = { ...toolPointer, ...(change === "job" ? { job_id: "djob_new" } : {}),
    ...(change === "turn" ? { turn_id: "t_new" } : {}), ...(change === "digest" ? { draft_digest: "b".repeat(64) } : {}) };
  read.mockResolvedValue({ ...fixture.review, id: next.job_id, origin_turn_id: next.turn_id, draft_digest: next.draft_digest } as DocumentJob);
  const restored = renderHook(() => useDocumentCanvasObjects({ ...scope, ...(change === "auth" ? { authSessionGeneration: "auth_new" } : {}) }, new URLSearchParams(), () => {}));
  await act(() => restored.result.current.openFromTool(next, admission()));
  expect(restored.result.current.objects).toHaveLength(1);
  expect(read).toHaveBeenCalledTimes(2);
});

it("fences close during a native preflight and remembers it on reload", async () => {
  let resolve!: (value: DocumentJob) => void;
  const read = vi.spyOn(client, "readDocumentDraft").mockReturnValue(new Promise((done) => { resolve = done; }));
  const first = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  act(() => first.result.current.open(toolPointer.job_id));
  let pending!: Promise<void>;
  act(() => { pending = first.result.current.openFromTool(toolPointer, admission()); });
  act(() => first.result.current.close(first.result.current.objects[0].context));
  await act(async () => { resolve({ ...fixture.review, id: toolPointer.job_id } as DocumentJob); await pending; });
  expect(first.result.current.objects).toEqual([]);
  first.unmount();
  const restored = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  await act(() => restored.result.current.openFromTool(toolPointer, admission()));
  expect(restored.result.current.objects).toEqual([]);
  expect(read).toHaveBeenCalledTimes(1);
});

it("keeps the in-memory close fence when tab storage is blocked", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
  const read = vi.spyOn(client, "readDocumentDraft").mockResolvedValue({ ...fixture.review, id: toolPointer.job_id } as DocumentJob);
  const { result } = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  await act(() => result.current.openFromTool(toolPointer, admission()));
  act(() => result.current.close(result.current.objects[0].context));
  await act(() => result.current.openFromTool(toolPointer, admission()));
  expect(result.current.objects).toEqual([]);
  expect(read).toHaveBeenCalledTimes(1);
});

it("opens only an authorized exact review and preserves explicit background selection", async () => {
  const read = vi.spyOn(client, "readDocumentDraft").mockResolvedValue({ ...fixture.review, id: toolPointer.job_id } as DocumentJob);
  const { result } = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  act(() => result.current.open("djob_manual"));
  const original = result.current.selectedKey;
  const control = admission();
  control.maySelect.mockReturnValue(false);
  await act(() => result.current.openFromTool(toolPointer, control));
  expect(result.current.objects).toHaveLength(2);
  expect(result.current.selectedKey).toBe(original);
  expect(control.onOpened).toHaveBeenCalledWith(false);
  expect(read.mock.calls[0][0]).toMatchObject({ ...scope, jobId: toolPointer.job_id });
  const background = result.current.objects[1].context;
  act(() => result.current.close(background));
  await act(() => result.current.openFromTool(toolPointer, control));
  expect(result.current.objects).toHaveLength(1);
  expect(read).toHaveBeenCalledTimes(1);
  act(() => result.current.open(toolPointer.job_id));
  expect(result.current.objects[1].context.instanceId).not.toBe(background.instanceId);
});

it.each(["digest", "turn", "denied", "foreign"])("does not retain a %s Tool pointer", async (failure) => {
  const read = vi.spyOn(client, "readDocumentDraft").mockResolvedValue({ ...fixture.review, id: toolPointer.job_id,
    ...(failure === "digest" ? { draft_digest: "b".repeat(64) } : {}),
    ...(failure === "turn" ? { origin_turn_id: "t_other" } : {}) } as DocumentJob);
  if (failure === "denied") read.mockRejectedValue(new Error("Access denied"));
  const { result } = renderHook(() => useDocumentCanvasObjects(scope, new URLSearchParams(), () => {}));
  const control = admission();
  await act(() => result.current.openFromTool({ ...toolPointer, ...(failure === "foreign" ? { user_id: "u_other" } : {}) }, control));
  expect(result.current.objects).toHaveLength(0);
  expect(control.onOpened).not.toHaveBeenCalled();
  if (failure === "foreign") expect(read).not.toHaveBeenCalled();
});

it.each(["scope", "turn", "manual", "unmount", "revocation"])("fences a late Tool read after %s changes", async (change) => {
  let resolve!: (value: DocumentJob) => void;
  vi.spyOn(client, "readDocumentDraft").mockReturnValue(new Promise((done) => { resolve = done; }));
  const view = renderHook(({ scope, params }) => useDocumentCanvasObjects(scope, params, () => {}), { initialProps: options() });
  const control = admission();
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.openFromTool(toolPointer, control); });
  if (change === "scope") view.rerender({ ...options(), scope: { ...scope, authSessionGeneration: "auth_new" } });
  if (change === "turn") control.isCurrent.mockReturnValue(false);
  if (change === "manual") act(() => { view.result.current.open(toolPointer.job_id); });
  if (change === "unmount") view.unmount();
  if (change === "revocation") act(() => view.result.current.clear());
  await act(async () => { resolve({ ...fixture.review, id: toolPointer.job_id } as DocumentJob); await pending; });
  expect(control.onOpened).not.toHaveBeenCalled();
  if (change !== "unmount") expect(view.result.current.objects).toHaveLength(change === "manual" ? 1 : 0);
});
