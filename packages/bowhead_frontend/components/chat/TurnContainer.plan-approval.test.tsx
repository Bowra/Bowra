import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import {
  _resetAllPlansForTesting,
  applyPlanProposed,
  applyPlanSnapshot,
  applyPlanStatus,
  editStepObjective,
  getPlanSnapshot,
} from "@/lib/plan-store";
import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyTurnBlockedPlan,
  pushPlanBeat,
} from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

const planApi = vi.hoisted(() => ({
  approve: vi.fn(),
  fetchState: vi.fn(),
  patch: vi.fn(),
  amend: vi.fn(),
  realCard: false,
}));

const toastSpy = vi.hoisted(() => vi.fn());
const activeSpace = vi.hoisted(() => ({ id: "ws_1" }));
vi.mock("@/lib/space-storage", async (orig) => ({
  ...await orig<typeof import("@/lib/space-storage")>(),
  getActiveSpaceId: () => activeSpace.id,
}));
vi.mock("@/lib/use-toast", async (orig) => {
  const actual = await orig<typeof import("@/lib/use-toast")>();
  return { ...actual, toast: (...args: Parameters<typeof actual.toast>) => { toastSpy(...args); return actual.toast(...args); } };
});

vi.mock("@/lib/auth-api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/auth-api")>(),
  fetchMe: vi.fn().mockResolvedValue({ id: "u_1" }),
}));

vi.mock("@/lib/plan-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/plan-api")>();
  return {
    ...actual,
    approvePlanViaSend: planApi.approve,
    fetchPlanStateAuthed: planApi.fetchState,
    patchPlanSteps: planApi.patch,
    amendPlan: planApi.amend,
  };
});

vi.mock("./plan-card", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./plan-card")>();
  return {
  PlanCardConnected: ({
    planId,
    onApprove,
    onPersistEdits,
    onAmendPlan,
    onViewReport,
    reconcileOnMount,
  }: {
    planId: string;
    onApprove?: (planId: string) => Promise<void> | void;
    onPersistEdits?: (planId: string) => Promise<void> | void;
    onAmendPlan?: (planId: string, instruction: string) => Promise<boolean>;
    onViewReport?: () => void;
    reconcileOnMount?: boolean;
  }) => planApi.realCard ? <actual.PlanCardConnected planId={planId} reconcileOnMount={reconcileOnMount} /> : (
    <>
    <button type="button" data-testid="view-report" onClick={() => onViewReport?.()}>view</button>
    <button type="button" data-testid="approve-plan" onClick={() => void onApprove?.(planId)}>
      approve
    </button>
    <button type="button" data-testid="save-plan" onClick={() => void onPersistEdits?.(planId)}>save</button>
    <button type="button" data-testid="amend-plan" onClick={() => void onAmendPlan?.(planId, "amend")}>amend</button>
    </>
  ),
  };
});

import { TurnContainer } from "./TurnContainer";

const PLAN_ID = "plan_approval_race";
const TURN_ID = "turn_plan_proposal";

function runner(): TurnRunnerState {
  return {
    user: "先给我计划",
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: TURN_ID,
    sessionId: "session_plan",
    sessionTitle: null,
    running: false,
    started: true,
    messages: [],
    userAttachments: [],
    thinking: "",
    thinkingLength: 0,
    lastThinkingHintAttachedAt: "",
    clarifications: [],
    createToolOpen: null,
    subagentCreatorOpen: null,
    solidifyOpen: null,
    plan: null,
  };
}

function seedPlan(): void {
  applyPlanProposed({
    plan_id: PLAN_ID,
    current_rev: 1,
    workspace_id: "ws_1",
    title: "审批竞态",
    steps: [{
      id: "step_1",
      agent: "writer",
      objective: "完成报告",
      level: 0,
      depends_on: [],
      needs_corpus: false,
    }],
  });
  applyCardCreated({ turnId: TURN_ID });
  pushPlanBeat(TURN_ID, PLAN_ID);
}

function rejection(code: string): ApiError {
  return new ApiError(409, code, { code });
}

