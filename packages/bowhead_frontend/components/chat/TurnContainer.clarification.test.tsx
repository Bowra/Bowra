/**
 * dingsiyan #11 (test review #9 / P1-A) — TurnContainer clarification orchestration.
 *
 * Core user-visible flow: select → Continue (D2) → collapse to the answered row → resume.
 * Pin P1-A: the answered summary row must disappear after resume completes (otherwise lingering "已确认：X" =
 * a new version of the reporter's "面板不收起" complaint).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { _resetClarificationTombstonesForTest } from "@/lib/clarification-tombstone";
import { ApiError, readApiError } from "@/lib/api-client";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => Object.assign((key: string) => key, { rich: (key: string) => key }),
}));

const resolveMock = vi.fn();
const resumeMock = vi.fn();
const listMock = vi.fn();
vi.mock("@/lib/api", () => ({
  resolveClarification: (...a: unknown[]) => resolveMock(...a),
  resumeTurnAfterClarification: (...a: unknown[]) => resumeMock(...a),
  listClarificationsForTurn: (...a: unknown[]) => listMock(...a),
}));

const toastMock = vi.fn();
vi.mock("@/lib/use-toast", () => ({
  toast: (...a: unknown[]) => toastMock(...a),
}));

import { TurnContainer } from "./TurnContainer";

afterEach(() => cleanup());
beforeEach(() => {
  _resetClarificationTombstonesForTest();
  resolveMock.mockReset();
  resumeMock.mockReset();
  listMock.mockReset();
  toastMock.mockReset();
});

/** D2: picking an option only selects it; "Continue" submits. */
async function choose(label: string) {
  await act(async () => {
    fireEvent.click(screen.getByText(label));
  });
  await act(async () => {
    fireEvent.click(screen.getByText("continue"));
  });
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: null,
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: null,
    sessionId: null,
    sessionTitle: null,
    running: false,
    started: false,
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
    ...over,
  };
}

const PENDING = {
  clarificationId: "c1",
  candidates: ["报告A", "报告B"],
  fallbackText: "您想重试哪个报告？",
  questions: undefined,
};

// 2026-09-04 HITL audit §4.1: the multi-select path previously sent the card's rendered **display names** back unchanged.
// Single-select already consulted valueByLabel; multi-select did not, which breaks as soon as `option_labels` is connected.
const PENDING_MULTI_RELABELED = {
  clarificationId: "c2",
  candidates: [],
  fallbackText: "Which dimensions?",
  questions: [
    {
      question: "Which dimensions?",
      options: ["一级品牌名称英文", "品规"],
      option_labels: ["Brand", "SKU"],
      type: "multi_select",
    },
  ],
};

