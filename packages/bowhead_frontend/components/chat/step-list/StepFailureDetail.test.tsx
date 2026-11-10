import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { AgentTrace } from "./AgentTrace";
import { readableFailureFacts } from "./StepFailureDetail";
import { StepRecoveryProvider } from "./step-recovery";
import { stepsToStepViews } from "./step-view";
import { TurnRunningProvider } from "./turn-running";
import type { StepView } from "./types";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  vi.restoreAllMocks();
});

const TURN = "t_failed_dispatch";
const OCCURRENCE = "20b91dd835a842319527e3f4768f3aca";

/** The 116 dogfood shape (`t_7eb6ab3b9876`): a refused dispatch whose diagnostic now names the argument. */
function rows({ code = "tool_arguments_invalid", safeDetail = "field=canvas_mode", outcome }: {
  code?: string;
  safeDetail?: string;
  outcome?: { issue_steps: number; recovered_steps: number; unresolved_steps: number };
} = {}) {
  const wire = [
    ["step_list_card_created", { plan: [] }],
    ["step_started", { step_id: "spawn_1", step_type: "subagent", tool_name: "subagent_spawn", header_zh: "Call", started_at: "2026-11-08T02:40:00Z", input_preview: "Recolor the chart" }],
    ["step_done", {
      step_id: "spawn_1", step_type: "subagent", tool_name: "subagent_spawn", state: "failure", duration_ms: 12, output_preview: "Failed: invalid input",
      error: {
        type: "invalid_args", code, message_zh: code, is_transient: false,
        diagnostics: { schema_version: 1, disclosure: "authenticated", diagnostic_code: "tool_failure", occurrence_id: OCCURRENCE, turn_id: TURN, safe_detail: safeDetail, retryable: false },
      },
    }],
    ["final_answer", { text: "The chart is green now." }],
    ["done", { status: "completed", ...(outcome ? { step_outcome: { schema_version: 1, answer_affected: false, ...outcome } } : {}) }],
  ] as const;
  return wire.map(([event_type, payload], index) => ({
    event_type, seq: index + 1, payload: { ...payload, card_id: TURN, turn_id: TURN, seq: index + 1 }, occurred_at: "",
  }));
}

function ingestLive(events: ReturnType<typeof rows>) {
  for (const event of events) dispatchToTurnStore(event.event_type, event.payload);
}

function renderTurn({ locale = "en-US" as const, running = false }: { locale?: "en-US" | "zh-CN"; running?: boolean } = {}) {
  const turn = getTurnSnapshot(TURN)!;
  return renderIntl(
    <TurnRunningProvider running={running}>
      <StepRecoveryProvider turnId={TURN}>
        <div className="bh-step-list">
          <AgentTrace steps={stepsToStepViews(turn.steps, turn.peerTerminalById)} peerNames={{}} peerProse={{}} />
        </div>
      </StepRecoveryProvider>
    </TurnRunningProvider>,
    { locale },
  );
}

function failedRow(): HTMLButtonElement {
  return screen.getByRole("button", { name: /Recolor the chart/ });
}