beforeEach(() => {
  planApi.realCard = false;
  activeSpace.id = "ws_1";
  _resetAllPlansForTesting();
  _resetAllTurnsForTesting();
  planApi.approve.mockReset();
  planApi.fetchState.mockReset();
  planApi.patch.mockReset();
  planApi.amend.mockReset();
  seedPlan();
});

afterEach(() => {
  cleanup();
  _resetAllPlansForTesting();
  _resetAllTurnsForTesting();
});

describe("TurnContainer Plan approval reconciliation", () => {
  it("restores saved criteria in a paused current turn after replay", async () => {
    planApi.realCard = true;
    planApi.fetchState.mockResolvedValueOnce({
      plan: { id: PLAN_ID, current_rev: 2, status: "proposed", workspace_id: "ws_1" },
      steps: [{ id: "step_1", agent: "writer", objective: "Compare the supplied data", success_criteria: ["ROE is 30%"] }],
    });
    renderIntl(<TurnContainer runner={runner()} />);
    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.currentRev).toBe(2));
    expect(screen.getByText("ROE is 30%")).toBeTruthy();
    expect(planApi.approve).not.toHaveBeenCalled();
  });

  it("submits the displayed revision and refreshes a changed plan without executing it", async () => {
    planApi.approve.mockRejectedValueOnce(rejection("plan_not_approvable"));
    planApi.fetchState.mockResolvedValueOnce({ plan: { current_rev: 2, status: "proposed", retryable: true }, steps: [{ id: "step_1", agent: "writer", objective: "other tab" }] });
    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));
    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.currentRev).toBe(2));
    expect(planApi.approve).toHaveBeenCalledExactlyOnceWith(PLAN_ID, { workspaceId: "ws_1", sessionId: "session_plan", expectedRev: 1 });
    expect(getPlanSnapshot(PLAN_ID)?.status).toBe("proposed");
    expect(getPlanSnapshot(PLAN_ID)?.steps[0].objective).toBe("other tab");
  });

  it("waits for a fresh click after a pending save before approving the saved revision", async () => {
    let finishSave!: (value: unknown) => void;
    planApi.patch.mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve; }));
    planApi.approve.mockResolvedValueOnce({ stream_url: "/stream" });
    renderIntl(<TurnContainer runner={runner()} />);
    act(() => editStepObjective(PLAN_ID, "step_1", "edited"));
    fireEvent.click(screen.getByTestId("save-plan"));
    await waitFor(() => expect(planApi.patch).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByTestId("approve-plan"));
    expect(planApi.approve).not.toHaveBeenCalled();
    await act(async () => finishSave({ plan_id: PLAN_ID, status: "proposed", current_rev: 2, steps: [{ id: "step_1", agent: "writer", objective: "edited" }] }));
    expect(planApi.approve).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("approve-plan"));
    await waitFor(() => expect(planApi.approve).toHaveBeenCalledWith(PLAN_ID, expect.objectContaining({ expectedRev: 2 })));
  });

  it("invalidates optimistic steps if both saving and state reconciliation fail", async () => {
    planApi.patch.mockRejectedValueOnce(new TypeError("save response lost"));
    planApi.fetchState.mockRejectedValueOnce(new TypeError("state unavailable"));
    renderIntl(<TurnContainer runner={runner()} />);
    act(() => editStepObjective(PLAN_ID, "step_1", "unsaved edit"));
    fireEvent.click(screen.getByTestId("save-plan"));
    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.currentRev).toBeUndefined());
    expect(getPlanSnapshot(PLAN_ID)?.steps[0].objective).toBe("unsaved edit");
  });

  it("reloads the current revision after a stale save without automatically retrying", async () => {
    planApi.patch.mockRejectedValueOnce(rejection("plan_not_approvable"));
    planApi.fetchState.mockResolvedValueOnce({ plan: { current_rev: 2, status: "proposed" }, steps: [{ id: "step_1", agent: "writer", objective: "other tab" }] });
    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("save-plan"));
    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.currentRev).toBe(2));
    expect(getPlanSnapshot(PLAN_ID)?.steps[0].objective).toBe("other tab");
    expect(planApi.patch).toHaveBeenCalledTimes(1);
  });

  it("does not overwrite later local edits with a delayed natural-language preview", async () => {
    let finishPreview!: (result: unknown) => void;
    planApi.amend.mockImplementationOnce(() => new Promise((resolve) => { finishPreview = resolve; }));
    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("amend-plan"));
    await waitFor(() => expect(planApi.amend).toHaveBeenCalledTimes(1));
    act(() => editStepObjective(PLAN_ID, "step_1", "keep my later edit"));
    await act(async () => finishPreview({ expected_rev: 1, steps: [{ id: "step_1", agent: "writer", objective: "old preview" }] }));
    expect(getPlanSnapshot(PLAN_ID)?.steps[0].objective).toBe("keep my later edit");
    expect(planApi.patch).not.toHaveBeenCalled();
  });

  it("saves queued edits with each confirmed revision without discarding the later edit", async () => {
    let finishFirst!: (result: unknown) => void;
    planApi.patch.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }));
    planApi.patch.mockImplementationOnce(async (_id, steps) => ({ plan_id: PLAN_ID, status: "proposed", current_rev: 3, steps: steps.map((step: { id: string; objective: string }) => ({ ...step, agent: "writer" })) }));
    renderIntl(<TurnContainer runner={runner()} />);
    act(() => editStepObjective(PLAN_ID, "step_1", "first edit"));
    fireEvent.click(screen.getByTestId("save-plan"));
    await waitFor(() => expect(planApi.patch).toHaveBeenCalledTimes(1));
    expect(planApi.patch.mock.calls[0][2].expectedRev).toBe(1);
    act(() => editStepObjective(PLAN_ID, "step_1", "later edit"));
    fireEvent.click(screen.getByTestId("save-plan"));
    await act(async () => finishFirst({ plan_id: PLAN_ID, status: "proposed", current_rev: 2, steps: [{ id: "step_1", agent: "writer", objective: "first edit" }] }));
    await waitFor(() => expect(planApi.patch).toHaveBeenCalledTimes(2));
    expect(planApi.patch.mock.calls[1][2].expectedRev).toBe(2);
    expect(planApi.patch.mock.calls[1][1][0].objective).toBe("later edit");
    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.currentRev).toBe(3));
    expect(getPlanSnapshot(PLAN_ID)?.steps[0].objective).toBe("later edit");
  });

  it("dismisses a cancelled authoritative state after a concurrent 409", async () => {
    planApi.approve.mockRejectedValueOnce(rejection("plan_already_running"));
    planApi.fetchState.mockResolvedValueOnce({
      plan_id: PLAN_ID,
      plan: { id: PLAN_ID, status: "cancelled", title: "审批竞态" },
      steps: [],
    });

    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));

    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)).toBeUndefined());
  });

  it("keeps plan_not_approvable non-editable when its state read fails", async () => {
    planApi.approve.mockRejectedValueOnce(rejection("plan_not_approvable"));
    planApi.fetchState.mockRejectedValueOnce(new TypeError("state read failed"));

    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));

    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)?.status).toBe("running"));
  });

  it("dismisses a definitive plan_not_found without leaving a retryable ghost card", async () => {
    planApi.approve.mockRejectedValueOnce(
      new ApiError(404, "plan_not_found", { code: "plan_not_found" }),
    );

    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));

    await waitFor(() => expect(getPlanSnapshot(PLAN_ID)).toBeUndefined());
    expect(planApi.fetchState).not.toHaveBeenCalled();
  });

  it("does not overwrite an SSE terminal state with a late RUNNING read", async () => {
    let resolveState!: (state: unknown) => void;
    planApi.approve.mockImplementationOnce(async () => {
      throw new TypeError("approval response lost");
    });
    planApi.fetchState.mockImplementationOnce(() => new Promise((resolve) => {
      resolveState = resolve;
    }));

    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));
    await waitFor(() => expect(planApi.fetchState).toHaveBeenCalledTimes(1));
    act(() => applyPlanStatus(PLAN_ID, "completed"));
    await act(async () => resolveState({
      plan_id: PLAN_ID,
      plan: { id: PLAN_ID, status: "running", title: "审批竞态" },
      steps: [],
    }));

    expect(getPlanSnapshot(PLAN_ID)?.status).toBe("completed");
  });

  it("keeps an ambiguous approval non-editable when state is still proposed", async () => {
    let resolveState!: (state: unknown) => void;
    planApi.approve.mockRejectedValueOnce(new TypeError("approval response lost"));
    planApi.fetchState.mockImplementationOnce(() => new Promise((resolve) => {
      resolveState = resolve;
    }));

    renderIntl(<TurnContainer runner={runner()} />);
    fireEvent.click(screen.getByTestId("approve-plan"));
    await waitFor(() => expect(planApi.fetchState).toHaveBeenCalledTimes(1));
    await act(async () => resolveState({
      plan_id: PLAN_ID,
      plan: { id: PLAN_ID, status: "proposed", title: "审批竞态" },
      steps: [],
    }));

    expect(getPlanSnapshot(PLAN_ID)?.status).toBe("running");
  });
});

