/**
 * UX-2 (chat-solidify 2026-07-03) — liveness indicator during mid-turn silence.
 *
 * User report:「chat 跑的时候有时候像卡住了, 不知道还在运行」. Facts: after
 * 30s without a chunk, backend emits streaming.state{paused} (connection remains healthy), but FE only rendered
 * errors (NetworkBanner), with no visible paused signal; existing working indicator appeared only
 * in the opening phase with no beats.
 *
 * ★ chat-ux-2026-08 (1e running band): **move the carrier, retain the capability**.
 * When the band owns running state, its current row carries the signal (`data-paused="1"` +「仍在等模型响应」);
 * `MidTurnLivenessNote` no longer adds a separate row when the band is present. Rendering both creates a second
 * `role=status` on screen, making screen readers announce twice (invariant inventory §2.7).
 * ⚠ Thus pin **both** rules here:
 *   ① Band present: it carries paused semantics, with no second liveness row;
 *   ② Band **absent** (user requested details → bandMode off): `MidTurnLivenessNote` still appears.
 * Without ②, deleting MidTurnLivenessNote entirely also passes — genuinely losing the capability.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyNarrativeAppend,
} from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";

import { TurnContainer } from "./TurnContainer";

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

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

function seedTurnWithContent(turnId: string): void {
  applyCardCreated({ turnId });
  applyNarrativeAppend({ turnId, segId: "seg-1", text: "正在分析销售数据" });
}

describe("TurnContainer · UX-2 MidTurnLivenessNote", () => {
  it("★ 带在场 + paused → 语义在带上, 且不出第二条活性行", () => {
    seedTurnWithContent("t-live1");
    const { container } = renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-live1",
          user: "分析",
          running: true,
          startedAt: Date.now() - 45_000,
          streamingState: "paused",
        })}
      />,
    );
    const band = container.querySelector('[data-testid="running-band"]');
    expect(band).toBeTruthy();
    expect(band?.getAttribute("data-paused")).toBe("1");
    expect(band?.textContent).toMatch(/仍在等模型响应/);
    // Elapsed seconds remain visible (moved to the disclosure band).
    expect(band?.textContent).toMatch(/4[45] 秒/);
    // Only one announcement source on screen.
    expect(screen.queryByTestId("midturn-liveness-note")).toBeNull();
  });

  it("★ 正对照: 带不在场(用户已要求看细节)时 MidTurnLivenessNote 照常出现", () => {
    // Mutation: delete MidTurnLivenessNote entirely → this must fail. Without this test, the preceding
    // `queryByTestId(...).toBeNull()` always passes simply because the component does not exist.
    seedTurnWithContent("t-live1b");
    const { container } = renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-live1b",
          user: "分析",
          running: true,
          startedAt: Date.now() - 45_000,
          streamingState: "paused",
        })}
      />,
    );
    // Click band = request details → bandMode off → return to trace layout.
    fireEvent.click(container.querySelector('[data-testid="running-band-bar"]') as HTMLElement);
    // Yielding means the **three rows** give way to full trace (disclosure band stays as collapse entry since 2026-08-15).
    expect(container.querySelector(".bh-band__now")).toBeNull();
    const note = screen.getByTestId("midturn-liveness-note");
    expect(note.textContent).toMatch(/仍在运行/);
    expect(note.textContent).toMatch(/4[45] 秒/);
  });

  it("running + active → 不出现", () => {
    seedTurnWithContent("t-live2");
    renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-live2",
          user: "分析",
          running: true,
          streamingState: "active",
        })}
      />,
    );
    expect(screen.queryByTestId("midturn-liveness-note")).toBeNull();
  });

  it("非 running(turn 已完成) + 残留 paused → 不出现", () => {
    seedTurnWithContent("t-live3");
    renderIntl(
      <TurnContainer
        runner={runner({
          turnId: "t-live3",
          user: "分析",
          running: false,
          streamingState: "paused",
        })}
      />,
    );
    expect(screen.queryByTestId("midturn-liveness-note")).toBeNull();
  });
});
