import { act, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  _resetAllPlansForTesting,
  applyPlanProposed,
  applyPlanStatus,
  getPlanSnapshot,
} from "@/lib/plan-store";
import { PlanCardConnected } from "./PlanCardConnected";

const originalFetch = globalThis.fetch;

afterEach(() => {
  cleanup();
  _resetAllPlansForTesting();
  globalThis.fetch = originalFetch;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PlanCardConnected running reconciliation", () => {
  function seedRunning(): void {
    applyPlanProposed({
      plan_id: "plan_running",
      workspace_id: "ws_1",
      title: "并发审批计划",
      steps: [{
        id: "step_1",
        agent: "writer",
        objective: "完成报告",
        level: 0,
        depends_on: [],
        needs_corpus: false,
      }],
    });
    applyPlanStatus("plan_running", "running");
  }

  function stateResponse(status: "proposed" | "running" | "completed"): Response {
    return new Response(JSON.stringify({
      plan: { id: "plan_running", status, title: "并发审批计划" },
      steps: [{
        id: "step_1",
        agent: "writer",
        objective: "完成报告",
        level: 0,
        depends_on: [],
        needs_corpus: false,
        status: status === "completed" ? "done" : "running",
      }],
    }), { status: 200, headers: { "content-type": "application/json" } });
  }

  it("converges a RUNNING card to the authoritative terminal state without F5", async () => {
    vi.useFakeTimers();
    seedRunning();
    globalThis.fetch = vi.fn(async () => stateResponse("completed")) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(5_000);

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect(getPlanSnapshot("plan_running")?.status).toBe("completed");
  });

  it("retries a transient read and stops polling after terminal convergence", async () => {
    vi.useFakeTimers();
    seedRunning();
    globalThis.fetch = vi.fn()
      .mockRejectedValueOnce(new TypeError("temporary network failure"))
      .mockResolvedValueOnce(stateResponse("completed")) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    expect(getPlanSnapshot("plan_running")?.status).toBe("completed");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });

  it("does not let a late RUNNING read overwrite an SSE terminal state", async () => {
    vi.useFakeTimers();
    seedRunning();
    let resolveRead!: (response: Response) => void;
    globalThis.fetch = vi.fn(() => new Promise<Response>((resolve) => {
      resolveRead = resolve;
    })) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(5_000);
    act(() => applyPlanStatus("plan_running", "completed"));
    await act(async () => resolveRead(stateResponse("running")));

    expect(getPlanSnapshot("plan_running")?.status).toBe("completed");
  });

  it("discards a stale mount reconcile after SSE changes the snapshot", async () => {
    vi.useFakeTimers();
    applyPlanProposed({
      plan_id: "plan_mount_race",
      workspace_id: "ws_1",
      title: "历史计划",
      steps: [],
    });
    let resolveRead!: (response: Response) => void;
    globalThis.fetch = vi.fn(() => new Promise<Response>((resolve) => {
      resolveRead = resolve;
    })) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_mount_race" reconcileOnMount />);
    act(() => applyPlanStatus("plan_mount_race", "completed"));
    await act(async () => resolveRead(new Response(JSON.stringify({
      plan: { id: "plan_mount_race", status: "running", title: "历史计划" },
      steps: [],
    }), { status: 200, headers: { "content-type": "application/json" } })));

    expect(getPlanSnapshot("plan_mount_race")?.status).toBe("completed");
  });

  it("keeps RUNNING and continues observing a stale proposed snapshot", async () => {
    vi.useFakeTimers();
    seedRunning();
    globalThis.fetch = vi.fn(async () => stateResponse("proposed")) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    expect(getPlanSnapshot("plan_running")?.status).toBe("running");
  });

  it("reconciles saved steps when completed replay arrives during the read", async () => {
    const planId = "plan_completed_replay";
    applyPlanProposed({ plan_id: planId, current_rev: 1, workspace_id: "ws_1", steps: [] });
    let resolveRead!: (response: Response) => void;
    globalThis.fetch = vi.fn(() => new Promise<Response>((resolve) => { resolveRead = resolve; })) as typeof fetch;
    renderIntl(<PlanCardConnected planId={planId} reconcileOnMount />);
    act(() => applyPlanStatus(planId, "completed"));
    await act(async () => resolveRead(new Response(JSON.stringify({
      plan: { id: planId, current_rev: 2, status: "completed" },
      steps: [{ id: "step_1", agent: "writer", status: "done", success_criteria: ["ROE is 30%"] }],
    }), { status: 200 })));
    expect(getPlanSnapshot(planId)?.currentRev).toBe(2);
    expect(getPlanSnapshot(planId)?.steps[0].status).toBe("done");
    expect(getPlanSnapshot(planId)?.steps[0].successCriteria).toEqual(["ROE is 30%"]);
  });

  it("times out one hung read and continues to the next observation", async () => {
    vi.useFakeTimers();
    seedRunning();
    globalThis.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (vi.mocked(globalThis.fetch).mock.calls.length > 1) {
        return Promise.resolve(stateResponse("completed"));
      }
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(20_000);

    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    expect(getPlanSnapshot("plan_running")?.status).toBe("completed");
  });

  it("aborts observations at the total reconciliation deadline", async () => {
    vi.useFakeTimers();
    seedRunning();
    const requestSignals: AbortSignal[] = [];
    globalThis.fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.signal) requestSignals.push(init.signal);
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }) as typeof fetch;

    renderIntl(<PlanCardConnected planId="plan_running" />);
    await vi.advanceTimersByTimeAsync(2 * 60 * 60 * 1_000);
    const callsAtDeadline = vi.mocked(globalThis.fetch).mock.calls.length;
    expect(callsAtDeadline).toBeGreaterThan(1);
    expect(requestSignals.every((signal) => signal.aborted)).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(globalThis.fetch).toHaveBeenCalledTimes(callsAtDeadline);
  });
});
