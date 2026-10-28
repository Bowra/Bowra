/**
 * Merge one tool call's「准备中」(`writing_<call_id>`) and execution (`step_<call_id>`) steps into one row.
 *
 * A replayed kb_search call can have two step_id values,
 * rendering「搜索知识库 0ms」+「搜索知识库 · 找到 5 条 367ms」as two rows.
 * Event shapes copy `subagent.py`'s Lead writing branch and `_execute_tool`:
 * writing started → progress (argument tokens) → writing done(duration_ms:0) → step started → step done.
 *
 * Feed live events individually through `dispatchToTurnStore` (same reducer as use-turn-runner),
 * and replay through `replayTurnEventRows` (F5 / session switch); both projections must be identical.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import type { TurnEventDTO } from "@/lib/api";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { TurnContainer } from "../TurnContainer";
import { AgentTrace } from "./AgentTrace";
import { stepsToStepViews } from "./step-view";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

type Ev = [string, Record<string, unknown>];

const CALL = "3f9e8b879a03442db6f0fa04";

/** Event sequence for the kb_search turn (116 replay shape). */
function kbSearchEvents(turnId: string): Ev[] {
  let seq = 0;
  const e = (name: string, payload: Record<string, unknown>): Ev => [
    name,
    { card_id: turnId, evt_seq: ++seq, ...payload },
  ];
  return [
    e("step_list_card_created", {}),
    e("step_started", {
      step_id: `writing_${CALL}`,
      step_type: "lead_thinking_writing",
      header_zh: "查知识库 (KB)",
      started_at: "2026-09-25T08:00:00.000Z",
      tool_name: "kb_search",
      call_id: CALL,
    }),
    e("step_progress", { step_id: `writing_${CALL}`, content_delta: '{"query": "退货' }),
    e("step_progress", { step_id: `writing_${CALL}`, content_delta: '政策"}' }),
    e("step_done", {
      step_id: `writing_${CALL}`,
      step_type: "lead_thinking_writing",
      state: "success",
      duration_ms: 0,
      tool_name: "kb_search",
      call_id: CALL,
      ok: true,
    }),
    e("step_started", {
      step_id: `step_${CALL}`,
      step_type: "tool_run",
      header_zh: "搜索知识库",
      started_at: "2026-09-25T08:00:01.000Z",
      input_preview: "退货政策",
      tool_name: "kb_search",
      call_id: CALL,
      subagent_id: "sub_lead",
    }),
    e("step_done", {
      step_id: `step_${CALL}`,
      step_type: "tool_run",
      state: "success",
      duration_ms: 367,
      output_preview: "找到 5 条",
      tool_name: "kb_search",
      call_id: CALL,
    }),
  ];
}

function feedLive(events: Ev[]): void {
  for (const [name, payload] of events) dispatchToTurnStore(name, payload);
}

function toRows(events: Ev[]): TurnEventDTO[] {
  return events.map(([name, payload], i) => ({
    seq: i + 1,
    event_type: name,
    payload,
    occurred_at: "2026-09-25T08:00:00.000Z",
  }));
}

