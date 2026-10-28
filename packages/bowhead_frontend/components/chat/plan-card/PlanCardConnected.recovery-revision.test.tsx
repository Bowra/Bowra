import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllPlansForTesting, applyPlanSnapshot, getPlanSnapshot } from "@/lib/plan-store";
import { PlanCardConnected } from "./PlanCardConnected";

const originalFetch = globalThis.fetch;
const planId = "plan_recovery_version";
const failedStep = { id: "ps_failed", agent: "compute", objective: "original task", status: "failed", level: 0, depends_on: [], included: true, needs_corpus: false };
const revisedStep = { ...failedStep, id: "ps_revised", objective: "reviewed task", success_criteria: ["use the reviewed method"], status: "pending" };

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

function seed(revision = 1): void {
  applyPlanSnapshot({ plan_id: planId, plan: { status: "blocked", current_rev: revision, workspace_id: "ws_1" }, steps: [failedStep] });
}

async function preview(kind: "revise" | "replan"): Promise<void> {
  if (kind === "revise") {
    const bar = document.querySelector(".pblock-acts")!;
    fireEvent.click([...bar.querySelectorAll("button")].find((button) => button.textContent?.includes("修订重试"))!);
  } else {
    fireEvent.click(screen.getByText("重新规划"));
    const input = document.querySelector(".pnl input")!;
    fireEvent.change(input, { target: { value: "use another method" } });
    fireEvent.keyDown(input, { key: "Enter" });
  }
  await screen.findByText("确认并重跑");
}

afterEach(() => {
  cleanup();
  _resetAllPlansForTesting();
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("Plan recovery retains the confirmed revision", () => {
  it.each(["revise", "replan"] as const)("%s confirms and resumes exactly the returned revision", async (kind) => {
    seed();
    const bodies: Array<Record<string, unknown>> = [];
    globalThis.fetch = vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      bodies.push(body);
      if (body.confirm) return response({ current_rev: 2, steps: [revisedStep] });
      return response({ expected_rev: 1, revised: { ...revisedStep, agent_name: "compute" }, new_steps: [revisedStep], kept_done: [], superseded: [failedStep.id] });
    }) as typeof fetch;
    const approvedRevisions: Array<number | undefined> = [];
    const approve = vi.fn(() => { approvedRevisions.push(getPlanSnapshot(planId)?.currentRev); });
    renderIntl(<PlanCardConnected planId={planId} onApprove={approve} />);
    await preview(kind);
    fireEvent.click(screen.getByText("确认并重跑"));
    await waitFor(() => expect(approve).toHaveBeenCalledExactlyOnceWith(planId));
    expect(approvedRevisions).toEqual([2]);
    expect(bodies[1]).toMatchObject({ confirm: true, expected_rev: 1 });
    expect(getPlanSnapshot(planId)?.steps[0].objective).toBe("reviewed task");
  });

  it("skip sends the displayed revision and resumes with the saved graph", async () => {
    seed();
    const fetcher = vi.fn(async () => response({ current_rev: 2, steps: [{ ...failedStep, status: "skipped" }] }));
    globalThis.fetch = fetcher as typeof fetch;
    const approvedRevisions: Array<number | undefined> = [];
    const approve = vi.fn(() => { approvedRevisions.push(getPlanSnapshot(planId)?.currentRev); });
    renderIntl(<PlanCardConnected planId={planId} onApprove={approve} />);
    const bar = document.querySelector(".pblock-acts")!;
    fireEvent.click([...bar.querySelectorAll("button")].find((button) => button.textContent?.includes("跳过该步"))!);
    await waitFor(() => expect(approve).toHaveBeenCalledTimes(1));
    expect(approvedRevisions).toEqual([2]);
    expect(JSON.parse(String((fetcher.mock.calls[0] as unknown as [string, RequestInit])[1].body))).toMatchObject({ expected_rev: 1 });
    expect(getPlanSnapshot(planId)?.steps[0].status).toBe("skipped");
  });

  it.each(["revise", "replan"] as const)("%s ignores a late confirmation after the displayed graph changes", async (kind) => {
    seed();
    let finish!: (value: Response) => void;
    globalThis.fetch = vi.fn(async (_url, init) => {
      if (JSON.parse(String(init?.body)).confirm) return new Promise<Response>((resolve) => { finish = resolve; });
      return response({ expected_rev: 1, revised: { ...revisedStep, agent_name: "compute" }, new_steps: [revisedStep], kept_done: [], superseded: [failedStep.id] });
    }) as typeof fetch;
    const approve = vi.fn();
    renderIntl(<PlanCardConnected planId={planId} onApprove={approve} />);
    await preview(kind);
    fireEvent.click(screen.getByText("确认并重跑"));
    await waitFor(() => expect(finish).toBeDefined());
    act(() => seed(3));
    await act(async () => finish(response({ current_rev: 2, steps: [revisedStep] })));
    expect(approve).not.toHaveBeenCalled();
    expect(getPlanSnapshot(planId)?.currentRev).toBe(3);
    expect(getPlanSnapshot(planId)?.steps[0].objective).toBe("original task");
  });
});
