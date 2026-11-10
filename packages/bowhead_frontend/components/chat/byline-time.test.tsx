/**
 * Chat byline times (dogfood N3, 116 1.7.18): one meaning per byline, from server timestamps, identical while the
 * turn is live, after it is archived (the next message is sent) and after a reload.
 *
 * Observed before: a reply read 18:48 live (its start, from `runner.startedAt`) and 18:50 the moment the next message
 * was sent (`archivedAt: Date.now()`); another read 18:47 live and 18:41 after reload; a clarification-resumed reply
 * read its start 18:48 after reload instead of its end 18:49; the user bubble showed the browser's 18:50, then the
 * server's 18:51 (a 49.5 s slow Mac).
 *
 * Reply byline = the root `done.ended_at` (persisted `turns.ended_at` as the hydration fallback), nothing while running.
 * User byline = the first `turn_started.started_at` (`turns.started_at` as the hydration fallback), nothing before it.
 */
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";
import type { TurnEventDTO } from "@/lib/api";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { replayTurnEventRows } from "@/lib/turn-store-replay";
import { _clearAllToasts } from "@/lib/use-toast";
import { snapshotArchivedTurn } from "@/lib/use-turn-runner";
import type { ArchivedTurn, TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

beforeEach(() => {
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  _clearAllToasts();
  // Only Date is faked: "now" stays on the turns' calendar day, so bylines read as a time of day.
  vi.useFakeTimers({ toFake: ["Date"] });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  document.documentElement.lang = "";
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  _clearAllToasts();
});

/** zh-CN byline text for a server instant, computed independently of the code under test: 24-hour "HH:MM". */
function zhClock(iso: string): string {
  const date = new Date(iso);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** Render inside the intl provider; unlike `renderIntl`, `rerender` keeps the provider. */
function renderIntl(ui: ReactElement, { locale = "zh-CN" }: { locale?: "zh-CN" | "en-US" } = {}) {
  const messages = locale === "en-US" ? enMessages : zhMessages;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <NextIntlClientProvider locale={locale} messages={messages}>{children}</NextIntlClientProvider>
  );
  return render(ui, { wrapper });
}

function runner(over: Partial<TurnRunnerState> = {}): TurnRunnerState {
  return {
    user: null, intent: null, complexity: null, chips: [], answer: "", artifact: null, source: null, summary: null,
    traceSteps: [], turnId: null, sessionId: "s_1", sessionTitle: null, running: false, started: true, messages: [],
    userAttachments: [], thinking: "", thinkingLength: 0, lastThinkingHintAttachedAt: "", clarifications: [],
    createToolOpen: null, subagentCreatorOpen: null, solidifyOpen: null, plan: null,
    ...over,
  };
}

function botTime(container: HTMLElement, index = 0): string {
  return container.querySelectorAll('[data-testid="bot-byline"]')[index]?.textContent ?? "";
}

function userTime(index = 0): string {
  return screen.getAllByTestId("user-msg")[index]?.textContent ?? "";
}

let rowSeq = 0;
function row(event_type: string, payload: Record<string, unknown>): TurnEventDTO {
  rowSeq += 1;
  return { seq: rowSeq, event_type, payload: { seq: rowSeq, ...payload }, occurred_at: "2026-11-08T00:00:00Z" };
}

function live(event: TurnEventDTO, turnId: string): void {
  dispatchToTurnStore(event.event_type, { ...event.payload }, "live", turnId);
}

describe("reply byline: the server end time, identical live, archived and reloaded", () => {
  const T = "t_n3_failed";
  // The tester's evening (UTC-8): started 18:40, ended 18:41:53, next message sent 18:47.
  const STARTED = "2026-11-09T02:40:10Z";
  const ENDED = "2026-11-09T02:41:53Z";
  const NEXT_SENT = "2026-11-09T02:47:30Z";

  it("shows nothing while running, the done time once ended, and keeps it when the next message archives the turn", () => {
    rowSeq = 0;
    const events = [
      row("turn_started", { turn_id: T, session_id: "s_1", started_at: STARTED }),
      row("done", { turn_id: T, status: "failed", reason_code: "lead_internal_error", ended_at: ENDED }),
    ];
    vi.setSystemTime(new Date("2026-11-09T02:40:09Z"));
    const localSendTime = Date.now();
    live(events[0], T);
    const running = runner({ turnId: T, user: "Top 5 regions", running: true, startedAt: localSendTime });
    const view = renderIntl(<TurnContainer runner={running} />);
    // Running: no reply time that would later be replaced (the band shows the elapsed time).
    expect(botTime(view.container)).toBe("");
    expect(userTime()).toContain(zhClock(STARTED));

    vi.setSystemTime(new Date("2026-11-09T02:41:54Z"));
    act(() => live(events[1], T));
    const ended = { ...running, running: false, terminalStatus: "failed" };
    view.rerender(<TurnContainer runner={ended} />);
    expect(botTime(view.container)).toBe(zhClock(ENDED));

    // The next message is sent six minutes later: archiving must not re-stamp the reply with the browser clock.
    vi.setSystemTime(new Date(NEXT_SENT));
    const archived = snapshotArchivedTurn(ended);
    expect(archived).not.toBeNull();
    view.rerender(
      <TurnContainer
        runner={runner({
          archivedTurns: [archived as ArchivedTurn],
          turnId: "t_next", user: "And by month?", running: true, startedAt: Date.now(),
        })}
      />,
    );
    expect(botTime(view.container, 0)).toBe(zhClock(ENDED));
    expect(botTime(view.container, 0)).not.toBe(zhClock(NEXT_SENT));
    expect(userTime(0)).toContain(zhClock(STARTED));
    // The new turn has no server acknowledgement yet: no local send time in its bubble.
    expect(userTime(1)).not.toMatch(/\d{2}:\d{2}/);

    // Reload: the session DTO baseline, then the replayed event log; the same time throughout.
    cleanup();
    _resetAllTurnsForTesting();
    _resetNarrativeCursorsForTesting();
    const hydrated: ArchivedTurn = {
      turnId: T, terminalStatus: "failed", user: "Top 5 regions", userAttachments: [], answer: "", traceSteps: [],
      peersDispatched: [], errorReason: null,
      // As page.tsx `_toArchivedTurn`: persisted turns.ended_at / turns.started_at (written a moment apart from the events).
      endedAt: Date.parse("2026-11-09T02:41:53.4Z"), userSentAt: Date.parse("2026-11-09T02:40:09.8Z"),
    };
    const reloaded = renderIntl(<TurnContainer runner={runner({ archivedTurns: [hydrated] })} />);
    expect(botTime(reloaded.container)).toBe(zhClock(ENDED));
    act(() => { replayTurnEventRows(T, events); });
    expect(botTime(reloaded.container)).toBe(zhClock(ENDED));
    expect(userTime()).toContain(zhClock(STARTED));
  });
});

describe("reply byline of a clarification-paused-then-resumed turn", () => {
  const T = "t_7ab16f11ce9a";
  const STARTED = "2026-11-09T02:48:08Z";
  const PAUSED = "2026-11-09T02:48:20Z";
  const RESUMED = "2026-11-09T02:48:33Z";
  const ENDED = "2026-11-09T02:49:21Z";

  function events(): TurnEventDTO[] {
    rowSeq = 0;
    return [
      row("turn_started", { turn_id: T, session_id: "s_1", started_at: STARTED }),
      row("done", { turn_id: T, status: "clarification_pending", ended_at: PAUSED }),
      row("turn.resumed", { turn_id: T, execution_generation: 2 }),
      row("turn_started", { turn_id: T, session_id: "s_1", started_at: RESUMED }),
      row("done", { turn_id: T, status: "completed", ended_at: ENDED }),
    ];
  }

  it("shows the pause time while waiting, nothing while the resumed generation runs, then the final end", () => {
    const rows = events();
    vi.setSystemTime(new Date(PAUSED));
    live(rows[0], T);
    live(rows[1], T);
    const base = runner({ turnId: T, user: "Compare the regions", startedAt: Date.parse(STARTED) });
    const view = renderIntl(<TurnContainer runner={{ ...base, terminalStatus: "clarification_pending" }} />);
    expect(botTime(view.container)).toBe(zhClock(PAUSED));

    vi.setSystemTime(new Date(RESUMED));
    act(() => { live(rows[2], T); live(rows[3], T); });
    view.rerender(<TurnContainer runner={{ ...base, running: true, startedAt: Date.parse(RESUMED) }} />);
    expect(botTime(view.container)).toBe("");

    vi.setSystemTime(new Date(ENDED));
    act(() => live(rows[4], T));
    view.rerender(<TurnContainer runner={{ ...base, terminalStatus: "completed", startedAt: Date.parse(RESUMED) }} />);
    expect(botTime(view.container)).toBe(zhClock(ENDED));
    // The user byline keeps the first generation's start.
    expect(userTime()).toContain(zhClock(STARTED));
  });

  it("after reload, as the session's last turn, shows the end 18:49, not the start 18:48", () => {
    // Before: the hydrated last turn rendered `runner.startedAt` (turns.started_at) as its reply time.
    vi.setSystemTime(new Date("2026-11-09T03:00:00Z"));
    const hydratedLast = runner({
      turnId: T, user: "Compare the regions", answer: "East leads.", terminalStatus: "completed",
      startedAt: Date.parse(STARTED), persistedStartedAt: Date.parse(STARTED), persistedEndedAt: Date.parse(ENDED),
    });
    const view = renderIntl(<TurnContainer runner={hydratedLast} />);
    expect(botTime(view.container)).toBe(zhClock(ENDED));
    act(() => { replayTurnEventRows(T, events()); });
    expect(botTime(view.container)).toBe(zhClock(ENDED));
    expect(botTime(view.container)).not.toBe(zhClock(STARTED));
    expect(userTime()).toContain(zhClock(STARTED));
  });
});

describe("user byline before the server acknowledges the turn", () => {
  it("shows no browser-clock time that the server time would later replace", () => {
    const T = "t_skew";
    // The browser clock is 49.5 s slow: the local send time reads 18:50, the server start 18:51.
    vi.setSystemTime(new Date("2026-11-09T02:50:40Z"));
    const view = renderIntl(
      <TurnContainer runner={runner({ turnId: T, user: "Revenue by week", running: true, startedAt: Date.now() })} />,
    );
    expect(userTime()).not.toContain(zhClock("2026-11-09T02:50:40Z"));
    expect(userTime()).not.toMatch(/\d{2}:\d{2}/);

    act(() => {
      dispatchToTurnStore("turn_started", { turn_id: T, seq: 1, started_at: "2026-11-09T02:51:29.5Z" }, "live", T);
    });
    expect(userTime()).toContain(zhClock("2026-11-09T02:51:29.5Z"));
  });
});

describe("byline locale and legacy turns", () => {
  it("follows the interface language and adds the date for an earlier day", () => {
    document.documentElement.lang = "en-US";
    vi.setSystemTime(new Date(2026, 10, 9, 9, 0));
    const archived: ArchivedTurn = {
      turnId: "t_yesterday", terminalStatus: "completed", user: "q", userAttachments: [], answer: "a", traceSteps: [],
      peersDispatched: [], errorReason: null,
      endedAt: new Date(2026, 10, 8, 18, 39).getTime(), userSentAt: new Date(2026, 10, 8, 18, 38).getTime(),
    };
    const { container } = renderIntl(<TurnContainer runner={runner({ archivedTurns: [archived] })} />, { locale: "en-US" });
    expect(botTime(container).replace(/\s/g, " ")).toBe("Nov 8, 6:39 PM");
    expect(userTime().replace(/\s/g, " ")).toContain("Nov 8, 6:38 PM");
  });

  it("shows no time, and never Invalid Date, for a legacy turn without server times", () => {
    const archived: ArchivedTurn = {
      turnId: "t_legacy", terminalStatus: "completed", user: "Top 5 last 30 days", userAttachments: [], answer: "a",
      traceSteps: [], peersDispatched: [], errorReason: null, endedAt: Number.NaN,
    };
    const { container } = renderIntl(<TurnContainer runner={runner({ archivedTurns: [archived] })} />);
    expect(container.textContent).not.toContain("Invalid");
    expect(container.textContent).not.toContain("NaN");
    expect(botTime(container)).toBe("");
    expect(screen.getByText(/Top 5 last 30 days/)).toBeTruthy();
  });
});
