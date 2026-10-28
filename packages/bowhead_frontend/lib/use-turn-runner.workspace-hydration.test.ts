/**
 * `workspace_id missing` noise regression—observed on 116 prod 1.0.18, 2026-07-30.
 *
 * This console.error lived in the useTurnRunner hook body, logging every render,
 * doubled by StrictMode renders. workspaceId must be empty before space hydration completes—report deep links
 * especially wait for the canvas DTO before page.tsx calls setActiveId(canvas.workspaceId)—so
 * every fresh browser/incognito window immediately flooded red logs. Observed functionality was entirely normal (report rendered,
 * attachments 200, zero HTTP errors); the logs were pure noise but led testers to report a failure.
 *
 * Contract:
 *   1. No pending message (hydration window / report browsing only) → no log
 *   2. Actually sending with empty workspaceId → log (gateway returns 400 then; only here is it informative)
 *   3. Valid workspaceId → never log
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";

import { useTurnRunner } from "./use-turn-runner";

let errorSpy: ReturnType<typeof vi.spyOn>;

function missingCalls(): number {
  return errorSpy.mock.calls.filter((c: unknown[]) => String(c[0]).includes("workspace_id missing"))
    .length;
}

beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  // Explicit unmount required: pre-fix implementation logs on **every** later rerender; a previous case's
  // async runner update would count its log in the next case, attributing the failure to the wrong one.
  cleanup();
  errorSpy.mockRestore();
  vi.restoreAllMocks();
});

describe("useTurnRunner · workspace_id missing 只在真要发时报", () => {
  it("水合窗口（无待发消息）不报，重渲染多次也不报", () => {
    const { rerender } = renderHook(
      (props: { workspaceId?: string }) =>
        useTurnRunner({ mode: "sse", message: null, workspaceId: props.workspaceId }),
      { initialProps: {} },
    );
    rerender({});
    rerender({});

    expect(missingCalls()).toBe(0);
  });

  it("带空 workspaceId 真要发 → 报", () => {
    renderHook(() =>
      useTurnRunner({ mode: "sse", message: "统计各省份营业额", workspaceId: "", sendTick: 1 }),
    );

    expect(missingCalls()).toBeGreaterThan(0);
  });

  it("workspaceId 正常 → 不报", () => {
    renderHook(() =>
      useTurnRunner({ mode: "sse", message: "统计各省份营业额", workspaceId: "ws_main", sendTick: 1 }),
    );

    expect(missingCalls()).toBe(0);
  });
});
