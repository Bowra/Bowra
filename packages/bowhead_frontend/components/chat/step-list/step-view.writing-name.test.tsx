/**
 * Row names for「准备中」(`lead_thinking_writing`).
 *
 * Backend writing steps send the tool that will be called as `tool_name`; a direct catalog lookup
 * shows「运行代码」while Python code is still being written, before anything runs. Rules (`tool-catalog.ts::writingDisplayKey`):
 *   · python_sandbox →「正在写代码」, bi_query →「正在写查询」(do not repeat「准备中」on the right)
 *   · kb_search / web_search → actual tool name +「准备中…」on the right
 *   · Restore actual tool names (「运行代码」「查询数据」…) when execution takes over.
 *
 * Every frame goes through both live (`dispatchToTurnStore`) and replay (`replayTurnEventRows`, F5 / session switch);
 * the rendered row text must match exactly.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { _resetAllTurnsForTesting, getTurnSnapshot } from "@/lib/turn-store";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import type { TurnEventDTO } from "@/lib/api";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { AgentTrace } from "./AgentTrace";
import { humanLabel } from "./running-band-model";
import { stepsToStepViews } from "./step-view";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  document.documentElement.lang = "";
});

type Ev = [string, Record<string, unknown>];

const WRITING_HEADERS: Record<string, string> = {
  python_sandbox: "写代码 (Python)",
  bi_query: "写查询 (SQL)",
  kb_search: "查知识库 (KB)",
  web_search: "搜网络 (Web)",
};

/** Complete event sequence for one Lead call, shaped after `subagent.py`'s writing branch and `_execute_tool`. */
function callEvents(turnId: string, tool: string): Ev[] {
  let seq = 0;
  const e = (name: string, p: Record<string, unknown>): Ev => [name, { card_id: turnId, evt_seq: ++seq, ...p }];
  return [
    e("step_list_card_created", {}),
    e("step_started", {
      step_id: "writing_c1",
      step_type: "lead_thinking_writing",
      header_zh: WRITING_HEADERS[tool],
      tool_name: tool,
      call_id: "c1",
    }),
    e("step_progress", { step_id: "writing_c1", content_delta: '{"code": "print(1)"}' }),
    e("step_done", {
      step_id: "writing_c1",
      step_type: "lead_thinking_writing",
      state: "success",
      duration_ms: 0,
      tool_name: tool,
      call_id: "c1",
    }),
    e("step_started", {
      step_id: "step_c1",
      step_type: "tool_run",
      header_zh: "执行",
      tool_name: tool,
      call_id: "c1",
      ...(tool === "python_sandbox"
        ? { code_input: { language: "python", code: "print(1)", truncated: false } }
        : {}),
    }),
    e("step_done", { step_id: "step_c1", state: "success", duration_ms: 300, tool_name: tool, call_id: "c1" }),
  ];
}

function toRows(events: Ev[]): TurnEventDTO[] {
  return events.map(([name, payload], i) => ({
    seq: i + 1,
    event_type: name,
    payload,
    occurred_at: "2026-09-25T08:00:00.000Z",
  }));
}

function viewsOf(turnId: string) {
  const st = getTurnSnapshot(turnId);
  return st ? stepsToStepViews(st.steps, st.peerTerminalById) : [];
}

/** Render one frame: return the sole row's name and right-side summary; live and replay must match exactly. */
function frame(tool: string, upto: number, tag: string): { name: string; sum: string | null } {
  const events = callEvents(`t-live-${tag}`, tool).slice(0, upto);
  for (const [name, payload] of events) dispatchToTurnStore(name, payload);
  replayTurnEventRows(`t-replay-${tag}`, toRows(callEvents(`t-replay-${tag}`, tool).slice(0, upto)));

  const read = (turnId: string) => {
    const v = viewsOf(turnId);
    const { container, unmount } = render(<AgentTrace steps={v} peerNames={{}} peerProse={{}} />);
    const rows = container.querySelectorAll(".stp");
    expect(rows).toHaveLength(1);
    // The tool glyph renders as an icon whose accessible label is the catalog character.
    const tool = rows[0]!.querySelector(".stp-tool");
    const glyph = tool?.querySelector("svg.bh-glyph")?.getAttribute("aria-label");
    const text = tool?.textContent?.trim() ?? "";
    const out = {
      name: glyph ? `${glyph} ${text}` : text,
      sum: rows[0]!.querySelector(".stp-sum")?.textContent ?? null,
    };
    unmount();
    return out;
  };
  const live = read(`t-live-${tag}`);
  expect(read(`t-replay-${tag}`)).toEqual(live);
  return live;
}

// Frames: 3 = writing in progress (argument tokens received); 5 = execution starts; 6 = execution completes.
describe("写代码 / 写查询阶段的行名（live 与重放一致）", () => {
  it("python_sandbox：写代码时「正在写代码」，执行时恢复「运行代码」", () => {
    expect(frame("python_sandbox", 3, "py-w")).toEqual({ name: "✏ 正在写代码", sum: null });
    expect(frame("python_sandbox", 5, "py-r")).toEqual({ name: "🐍 运行代码", sum: "running" });
    expect(frame("python_sandbox", 6, "py-d").name).toBe("🐍 运行代码");
  });

  it("bi_query：写 SQL 时「正在写查询」，执行时恢复「查询数据」", () => {
    expect(frame("bi_query", 3, "bi-w")).toEqual({ name: "✏ 正在写查询", sum: null });
    expect(frame("bi_query", 5, "bi-r").name).toBe("🔍 查询数据");
    expect(frame("bi_query", 6, "bi-d").name).toBe("🔍 查询数据");
  });

  it("kb_search / web_search：保持工具本名，右侧「准备中」", () => {
    expect(frame("kb_search", 3, "kb-w")).toEqual({ name: "📚 搜索知识库", sum: "preparing" });
    expect(frame("kb_search", 5, "kb-r")).toEqual({ name: "📚 搜索知识库", sum: "running" });
    expect(frame("web_search", 3, "web-w")).toEqual({ name: "🌐 网络搜索", sum: "preparing" });
  });

  it("英文界面：Writing code → Running code；Writing query → Query Data", () => {
    document.documentElement.lang = "en-US";
    expect(frame("python_sandbox", 3, "en-py-w")).toEqual({ name: "✏ Writing code", sum: null });
    expect(frame("python_sandbox", 5, "en-py-r").name).toBe("🐍 Running code");
    expect(frame("bi_query", 3, "en-bi-w").name).toBe("✏ Writing query");
    expect(frame("bi_query", 6, "en-bi-d").name).toBe("🔍 Query Data");
    expect(frame("kb_search", 3, "en-kb-w").name).toBe("📚 Search Knowledge Base");
  });
});

describe("运行带当前行与步骤行同一口径", () => {
  it("写代码阶段英文不说 Run code；执行后回到工具名", () => {
    document.documentElement.lang = "en-US";
    for (const [name, payload] of callEvents("t-band", "python_sandbox").slice(0, 3)) {
      dispatchToTurnStore(name, payload);
    }
    expect(humanLabel(viewsOf("t-band")[0]!, false)).toBe("Write code");
    for (const [name, payload] of callEvents("t-band", "python_sandbox").slice(3, 5)) {
      dispatchToTurnStore(name, payload);
    }
    expect(humanLabel(viewsOf("t-band")[0]!, false)).toBe("Run Code");
  });
});