describe("TurnContainer plan edit and amend failures", () => {
  const REQUEST_ID = "0123456789abcdef0123456789abcdef";
  const failure = (status: number, message: string) =>
    new ApiError(status, message, { requestId: REQUEST_ID });

  it.each([
    ["zh-CN", "至少要保留一个步骤", "复制诊断"],
    ["en-US", "at least one step", "Copy diagnostics"],
    ["de-DE", "mindestens einen Schritt", "Diagnoseinformationen kopieren"],
  ] as const)("a rejected edit says how to fix it, not the raw detail (%s)", async (locale, fix, copy) => {
    toastSpy.mockReset();
    planApi.patch.mockRejectedValueOnce(failure(422, "plan must keep at least one step"));
    planApi.fetchState.mockRejectedValue(new Error("offline"));
    renderIntl(<TurnContainer runner={runner()} />, { locale });

    fireEvent.click(screen.getByTestId("save-plan"));

    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    const shown = toastSpy.mock.calls.at(-1)![0];
    expect(shown.text).toContain(fix);
    expect(shown.text).not.toMatch(/422|plan must keep/);
    expect(shown.action?.label).toBe(copy);
  });

  it.each([
    ["zh-CN", "复制诊断"],
    ["en-US", "Copy diagnostics"],
    ["de-DE", "Diagnoseinformationen kopieren"],
  ] as const)("a failed amend keeps the status code out of the toast (%s)", async (locale, copy) => {
    toastSpy.mockReset();
    planApi.amend.mockRejectedValueOnce(failure(503, "plan.amend 503: multi_agent_registry unavailable"));
    renderIntl(<TurnContainer runner={runner()} />, { locale });

    fireEvent.click(screen.getByTestId("amend-plan"));

    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    const shown = toastSpy.mock.calls.at(-1)![0];
    expect(shown.text).not.toMatch(/503|plan\.amend|registry/);
    expect(shown.action?.label).toBe(copy);
  });
  it.each([
    ["zh-CN", "报告没能打开", "复制诊断"],
    ["en-US", "The report couldn't be opened", "Copy diagnostics"],
    ["de-DE", "Der Bericht konnte nicht geöffnet werden", "Diagnoseinformationen kopieren"],
  ] as const)("View report failure no longer shows canvas: 500 (%s)", async (locale, text, copy) => {
    toastSpy.mockReset();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () => new Response("oops", { status: 500, headers: { "x-request-id": REQUEST_ID } })) as typeof fetch;
    try {
      renderIntl(<TurnContainer runner={runner()} />, { locale });
      fireEvent.click(screen.getByTestId("view-report"));
      // View Report first dynamically loads lib/api; under heavy machine load, the first case can exceed the default 1 second.
      await waitFor(() => expect(toastSpy).toHaveBeenCalled(), { timeout: 5000 });
    } finally {
      globalThis.fetch = originalFetch;
    }
    const shown = toastSpy.mock.calls.at(-1)![0];
    expect(shown.text).toContain(text);
    expect(shown.text).not.toMatch(/canvas|500/);
    expect(shown.action?.label).toBe(copy);
  });
});