describe("TurnContainer clarification orchestration", () => {
  it("keeps an answered clarification blocked without retrying its resolve request", async () => {
    const diagnostics = {
      schemaVersion: 1 as const, disclosure: "authenticated" as const,
      code: "clarification_resume_unavailable", diagnosticCode: "rule_clarification_failure",
      safeDetail: "phase=state_change; failure_class=state", retryable: false,
      requestId: "0123456789abcdef0123456789abcdef",
    };
    resolveMock.mockRejectedValue(await readApiError(new Response(JSON.stringify({
      detail: { error: { code: "clarification_resume_unavailable", message: "Continuation unavailable",
        diagnostics: { schema_version: 1, disclosure: "authenticated",
          diagnostic_code: "rule_clarification_failure", retryable: false,
          safe_detail: "phase=state_change; failure_class=state" } } },
    }), { status: 409, headers: { "x-request-id": diagnostics.requestId } })));
    let finishRead!: (value: unknown) => void;
    listMock.mockReturnValue(new Promise((resolve) => { finishRead = resolve; }));
    const onSend = vi.fn();
    const attachStream = vi.fn();
    const state = runner({ turnId: "t_original", executionGeneration: 1,
      user: "Inspect the original files", clarificationPending: PENDING, attachStream });
    const { rerender } = render(<TurnContainer runner={state} onSend={onSend} />);
    await choose("报告A");
    await waitFor(() => expect(listMock).toHaveBeenCalledWith("t_original"));
    expect(screen.queryByRole("button", { name: "retry" })).toBeNull();
    expect(screen.getByTestId("final-fail")).toHaveTextContent("failure_card.clarification_answer_unconfirmed_body");
    await act(async () => finishRead({ items: [], resume_failure: {
      request_id: "c1", turn_id: "t_original", execution_generation: 1,
      code: "clarification_resume_unavailable", recovery_action: "copy_diagnostics", diagnostics,
    } }));
    expect(screen.getByTestId("final-fail")).toHaveTextContent("failure_card.clarification_answer_saved_body");
    expect(screen.getByRole("button", { name: "copy_diagnostics" })).toBeTruthy();
    expect(screen.queryByTestId("clarification-card")).toBeNull();
    rerender(<TurnContainer runner={{ ...state }} onSend={onSend} />);
    expect(screen.queryByTestId("clarification-card")).toBeNull();
    expect(resolveMock).toHaveBeenCalledTimes(1);
    expect(resumeMock).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(attachStream).not.toHaveBeenCalled();
  });

  it.each(["mismatched", "unavailable"])("does not claim an answer was saved from %s authority", async (mode) => {
    resolveMock.mockRejectedValue(new ApiError(409, "Continuation unavailable", {
      code: "clarification_resume_unavailable", diagnostics: {
        schemaVersion: 1, disclosure: "authenticated", code: "clarification_resume_unavailable",
        diagnosticCode: "rule_clarification_failure", retryable: false,
      },
    }));
    if (mode === "unavailable") listMock.mockRejectedValue(new Error("Read unavailable"));
    else listMock.mockResolvedValue({ items: [], resume_failure: {
      request_id: "different_card", turn_id: "t_original", execution_generation: 1,
      code: "clarification_resume_unavailable", recovery_action: "copy_diagnostics",
    } });
    render(<TurnContainer runner={runner({ turnId: "t_original", executionGeneration: 1, clarificationPending: PENDING })} />);
    await choose("报告A");
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.getByTestId("final-fail")).toHaveTextContent("failure_card.clarification_answer_unconfirmed_body");
    expect(screen.getByTestId("final-fail")).not.toHaveTextContent("failure_card.clarification_answer_saved_body");
    expect(screen.queryByRole("button", { name: "retry" })).toBeNull();
    expect(resolveMock).toHaveBeenCalledTimes(1);
  });

  it("discards a late authority response after switching to another turn", async () => {
    resolveMock.mockRejectedValue(new ApiError(409, "Continuation unavailable", {
      code: "clarification_resume_unavailable", diagnostics: {
        schemaVersion: 1, disclosure: "authenticated", code: "clarification_resume_unavailable",
        diagnosticCode: "rule_clarification_failure", retryable: false,
      },
    }));
    let finishRead!: (value: unknown) => void;
    listMock.mockReturnValue(new Promise((resolve) => { finishRead = resolve; }));
    const { rerender } = render(<TurnContainer runner={runner({ turnId: "t_original", executionGeneration: 1,
      clarificationPending: PENDING })} />);
    await choose("报告A");
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    rerender(<TurnContainer runner={runner({ turnId: "t_next", executionGeneration: 1,
      user: "A different request", clarificationPending: { ...PENDING, clarificationId: "c_next" } })} />);
    await act(async () => finishRead({ items: [], resume_failure: {
      request_id: "c1", turn_id: "t_original", execution_generation: 1,
      code: "clarification_resume_unavailable", recovery_action: "copy_diagnostics",
    } }));
    expect(screen.queryByTestId("final-fail")).toBeNull();
    expect(screen.getByTestId("clarification-card")).toBeTruthy();
    expect(resolveMock).toHaveBeenCalledTimes(1);
  });

  it("submits the original single question and selected candidate as a typed answer", async () => {
    const question = "Total parcels 用哪个口径?(West=3810、East=2190 已按您的指示固定)";
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    render(<TurnContainer runner={runner({ turnId: "t_single", clarificationPending: {
      clarificationId: "c_single", kind: "ambiguous_turn", fallbackText: question,
      candidates: ["按数据相加:6000", "按我的业务备注:6200"], multiSelect: false,
    } })} />);
    await choose("按数据相加:6000");
    await waitFor(() => expect(resolveMock).toHaveBeenCalledWith("c_single", {
      chosen_value: "按数据相加:6000",
      answers: [{ question, source: "candidate", answer: "按数据相加:6000", option_index: 0 }],
    }));
  });

  it("preserves custom single answers even when they equal a translated option label", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    render(<TurnContainer runner={runner({ turnId: "t_custom", clarificationPending: {
      clarificationId: "c_custom", candidates: [], questions: [{
        question: "Which measure?", options: ["revenue"], option_labels: ["Sales"], type: "single_select",
      }],
    } })} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Sales" } });
    fireEvent.click(screen.getByText("continue"));
    await waitFor(() => expect(resolveMock).toHaveBeenCalledWith("c_custom", {
      chosen_value: "Sales", answers: [{ question: "Which measure?", source: "custom", answer: "Sales" }],
    }));
  });

  it("keeps connector selection as a canonical scalar with its remember preference", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    render(<TurnContainer runner={runner({ turnId: "t_connector", clarificationPending: {
      clarificationId: "c_connector", kind: "tool_context_select", candidates: [], fallbackText: "Which workspace?",
      options: [{ label: "Retail", value: "project_retail" }], rememberOption: true,
    } })} />);
    await choose("Retail");
    await waitFor(() => expect(resolveMock).toHaveBeenCalledWith("c_connector", { chosen_value: "project_retail", remember: true }));
  });

  it("attaches a settled document clarification without creating another turn", async () => {
    resolveMock.mockResolvedValue({action: "turn_settled", stream_url: "/api/v1/chat/stream/t_doc", execution_generation: 1});
    const onSend = vi.fn();
    const attachStream = vi.fn();
    const clearClarificationPending = vi.fn();
    render(<TurnContainer onSend={onSend} runner={runner({turnId: "t_doc", user: "Revise the presentation", clarificationPending: PENDING, attachStream, clearClarificationPending})} />);
    await choose("报告A");
    await waitFor(() => expect(attachStream).toHaveBeenCalledWith("/api/v1/chat/stream/t_doc", 1));
    expect(clearClarificationPending).toHaveBeenCalledWith("c1");
    expect(resumeMock).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("后端判定多选、但只发 candidates 时，合成问题继承该判定（不再渲成单选）", async () => {
    // 2026-09-07: the backend's computed multi_select previously never entered clarificationPending,
    // so CH2 combined N≥2 cards sending only candidates were hardcoded to single_select,
    // allowing the user to select only one before submission.
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    render(
      <TurnContainer
        runner={runner({
          turnId: "t9",
          user: "q",
          clarificationPending: { ...PENDING, multiSelect: true },
        })}
      />,
    );
    // Multi-select: checkboxes; selecting an option never submits.
    await act(async () => {
      fireEvent.click(screen.getByText("报告A"));
    });
    expect(resolveMock).not.toHaveBeenCalled();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);

    await act(async () => {
      fireEvent.click(screen.getByText("报告B"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("continue"));
    });
    await waitFor(() => expect(resolveMock).toHaveBeenCalled());
    const [, payload] = resolveMock.mock.calls[0] as [string, Record<string, unknown>];
    expect(payload.chosen_values).toEqual(["报告A", "报告B"]);
  });

  it("keeps immediate scalar submission for legacy candidate cards", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    render(
      <TurnContainer
        runner={runner({ turnId: "t10", user: "q", clarificationPending: PENDING })}
      />,
    );
    await choose("报告A");
    await waitFor(() =>
      expect(resolveMock).toHaveBeenCalledWith("c1", { chosen_value: "报告A" }),
    );
  });

  it("多选卡渲展示名、提交规范值（单选路早就这样，多选路对齐）", async () => {
    resolveMock.mockResolvedValue({
      action: "turn_resume",
      stream_url: "/api/v1/chat/stream/t2",
      execution_generation: 2,
    });
    const attachStream = vi.fn();
    render(
      <TurnContainer
        runner={runner({
          turnId: "t2",
          user: "Which dimensions do I break down by?",
          clarificationPending: PENDING_MULTI_RELABELED,
          attachStream,
        })}
      />,
    );

    // The user sees English display names; Chinese canonical names do not appear in the DOM.
    expect(screen.getByText("Brand")).toBeTruthy();
    expect(screen.queryByText("一级品牌名称英文")).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByText("Brand"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("SKU"));
    });
    await act(async () => {
      fireEvent.click(screen.getByText("continue"));
    });

    await waitFor(() => expect(resolveMock).toHaveBeenCalled());
    const [, payload] = resolveMock.mock.calls[0] as [string, Record<string, unknown>];
    expect(payload.chosen_values).toEqual(["一级品牌名称英文", "品规"]);
    expect(
      (payload.answers as Array<{ answer?: string }>).map((a) => a.answer),
    ).toEqual(["一级品牌名称英文", "品规"]);
  });

  it("原子 resolve+resume 回执直接 attach，不再依赖第二个请求", async () => {
    resolveMock.mockResolvedValue({
      action: "turn_resume",
      stream_url: "/api/v1/chat/stream/t1",
      execution_generation: 2,
    });
    const attachStream = vi.fn();
    render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "重试",
          clarificationPending: PENDING,
          attachStream,
        })}
      />,
    );

    await choose("报告A");
    await waitFor(() =>
      expect(attachStream).toHaveBeenCalledWith("/api/v1/chat/stream/t1", 2),
    );
    expect(resumeMock).not.toHaveBeenCalled();
  });

  it("单选点击 → 调 resolve+resume+attachStream，卡片收起为已答摘要", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    resumeMock.mockResolvedValue({
      stream_url: "/api/v1/chat/stream/t1",
      execution_generation: 2,
    });
    const attachStream = vi.fn();
    const { rerender } = render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "重试",
          clarificationPending: PENDING,
          attachStream,
        })}
      />,
    );
    // Render picker options.
    expect(screen.getByText("报告A")).toBeTruthy();

    await choose("报告A");
    await waitFor(() =>
      expect(resolveMock).toHaveBeenCalledWith("c1", { chosen_value: "报告A" }),
    );
    await waitFor(() => expect(resumeMock).toHaveBeenCalledWith("c1"));
    expect(attachStream).toHaveBeenCalledWith("/api/v1/chat/stream/t1", 2);
    // Optimistic: collapse the card into a summary row (tombstone hit → memo returns null; answeredSummary displays).
    rerender(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "重试",
          clarificationPending: PENDING,
          attachStream,
          running: true,
        })}
      />,
    );
    expect(screen.getByTestId("clarification-answered")).toBeTruthy();
    expect(screen.getByText("answered_line")).toBeTruthy(); // The answered row.
  });

  it("P1-A: resume 完成（running true→false）后摘要行消失，不悬浮残留", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    resumeMock.mockResolvedValue({ stream_url: "/api/v1/chat/stream/t1" });
    const attachStream = vi.fn();
    const base = { turnId: "t1", user: "重试", attachStream };
    const { rerender } = render(
      <TurnContainer
        runner={runner({ ...base, clarificationPending: PENDING })}
      />,
    );
    await choose("报告A");
    await waitFor(() => expect(resumeMock).toHaveBeenCalled());

    // Resume starts: running=true (summary row remains, showing the liveness row).
    rerender(
      <TurnContainer
        runner={runner({ ...base, clarificationPending: null, running: true })}
      />,
    );
    expect(screen.getByTestId("clarification-answered")).toBeTruthy();
    expect(screen.getByTestId("clarification-resuming")).toBeTruthy();

    // Resume finishes: running=false + an answer (done completed, same turnId).
    rerender(
      <TurnContainer
        runner={runner({
          ...base,
          clarificationPending: null,
          running: false,
          answer: "分析完成",
        })}
      />,
    );
    await waitFor(() =>
      expect(screen.queryByTestId("clarification-answered")).toBeNull(),
    );
  });

  it("resolve 成功但 resume 失败：不复活卡片（不撞 409），提示刷新", async () => {
    resolveMock.mockResolvedValue({ action: "turn_resume" });
    resumeMock.mockRejectedValue(new ApiError(500, "resume raw 500"));
    const { rerender } = render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "重试",
          clarificationPending: PENDING,
        })}
      />,
    );
    await choose("报告A");
    await waitFor(() => expect(resumeMock).toHaveBeenCalled());
    // Resume fails → the answered row says the choice was saved and offers Refresh (no toast);
    // the card stays dismissed (retain the tombstone).
    await waitFor(() =>
      expect(screen.getByTestId("clarification-resume-failed")).toBeTruthy(),
    );
    expect(screen.getByText("refresh")).toBeTruthy();
    expect(toastMock).not.toHaveBeenCalled();
    // Replaying clarification_required (gateway full replay) must not revive the card.
    rerender(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "重试",
          clarificationPending: PENDING,
        })}
      />,
    );
    expect(screen.queryByText("您想重试哪个报告？")).toBeNull(); // The card's question no longer appears.
    expect(screen.getByTestId("clarification-resume-failed")).toBeTruthy(); // The settled row stays.
  });

  it("resolve 失败（精确 stale 409）：收成「已过期」行、可重新提问、无技术详情、不静默起新 turn", async () => {
    resolveMock.mockRejectedValue(
      new ApiError(409, "expired_or_handled", {
        code: "clarification_not_resolvable",
        diagnostics: {
          schemaVersion: 1,
          disclosure: "authenticated",
          status: 409,
          code: "clarification_not_resolvable",
          diagnosticCode: "rule_clarification_failure",
          safeDetail: "phase=state_change; failure_class=state",
          retryable: false,
        },
      }),
    );
    const onSend = vi.fn();
    const onComposerPrefill = vi.fn();
    render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "q",
          clarificationPending: PENDING,
        })}
        onSend={onSend}
        onComposerPrefill={onComposerPrefill}
      />,
    );
    await choose("报告A");
    await waitFor(() => expect(screen.getByTestId("clarification-expired")).toBeTruthy());
    expect(toastMock).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled(); // No longer silently start a new turn (root-cause report D4).
    expect(screen.queryByText("您想重试哪个报告？")).toBeNull();
    expect(screen.queryByText("details")).toBeNull();
    // "Ask again" puts the question back into the composer without sending it.
    fireEvent.click(screen.getByText("ask_again"));
    expect(onComposerPrefill).toHaveBeenCalledWith("q");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("resolve 撞 404（孤儿卡）：收卡不复活，提示直接打字（审计 §5）", async () => {
    // The backend has no such row: persistence failed and the card used a local id. Retries always return 404, so do not
    // undo the optimistic marker and revive it like the generic error branch (that would require clicking N times).
    resolveMock.mockRejectedValue(
      new ApiError(404, "not found", { code: "clarification_not_found" }),
    );
    const onSend = vi.fn();
    render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "q",
          clarificationPending: PENDING,
        })}
        onSend={onSend}
      />,
    );
    await choose("报告A");
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "info", text: "unanswerable" }),
      ),
    );
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.queryByText("报告A")).toBeNull(); // The card collapses and does not revive.
    expect(screen.queryByTestId("error-diagnostics")).toBeNull();
  });

  it("未知 422：卡片带着原选择回来，错误写在卡内，原文不进 DOM，技术详情可展开", async () => {
    resolveMock.mockRejectedValue(
      new ApiError(
        422,
        "Traceback: password=raw-secret at /srv/private/app.py",
        {
          code: "private_validator_failure",
          requestId: "0123456789abcdef0123456789abcdef",
        },
      ),
    );
    render(
      <TurnContainer
        runner={runner({
          turnId: "t1",
          user: "q",
          clarificationPending: PENDING,
        })}
      />,
    );

    await choose("报告A");

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("submit_failed");
    });
    expect(document.body.textContent).not.toContain("raw-secret");
    // The card is back with the same choice selected and Retry as its primary action.
    expect(screen.getByText("报告A").closest('[role="radio"]')?.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("retry")).toBeTruthy();
    expect(screen.getByText("details")).toBeTruthy();
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("document pause still saving: the card stays locked with the reason and unlocks by itself", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      resolveMock.mockRejectedValue(
        new ApiError(409, "pause pending", { code: "clarification_pause_pending" }),
      );
      listMock
        .mockRejectedValueOnce(new ApiError(409, "pause pending", { code: "clarification_pause_pending" }))
        .mockResolvedValueOnce({ items: [] });
      render(
        <TurnContainer
          runner={runner({ turnId: "t_doc", user: "q", clarificationPending: PENDING })}
        />,
      );
      await choose("报告A");
      await waitFor(() => expect(screen.getByText("pause_pending")).toBeTruthy());
      // The choice is kept; options are locked while the pause settles.
      const option = screen.getByText("报告A").closest('[role="radio"]') as HTMLButtonElement;
      expect(option.getAttribute("aria-checked")).toBe("true");
      expect(option.disabled).toBe(true);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
      await waitFor(() => expect(screen.queryByText("pause_pending")).toBeNull());
      expect(listMock).toHaveBeenCalledWith("t_doc");
      expect((screen.getByText("报告A").closest('[role="radio"]') as HTMLButtonElement).disabled).toBe(false);
      expect(toastMock).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