function views(turnId: string) {
  const st = getTurnSnapshot(turnId);
  return st ? stepsToStepViews(st.steps, st.peerTerminalById) : [];
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

function archivedTurn(turnId: string): ArchivedTurn {
  return {
    turnId,
    terminalStatus: "completed",
    user: "退货政策是什么",
    userAttachments: [],
    answer: "结论。",
    traceSteps: [],
    peersDispatched: [],
    errorReason: null,
    archivedAt: Date.now(),
  } as ArchivedTurn;
}

describe("「准备中」与执行步骤合并为一行", () => {
  it("★ kb_search：live 与重放都只有一行，状态、耗时、摘要取执行步骤，且两边完全相等", () => {
    feedLive(kbSearchEvents("t-live"));
    const live = views("t-live");

    replayTurnEventRows("t-replay", toRows(kbSearchEvents("t-replay")));
    const replay = views("t-replay");

    expect(live).toHaveLength(1);
    const [row] = live;
    expect(row).toMatchObject({
      stepId: `writing_${CALL}`,
      stepType: "tool_run",
      toolName: "kb_search",
      state: "done",
      durationText: "367ms",
      summary: "找到 5 条",
      label: "退货政策",
      isStreaming: false,
      // Position uses writing's first appearance (evt_seq=2), not execution step's 6.
      evtSeq: 2,
    });
    expect(replay).toEqual(live);
  });

  it("逐帧：只有 writing 时是「准备中」并实时展示参数；执行开始后同一行接替，不再单列", () => {
    const events = kbSearchEvents("t-frames");
    // Through the second progress event: writing only.
    feedLive(events.slice(0, 4));
    let v = views("t-frames");
    expect(v).toHaveLength(1);
    expect(v[0]).toMatchObject({
      stepId: `writing_${CALL}`,
      stepType: "lead_thinking_writing",
      state: "active",
      isStreaming: true,
      partialContent: '{"query": "退货政策"}',
    });

    // Writing closed (duration_ms:0), but execution has not arrived: 0ms is a placeholder, never display it.
    feedLive(events.slice(4, 5));
    v = views("t-frames");
    expect(v).toHaveLength(1);
    expect(v[0]!.durationText).toBeUndefined();

    // Execution starts: same single row/key, state changes to executing.
    feedLive(events.slice(5, 6));
    v = views("t-frames");
    expect(v).toHaveLength(1);
    expect(v[0]).toMatchObject({
      stepId: `writing_${CALL}`,
      stepType: "tool_run",
      state: "active",
      isStreaming: false,
    });
    expect(v[0]!.durationText).toBeUndefined();
  });

  it("并行两次调用：两行、按 writing 出现顺序，各自归位", () => {
    const turnId = "t-par";
    let seq = 0;
    const e = (name: string, p: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...p }];
    const w = (id: string) =>
      e("step_started", { step_id: `writing_${id}`, step_type: "lead_thinking_writing", header_zh: "写查询 (SQL)", tool_name: "bi_query", call_id: id });
    const wd = (id: string) =>
      e("step_done", { step_id: `writing_${id}`, step_type: "lead_thinking_writing", state: "success", duration_ms: 0, tool_name: "bi_query", call_id: id });
    const s = (id: string) =>
      e("step_started", { step_id: `step_${id}`, step_type: "tool_run", header_zh: "查询数据", input_preview: id, tool_name: "bi_query", call_id: id });
    const sd = (id: string, ms: number) =>
      e("step_done", { step_id: `step_${id}`, state: "success", duration_ms: ms, output_preview: `${id} 行`, tool_name: "bi_query", call_id: id });
    feedLive([e("step_list_card_created", {}), w("A"), w("B"), wd("A"), s("A"), wd("B"), s("B"), sd("B", 900), sd("A", 1500)]);

    const v = views(turnId);
    expect(v.map((x) => [x.stepId, x.label, x.durationText])).toEqual([
      ["writing_A", "A", "1.5s"],
      ["writing_B", "B", "900ms"],
    ]);
  });

  it("没有 writing 的执行步骤原样保留（非流式模型 / peer 的调用）", () => {
    const turnId = "t-plain";
    dispatchToTurnStore("step_list_card_created", { card_id: turnId });
    dispatchToTurnStore("step_started", { card_id: turnId, step_id: "step_x", step_type: "tool_run", header_zh: "搜索知识库", tool_name: "kb_search", call_id: "x" });
    dispatchToTurnStore("step_done", { card_id: turnId, step_id: "step_x", state: "success", duration_ms: 120, tool_name: "kb_search" });
    expect(views(turnId).map((x) => [x.stepId, x.durationText])).toEqual([["step_x", "120ms"]]);
  });

  it("★ 渲染面：live 当前轮与归档轮都只渲一行步骤，右侧耗时是 367ms 不是 0ms", () => {
    feedLive(kbSearchEvents("t-ui"));
    const live = render(<TurnContainer runner={runner({ turnId: "t-ui", user: "退货政策是什么", running: false })} />);
    const liveRows = live.container.querySelectorAll(".stp");
    expect(liveRows).toHaveLength(1);
    expect(liveRows[0]!.querySelector(".stp-time")?.textContent).toBe("367ms");
    const liveText = liveRows[0]!.textContent;
    cleanup();

    const { container } = render(
      <TurnContainer runner={runner({ turnId: null, archivedTurns: [archivedTurn("t-ui")] } as Partial<TurnRunnerState>)} />,
    );
    const summary = container.querySelector('[data-testid="step-summary"]');
    if (summary) fireEvent.click(summary as HTMLElement);
    const archivedRows = container.querySelectorAll(".stp");
    expect(archivedRows).toHaveLength(1);
    expect(archivedRows[0]!.textContent).toBe(liveText);
  });
});