describe("plan_blocked failure card resumes the blocked plan", () => {
  function blockedRunner(): TurnRunnerState {
    return {
      ...runner(),
      errorReason: { source: "turn_failed", reasonCode: "plan_blocked", recoveryAction: "edit_and_resend" },
    };
  }
  function blockPlan(): void {
    applyPlanStatus(PLAN_ID, "blocked");
    applyTurnBlockedPlan(TURN_ID, PLAN_ID);
  }

  it("re-approves the plan at the shown revision after the server confirms it is unchanged", async () => {
    blockPlan();
    planApi.fetchState.mockResolvedValueOnce({ plan: { id: PLAN_ID, status: "blocked", current_rev: 1 }, steps: [] });
    planApi.approve.mockResolvedValueOnce({ stream_url: "/stream" });
    renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
    fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
    await waitFor(() => expect(planApi.approve).toHaveBeenCalledExactlyOnceWith(
      PLAN_ID, { workspaceId: "ws_1", sessionId: "session_plan", expectedRev: 1 },
    ));
    expect(planApi.fetchState).toHaveBeenCalledWith(PLAN_ID, { workspaceId: "ws_1" });
  });

  it.each([
    ["the plan moved to a newer revision", { plan: { id: PLAN_ID, status: "blocked", current_rev: 2 }, steps: [] }],
    ["the plan is no longer blocked", { plan: { id: PLAN_ID, status: "running", current_rev: 1 }, steps: [] }],
  ])("falls back to Edit and send when %s", async (_why, remote) => {
    blockPlan();
    planApi.fetchState.mockResolvedValueOnce(remote);
    renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
    fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
    await waitFor(() => expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull());
    expect(planApi.approve).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "编辑后发送" })).toBeTruthy();
  });

  it.each(["plan_not_approvable", "plan_already_running"])(
    "falls back to Edit and send when the approval itself is refused (409 %s)",
    async (code) => {
      blockPlan();
      planApi.fetchState
        .mockResolvedValueOnce({ plan: { id: PLAN_ID, status: "blocked", current_rev: 1 }, steps: [] })
        .mockResolvedValue({ plan: { id: PLAN_ID, status: "blocked", current_rev: 1 }, steps: [] });
      planApi.approve.mockRejectedValueOnce(rejection(code));
      renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
      fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
      await waitFor(() => expect(planApi.approve).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(screen.getByText(/计划已经变了/)).toBeTruthy());
      expect(screen.getByRole("button", { name: "编辑后发送" })).toBeTruthy();
    },
  );

  it("offers resuming again once the plan moves to a new blocked revision", async () => {
    blockPlan();
    planApi.fetchState.mockResolvedValueOnce({ plan: { id: PLAN_ID, status: "blocked", current_rev: 2 }, steps: [] });
    renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
    fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
    await waitFor(() => expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull());
    act(() => applyPlanSnapshot({ plan_id: PLAN_ID, plan: { id: PLAN_ID, status: "blocked", current_rev: 2, workspace_id: "ws_1" } }));
    await waitFor(() => expect(screen.getByTestId("final-fail-resume-plan")).toBeTruthy());
    expect(screen.queryByText(/计划已经变了/)).toBeNull();
  });

  it("falls back when the plan's Space can no longer be read", async () => {
    blockPlan();
    planApi.fetchState.mockRejectedValueOnce(new Error("plan.state_authed 403"));
    renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
    fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
    await waitFor(() => expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull());
    expect(planApi.approve).not.toHaveBeenCalled();
  });

  it("does not offer resuming after the user switched to another Space", () => {
    blockPlan();
    activeSpace.id = "ws_other";
    renderIntl(<TurnContainer runner={blockedRunner()} onComposerPrefill={vi.fn()} />);
    expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull();
    expect(screen.getByRole("button", { name: "编辑后发送" })).toBeTruthy();
  });
});
