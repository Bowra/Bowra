import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { _resetAllTurnsForTesting } from "./turn-store";
import { dispatchToTurnStore } from "./turn-store-dispatch";
import { replayTurnEventRows } from "./turn-store-replay";
import { projectCodeRuns } from "./code-result-model";
import { useSessionPythonSteps } from "./use-running-python";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

describe("useSessionPythonSteps", () => {
  it("keeps completed historical runs and updates the current active run", () => {
    const { result } = renderHook(() => useSessionPythonSteps(["turn_old", "turn_current"]));

    act(() => {
      dispatchToTurnStore("step_started", {
        card_id: "turn_old",
        step_id: "step_old",
        step_type: "tool_run",
        header_zh: "运行代码",
        started_at: "2026-08-08T00:00:00Z",
        tool_name: "python_sandbox",
        call_id: "call_old",
        code_input: {
          language: "python",
          code: "print('old')",
          truncated: false,
        },
      });
      dispatchToTurnStore("step_done", {
        card_id: "turn_old",
        step_id: "step_old",
        step_type: "tool_run",
        state: "success",
        duration_ms: 12,
        tool_name: "python_sandbox",
        result_inline: { stdout: "old\n", exit_code: 0 },
        result_meta: {
          output_artifacts: [{ artifact_id: "art_old", filename: "old.csv" }],
        },
      });
      dispatchToTurnStore("step_started", {
        card_id: "turn_current",
        step_id: "step_current",
        step_type: "tool_run",
        header_zh: "运行代码",
        started_at: "2026-08-08T00:01:00Z",
        tool_name: "python_sandbox",
        code_input: {
          language: "python",
          code: "print('current')",
          truncated: false,
        },
      });
    });

    expect(result.current).toHaveLength(2);
    expect(result.current[0]).toMatchObject({
      turnId: "turn_old",
      state: "done",
      codeInput: { code: "print('old')" },
      stdout: "old",
      outputs: [{ artifactId: "art_old", filename: "old.csv" }],
    });
    expect(result.current[1]).toMatchObject({
      turnId: "turn_current",
      state: "active",
      codeInput: { code: "print('current')" },
    });
  });
});

/**
 * While Lead writes code, backend first emits `writing_<call_id>` (`tool_name=python_sandbox`, no code_input),
 * then closes it at execution and opens `step_<call_id>` (with code_input). Results > Code should show just one code-bearing entry after
 * execution starts; writing must not flash a running entry without code.
 */
describe("useSessionPythonSteps — 写代码阶段不列条目", () => {
  type Ev = [string, Record<string, unknown>];
  function pyEvents(turnId: string): Ev[] {
    let seq = 0;
    const e = (name: string, p: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...p }];
    return [
      e("step_list_card_created", {}),
      e("step_started", { step_id: "writing_py1", step_type: "lead_thinking_writing", header_zh: "写代码 (Python)", tool_name: "python_sandbox", call_id: "py1" }),
      e("step_progress", { step_id: "writing_py1", content_delta: '{"code": "print(42)"}' }),
      e("step_done", { step_id: "writing_py1", step_type: "lead_thinking_writing", state: "success", duration_ms: 0, tool_name: "python_sandbox", call_id: "py1" }),
      e("step_started", {
        step_id: "step_py1",
        step_type: "tool_run",
        header_zh: "运行代码",
        tool_name: "python_sandbox",
        call_id: "py1",
        code_input: { language: "python", code: "print(42)", truncated: false },
      }),
      e("step_done", {
        step_id: "step_py1",
        state: "success",
        duration_ms: 2100,
        tool_name: "python_sandbox",
        call_id: "py1",
        result_inline: { ok: true, exit_code: 0, stdout: "42\n", stderr: "" },
      }),
    ];
  }

  function codeRuns(steps: Parameters<typeof projectCodeRuns>[0]["executionSteps"]) {
    return projectCodeRuns({ sections: [], executionSteps: steps })
      .map((r) => ({ status: r.status, code: r.code, stdout: r.stdout }));
  }

  it.each([
    // 3 = writing underway; 4 = writing closed, execution not yet received; 5 = executing; 6 = execution complete.
    [3, [], 0],
    [4, [], 0],
    [5, [{ status: "running", code: "print(42)", stdout: null }], 1],
    [6, [{ status: "success", code: "print(42)", stdout: "42" }], 0],
  ])("逐帧（前 %i 条事件）：live 与重放的代码条目相同", (upto, expected, runningCount) => {
    const { result } = renderHook(() => useSessionPythonSteps(["t_live", "t_replay"]));
    act(() => {
      for (const [name, payload] of pyEvents("t_live").slice(0, upto)) dispatchToTurnStore(name, payload);
      replayTurnEventRows("t_replay", pyEvents("t_replay").slice(0, upto).map(([name, payload], i) => ({
        seq: i + 1,
        event_type: name,
        payload,
        occurred_at: "2026-09-25T08:00:00.000Z",
      })));
    });
    const live = result.current.filter((s) => s.turnId === "t_live");
    const replay = result.current.filter((s) => s.turnId === "t_replay");
    expect(codeRuns(live)).toEqual(expected);
    expect(codeRuns(replay)).toEqual(expected);
    expect(live.map((s) => s.stepId)).toEqual(replay.map((s) => s.stepId));
    // chat-workbench runningPySteps (Code Running notice) reads these active items:
    // it must not light up while code is being written.
    expect(live.filter((s) => s.state === "active")).toHaveLength(runningCount);
  });
});