describe("python_sandbox 写代码体验不回退", () => {
  function pyEvents(turnId: string): Ev[] {
    let seq = 0;
    const e = (name: string, p: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...p }];
    return [
      e("step_list_card_created", {}),
      e("step_started", { step_id: "writing_py1", step_type: "lead_thinking_writing", header_zh: "写代码 (Python)", tool_name: "python_sandbox", call_id: "py1" }),
      e("step_progress", { step_id: "writing_py1", content_delta: '{"code": "import pandas as pd\\nprint(' }),
      e("step_progress", { step_id: "writing_py1", content_delta: '42)", "inputs": {"data": "art_1"}}' }),
      e("step_done", { step_id: "writing_py1", step_type: "lead_thinking_writing", state: "success", duration_ms: 0, tool_name: "python_sandbox", call_id: "py1" }),
      e("step_started", {
        step_id: "step_py1",
        step_type: "tool_run",
        header_zh: "运行代码",
        tool_name: "python_sandbox",
        call_id: "py1",
        code_input: { language: "python", code: "import pandas as pd\nprint(42)", truncated: false },
      }),
      e("step_done", {
        step_id: "step_py1",
        state: "success",
        duration_ms: 2100,
        tool_name: "python_sandbox",
        result_inline: { ok: true, exit_code: 0, stdout: "42\n", stderr: "" },
      }),
    ];
  }

  it("写代码时：一行、行名「正在写代码」、展开可见实时代码（去掉 JSON 外壳）", () => {
    feedLive(pyEvents("t-py").slice(0, 4));
    const v = views("t-py");
    expect(v).toHaveLength(1);
    const { container } = render(<AgentTrace steps={v} peerNames={{}} peerProse={{}} />);
    const rows = container.querySelectorAll(".stp");
    expect(rows).toHaveLength(1);
    // Name already says「正在写代码」; do not repeat「准备中」on the right (see step-view.writing-name.test.tsx).
    expect(rows[0]!.querySelector(".stp-tool")?.textContent).toContain("正在写代码");
    expect(rows[0]!.querySelector(".stp-sum")).toBeNull();
    expect(rows[0]!.querySelector(".stp-time")).toBeNull();
    const head = rows[0]!.querySelector(".stp-head") as HTMLElement;
    if (!rows[0]!.querySelector(".stp-detail")) fireEvent.click(head);
    const body = rows[0]!.querySelector(".stp-detail")?.textContent ?? "";
    expect(body).toContain("print(42)");
    expect(body).not.toContain('"inputs"');
  });

  it("执行完：一行，结果是 stdout，耗时 2.1s，codeInput 仍在（Results > Code 可看代码）", () => {
    feedLive(pyEvents("t-py2"));
    replayTurnEventRows("t-py2r", toRows(pyEvents("t-py2r")));
    const live = views("t-py2");
    expect(live).toHaveLength(1);
    expect(live[0]).toMatchObject({
      stepType: "tool_run",
      toolName: "python_sandbox",
      state: "done",
      durationText: "2.1s",
      detail: { kind: "stdout", text: "42" },
    });
    expect(views("t-py2r")).toEqual(live);
    // Execution-step codeInput remains in the store, not swallowed by merging.
    expect(getTurnSnapshot("t-py2")!.steps.find((s) => s.stepId === "step_py1")!.codeInput?.code).toBe(
      "import pandas as pd\nprint(42)",
    );
  });
});
