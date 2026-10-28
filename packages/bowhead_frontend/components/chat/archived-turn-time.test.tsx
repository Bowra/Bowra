/**
 * chat-ux-2026-08 batch F — archived-turn byline time.
 *
 * Existing defect: `ArchivedTurnView` **hardcodes `turnTimeText` to `""`**; its comment says
 * “historical turns don't have a captured turn-start ts”. That statement is outdated
 * —`ArchivedTurn.archivedAt` is now required.
 *
 * This breaks live=replay: the same turn's live byline shows 14:32, but after F5 the time simply
 * vanishes. CLAUDE.md's chat invariant requires live, persisted and replayed data to match.
 *
 * Both write paths use the same meaning, so using it is safe:
 *   live    → `archiveCompletedTurnInPlace` uses `Date.now()` when the turn **completes**
 *   hydrate → `Date.parse(turn.ended_at ?? turn.started_at)` (page.tsx)
 *
 * Mutation check: change `formatTurnTime(archived.archivedAt)` back to `""` → the first test must fail.
 *
 * ⚠ `archivedAt` is the **end** time, suitable only for BotByline. User bubbles also used it before ⇒ observed on 116:
 * “sent at 07:46 → displayed 你 · 07:53 after completion”. User bubbles show submission time: first turn_started
 * (turn-store, same source live and after refresh), then `userSentAt`, falling back to `archivedAt` only if both are absent.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, applyTurnStarted } from "@/lib/turn-store";
import { _clearAllToasts } from "@/lib/use-toast";
import type { TurnRunnerState, ArchivedTurn } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _clearAllToasts();
});
afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  _clearAllToasts();
});

/** Local time 2024-01-02 14:32 — assertions check only hour:minute, unaffected by timezone offsets. */
const AT = new Date(2024, 0, 2, 14, 32, 0).getTime();

function hhmm(ms: number): string {
  return new Date(ms).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function mount(archivedAt: number, userSentAt?: number) {
  return renderIntl(
    <TurnContainer
      runner={
        {
          user: null, intent: null, complexity: null, chips: [], answer: "",
          artifact: null, source: null, summary: null, traceSteps: [],
          turnId: null, sessionId: "s_1", sessionTitle: null,
          running: false, started: true, messages: [], userAttachments: [],
          thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "",
          clarifications: [], createToolOpen: null, subagentCreatorOpen: null,
          solidifyOpen: null, plan: null,
          archivedTurns: [
            {
              turnId: "t_hist",
              terminalStatus: "completed",
              user: "近30天 Top5",
              userAttachments: [],
              answer: "结论是 9.1 亿",
              traceSteps: [],
              peersDispatched: [],
              errorReason: null,
              archivedAt,
              ...(userSentAt === undefined ? {} : { userSentAt }),
            },
          ] as ArchivedTurn[],
        } as unknown as TurnRunnerState
      }
    />,
  );
}

describe("归档轮 byline 时间 — live/重放一致", () => {
  it("★ 显示 archivedAt 的时:分（旧实现硬编码空串，F5 后时间凭空消失）", () => {
    const { container } = mount(AT);
    const expected = new Date(AT).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
    expect(container.textContent).toContain(expected);
  });

  it("legacy 归档轮（archivedAt 解析不出 → NaN）退化回不显示，不渲 Invalid Date", () => {
    const { container } = mount(Number.NaN);
    expect(container.textContent).not.toContain("Invalid");
    expect(container.textContent).not.toContain("NaN");
    // But the turn itself is still present.
    expect(screen.getByText(/近30天 Top5/)).toBeTruthy();
  });
});

describe("用户气泡时间 = 提交时间，不随 turn 结束改写", () => {
  const SENT = new Date(2024, 0, 2, 7, 46, 0).getTime();
  const ENDED = new Date(2024, 0, 2, 7, 53, 0).getTime();

  it("★ 用户气泡显示 userSentAt（07:46），bot byline 仍是结束时间（07:53）", () => {
    const { container } = mount(ENDED, SENT);
    const user = screen.getByTestId("user-msg").textContent ?? "";
    expect(user).toContain(hhmm(SENT));
    expect(user).not.toContain(hhmm(ENDED));
    expect(container.textContent).toContain(hhmm(ENDED));
  });

  it("turn-store 的首条 turn_started 优先（与 live/重放同源，同轮续跑不覆写）", () => {
    const FIRST = new Date(2024, 0, 2, 7, 45, 0);
    const RESUMED = new Date(2024, 0, 2, 7, 50, 0);
    applyTurnStarted({ turnId: "t_hist", startedAt: FIRST.toISOString() });
    applyTurnStarted({ turnId: "t_hist", startedAt: RESUMED.toISOString() });
    mount(ENDED, SENT);
    const user = screen.getByTestId("user-msg").textContent ?? "";
    expect(user).toContain(hhmm(FIRST.getTime()));
    expect(user).not.toContain(hhmm(RESUMED.getTime()));
  });

  it("老归档快照没有 userSentAt 时退回 archivedAt（不留空）", () => {
    mount(ENDED);
    expect(screen.getByTestId("user-msg").textContent ?? "").toContain(hhmm(ENDED));
  });
});