describe("failed trace row disclosure", () => {
  it("opens a failed dispatch as a native disclosure with its reason, argument and reference", () => {
    ingestLive(rows({ outcome: { issue_steps: 1, recovered_steps: 0, unresolved_steps: 1 } }));
    renderTurn();
    const row = failedRow();
    expect(row.tagName).toBe("BUTTON");
    expect(row).toHaveAttribute("type", "button");
    expect(row).toBeEnabled();
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("step-failure-detail")).toBeNull();

    row.focus();
    fireEvent.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");
    expect(row).toHaveFocus();
    const detail = screen.getByTestId("step-failure-detail");
    expect(detail.closest(".stp-detail")).toHaveAttribute("id", row.getAttribute("aria-controls")!);
    expect(detail.querySelector(".stp-fail__why")?.textContent).toBe(
      "The step's inputs were rejected. Input: canvas_mode. If anything is missing, ask a follow-up.",
    );
    expect(detail.querySelector(".stp-fail__ref")?.textContent).toContain(`Error reference ${OCCURRENCE}`);
    expect(screen.getByRole("button", { name: "Copy diagnostic information" })).toBeTruthy();
    // No codes, Tool names, Turn identity, raw preview or JSON.
    for (const internal of ["tool_arguments_invalid", "tool_failure", "invalid_args", "subagent_spawn", TURN, "Failed: invalid input", "field=", "{"]) {
      expect(detail.textContent).not.toContain(internal);
    }
    expect(screen.queryByText("View technical details")).toBeNull();

    fireEvent.click(row);
    expect(row).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("step-failure-detail")).toBeNull();
  });

  it("copies the diagnostic information for the reference", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    ingestLive(rows());
    renderTurn();
    fireEvent.click(failedRow());
    fireEvent.click(within(screen.getByTestId("step-failure-detail")).getByRole("button", { name: "Copy diagnostic information" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(writeText.mock.calls[0][0]).toContain(OCCURRENCE);
    expect(await screen.findByText("Diagnostic information copied")).toBeInTheDocument();
  });

  it("says no action is needed only after the persisted receipt records every failed step as recovered", () => {
    ingestLive(rows({ outcome: { issue_steps: 1, recovered_steps: 1, unresolved_steps: 0 } }));
    renderTurn();
    fireEvent.click(failedRow());
    expect(screen.getByTestId("step-failure-detail").querySelector(".stp-fail__action")?.textContent).toBe(
      " A retry succeeded; no action needed.",
    );
    cleanup();
    // While the Turn runs, the agent may still handle the failure: no user action yet.
    renderTurn({ running: true });
    fireEvent.click(failedRow());
    expect(screen.getByTestId("step-failure-detail").querySelector(".stp-fail__action")).toBeNull();
  });

  it("keeps the effect check for an unconfirmed outcome even after a later success", () => {
    ingestLive(rows({ code: "tool_execution_unknown", safeDetail: "", outcome: { issue_steps: 1, recovered_steps: 1, unresolved_steps: 0 } }));
    renderTurn();
    fireEvent.click(failedRow());
    expect(screen.getByTestId("step-failure-detail").querySelector(".stp-fail__why")?.textContent).toBe(
      "The step's result is unconfirmed. Check whether it took effect before repeating it.",
    );
  });

  it("uses the interface language without a space between Chinese sentences", () => {
    ingestLive(rows());
    renderTurn({ locale: "zh-CN" });
    fireEvent.click(failedRow());
    const detail = screen.getByTestId("step-failure-detail");
    expect(detail.querySelector(".stp-fail__why")?.textContent).toBe("这一步的参数不符合要求。参数：canvas_mode。如有缺漏，可以追问重试。");
    expect(detail.querySelector(".stp-fail__ref")?.textContent).toContain(`错误编号 ${OCCURRENCE}`);
  });

  it("shows the same detail after persisted replay as after live ingestion", () => {
    const events = rows({ outcome: { issue_steps: 1, recovered_steps: 1, unresolved_steps: 0 } });
    ingestLive(events);
    renderTurn();
    fireEvent.click(failedRow());
    const live = screen.getByTestId("step-failure-detail").outerHTML;
    const liveRow = failedRow().textContent;
    cleanup();
    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();

    replayTurnEventRows(TURN, events);
    renderTurn();
    expect(failedRow()).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(failedRow());
    expect(failedRow().textContent).toBe(liveRow);
    expect(screen.getByTestId("step-failure-detail").outerHTML).toBe(live);
    expect(live).toContain("A retry succeeded");
  });

  it("keeps rows without error information non-interactive", () => {
    const base = { parentStepId: null, concurrencyGroupId: null, stepType: "tool_run" as const, headerZh: "", toolName: "bi_query" as const };
    const steps: StepView[] = [
      { ...base, stepId: "done", state: "done", label: "Plain query" },
      { ...base, stepId: "bare", state: "failed", label: "Bare failure", error: null },
      { ...base, stepId: "failed", state: "failed", label: "Explained failure", error: { kind: "permanent", message: "", code: "tool_execution_timeout" } },
    ];
    renderIntl(<AgentTrace steps={steps} peerNames={{}} peerProse={{}} />, { locale: "en-US" });
    for (const name of [/Plain query/, /Bare failure/]) {
      const row = screen.getByRole("button", { name });
      expect(row).toBeDisabled();
      expect(row).not.toHaveAttribute("aria-expanded");
    }
    const explained = screen.getByRole("button", { name: /Explained failure/ });
    fireEvent.click(explained);
    expect(screen.getByTestId("step-failure-detail").textContent).toBe(
      "The step timed out. Check whether it took effect before repeating it.",
    );
  });
});

describe("readableFailureFacts", () => {
  it("names known published facts, at most two, and never an internal identifier", () => {
    expect(readableFailureFacts("field=sec_45a118c5ec32; phase=chat; failure_class=configuration; exit_code=1")).toEqual([
      { key: "exit_code", value: "1" },
    ]);
    // At most the argument and the most specific cause of a code failure.
    expect(readableFailureFacts("exit_code=1; exception=KeyError; missing_key=bm; field=columns")).toEqual([
      { key: "field", value: "columns" },
      { key: "missing_key", value: "bm" },
    ]);
    expect(readableFailureFacts("exception=ValueError; exit_code=1")).toEqual([{ key: "exception", value: "ValueError" }]);
    expect(readableFailureFacts(undefined)).toEqual([]);
    expect(readableFailureFacts("field=")).toEqual([]);
  });
});
