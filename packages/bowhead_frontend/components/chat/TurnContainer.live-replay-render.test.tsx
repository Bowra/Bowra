/**
 * Live rendering == replay rendering (chat-pipeline.md core invariant): compare **rendered output**, not only store.
 *
 * `lib/turn-store-goldenreplay.test.ts` pins equal terminal store states on both paths,
 * but equal stores do not guarantee equal user-visible output: identical narrative text uses
 * separate rendering paths during streaming and after completion. On 116 1.3.36 (turn t_2119144cd8b3, Lead dispatched a private subagent to list
 * facebook/react docs), streaming nested lists flattened into one paragraph:
 * 「1. React Repository Overview - 1.1 … - 1.2 … 2. Core …」, returning to lists only after persisted-content
 * rerender; persisted final_answer had 36 `\n` characters, so live rendering lost the newlines.
 *
 * Fixtures use real wire shape (as `GET /turns/{id}/events`), sharing one event sequence:
 *   · live: individual dispatchToTurnStore calls, stopping before final_answer (answer still streaming), running=true;
 *   · replay: replayTurnEventRows consumes the entire stream, running=false.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import type { TurnEventDTO } from "@/lib/api";
import type { TurnRunnerState } from "@/lib/types";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

import { TurnContainer } from "./TurnContainer";

const T = "t_live_replay_render";

/** Original answer: nested ordered/unordered lists + bold, structurally matching the 116 turn. */
const ANSWER =
  "facebook/react 的文档目录结构如下：\n\n" +
  "1. **React Repository Overview**\n" +
  "   - 1.1 Repository Structure and Packages\n" +
  "   - 1.2 Feature Flags System\n" +
  "2. **Core Reconciler Architecture**\n" +
  "   - 2.1 Fiber Data Structure\n" +
  "   - 2.2 Work Loop and Scheduling\n" +
  "3. **Glossary**\n";

/** Realistic streaming slices: newlines often form separate deltas, and cuts also occur mid-line. */
function chunk(text: string): string[] {
  const out: string[] = [];
  let buf = "";
  for (const ch of text) {
    if (ch === "\n") {
      if (buf) out.push(buf);
      out.push("\n");
      buf = "";
      continue;
    }
    buf += ch;
    if (buf.length >= 7) {
      out.push(buf);
      buf = "";
    }
  }
  if (buf) out.push(buf);
  return out;
}

type Row = TurnEventDTO;

function buildEvents(opts: { leadIntro?: string } = {}): { beforeFinal: Row[]; all: Row[] } {
  let seq = 0;
  const ev = (event_type: string, payload: Record<string, unknown>): Row => {
    seq += 1;
    return {
      seq,
      event_type,
      payload: { seq, ...payload },
      occurred_at: new Date(Date.UTC(2026, 8, 24, 2, 0, seq)).toISOString(),
    };
  };
  const head: Row[] = [
    ev("turn_started", { turn_id: T, session_id: "s_1", started_at: "2026-09-24T02:00:00Z" }),
    ev("step_list_card_created", { card_id: T }),
    // Default: Lead dispatches without speaking (as on 116); leadIntro pins pre-dispatch narration.
    ...(opts.leadIntro ? [ev("narrative.delta", { card_id: T, delta: opts.leadIntro })] : []),
    ev("step_started", {
      card_id: T, step_id: "sp1", step_type: "subagent_spawn", tool_name: "subagent_spawn",
      header_zh: "派发", started_at: "2026-09-24T02:00:01Z",
    }),
    ev("agent_spawned", {
      card_id: T, subagent_id: "peer_w", profile: "dogfood-wiki-helper",
      display_name: "Wiki 助手", task: "列出 facebook/react 的文档目录",
    }),
    ev("peer_dispatched", {
      card_id: T, peer_name: "dogfood-wiki-helper", subagent_id: "peer_w", spawn_id: "peer_w",
      call_id: "c-sp1", parent_step_id: "sp1",
    }),
    ev("step_started", {
      card_id: T, step_id: "st_w1", step_type: "tool_run", tool_name: "web_fetch",
      header_zh: "读取网页", subagent_id: "peer_w", started_at: "2026-09-24T02:00:02Z",
    }),
    ev("partial_answer", { card_id: T, parent_agent_id: "peer_w", delta: "已读到 **Glossary** 一节。" }),
    ev("step_done", { card_id: T, step_id: "st_w1", state: "done", duration_ms: 900 }),
    ev("lead.subagent_summary", { card_id: T, spawn_id: "peer_w", status: "succeeded", duration_ms: 1200 }),
    ev("step_done", { card_id: T, step_id: "sp1", state: "done", duration_ms: 1300 }),
  ];
  const deltas = chunk(ANSWER).map((d) => ev("narrative.delta", { card_id: T, delta: d }));
  const tail: Row[] = [
    ev("final_answer", { card_id: T, turn_id: T, text: ANSWER }),
    ev("step_list_card_finalized", { card_id: T, state: "completed", step_count: 2 }),
    ev("done", { turn_id: T, status: "completed", duration_ms: 42000 }),
  ];
  const beforeFinal = [...head, ...deltas];
  return { beforeFinal, all: [...beforeFinal, ...tail] };
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: "列出 facebook/react 的文档目录结构",
    intent: null,
    complexity: null,
    chips: [],
    answer: "",
    artifact: null,
    source: null,
    summary: null,
    traceSteps: [],
    turnId: T,
    sessionId: "s_1",
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
    ...over,
  };
}

/** User-visible answer structure: each body list item's text in document order. */
function answerListItems(container: HTMLElement): string[] {
  const items: string[] = [];
  container.querySelectorAll(".lead-narrative-md li").forEach((li) => {
    // Only this item's own first line (nested sublists count separately).
    const own = Array.from(li.childNodes)
      .filter((n) => !(n instanceof HTMLElement && (n.tagName === "UL" || n.tagName === "OL")))
      .map((n) => n.textContent ?? "")
      .join("")
      .trim();
    items.push(own);
  });
  return items;
}

function renderLive(opts: { leadIntro?: string } = {}): HTMLElement {
  const { beforeFinal } = buildEvents(opts);
  for (const row of beforeFinal) dispatchToTurnStore(row.event_type, row.payload);
  const { container } = render(<TurnContainer runner={runner({ running: true })} />);
  return container;
}

function renderReplay(opts: { leadIntro?: string } = {}): HTMLElement {
  const { all } = buildEvents(opts);
  replayTurnEventRows(T, all);
  const { container } = render(<TurnContainer runner={runner({ running: false, answer: ANSWER })} />);
  return container;
}

function resetAll(): void {
  cleanup();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
}

describe("同一组事件 · live 渲染与重放渲染一致", () => {
  beforeEach(resetAll);
  afterEach(resetAll);

  it("★ 流式中的答案保留列表结构（嵌套列表 / 加粗），与重放后一致", () => {
    // Mutation: revert LeadNarrative streaming to blank-line paragraph splitting, treating intra-paragraph \n as whitespace → no live <li>; this fails.
    const live = renderLive();
    const liveItems = answerListItems(live);
    const liveStrong = Array.from(live.querySelectorAll(".lead-narrative-md strong"))
      .map((n) => n.textContent);
    resetAll();
    const replay = renderReplay();
    const replayItems = answerListItems(replay);
    const replayStrong = Array.from(replay.querySelectorAll(".lead-narrative-md strong"))
      .map((n) => n.textContent);

    // Positive control: replay actually renders the complete nested list (otherwise two empty sides also compare equal).
    expect(replayItems).toEqual([
      "React Repository Overview",
      "1.1 Repository Structure and Packages",
      "1.2 Feature Flags System",
      "Core Reconciler Architecture",
      "2.1 Fiber Data Structure",
      "2.2 Work Loop and Scheduling",
      "Glossary",
    ]);
    expect(liveItems).toEqual(replayItems);
    expect(liveStrong).toEqual(replayStrong);
    // Visible strings must not show flattened-paragraph shape or expose Markdown markers.
    const liveAnswerText = Array.from(live.querySelectorAll(".lead-narrative-md"))
      .map((n) => n.textContent ?? "").join("\n");
    expect(liveAnswerText).not.toMatch(/Overview\s*-\s*1\.1/);
    expect(liveAnswerText).not.toContain("**");
  });

  it("★ 流式中的答案排在产出它的执行过程之后，与重放顺序一致；派活前的旁白仍在带上方", () => {
    // On 116, answer body rendered **above** the running band (step card), moving below execution only after submission.
    // Replay orders execution at the first step position → subsequent prose/answer; live must match.
    // Mutation: stop rendering post-step beats after band (leave all in BeatStream) → answer precedes band; this fails.
    const INTRO = "我请 Wiki 助手先读一下仓库文档。";
    const pos = (a: Element | null, b: Element | null) => {
      expect(a).toBeTruthy();
      expect(b).toBeTruthy();
      return a!.compareDocumentPosition(b!) & Node.DOCUMENT_POSITION_FOLLOWING;
    };
    const findText = (root: HTMLElement, needle: string) =>
      Array.from(root.querySelectorAll(".lead-narrative-md"))
        .find((n) => (n.textContent ?? "").includes(needle)) ?? null;

    const live = renderLive({ leadIntro: INTRO });
    const band = live.querySelector('[data-testid="running-band"]');
    // Pre-dispatch narration stays above band (design 1e: prose above band).
    expect(pos(findText(live, INTRO), band)).toBeTruthy();
    // Post-dispatch answer stays below band.
    expect(pos(band, findText(live, "Glossary"))).toBeTruthy();

    resetAll();
    const replay = renderReplay({ leadIntro: INTRO });
    const trace = replay.querySelector(".bh-step-list");
    // Positive control: replay also puts execution before answer.
    expect(pos(trace, findText(replay, "Glossary"))).toBeTruthy();
    expect(pos(findText(replay, INTRO), trace)).toBeTruthy();
  });
});
