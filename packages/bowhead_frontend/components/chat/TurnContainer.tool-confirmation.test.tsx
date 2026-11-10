/**
 * Inline tool call confirmation · visibility while the turn runs. The collapsed running band
 * replaces the step trace, so a card attached to its waiting step would be hidden; the card
 * must be on screen in every live layout, and exactly once.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import {
  _resetAllTurnsForTesting,
  applyCardCreated,
  applyConfirmationRequested,
  applyStepStarted,
} from "@/lib/turn-store";
import type { TurnRunnerState } from "@/lib/types";
import type { ToolName } from "./step-list/types";

vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations: () => (key: string) => key,
}));
vi.mock("@/lib/tool-confirmation-api", () => ({
  getToolConfirmation: () => new Promise(() => undefined),
  decideToolConfirmation: vi.fn(),
}));

import { TurnContainer } from "./TurnContainer";

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

function seedWaitingStep(turnId: string): void {
  applyCardCreated({ turnId });
  applyStepStarted({
    turnId,
    stepId: "step_call_1",
    stepType: "tool_run",
    toolName: "send_email" as ToolName,
    headerZh: "Send email",
    startedAt: new Date(),
  });
  applyConfirmationRequested({
    turnId,
    stepId: "step_call_1",
    confirmation: {
      id: "esc_one",
      kind: "email_send",
      requesterUserId: "u_owner",
      title: "send_email",
      sourceLabel: null,
      category: null,
      choices: ["reject", "approve"],
      expiresAt: "2026-11-08T10:03:00Z",
      timeoutSeconds: 180,
    },
  });
}

const cards = (container: HTMLElement) => container.querySelectorAll("[data-tool-confirmation-id='esc_one']");

beforeEach(() => _resetAllTurnsForTesting());
afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
});

describe("confirmation card while the turn runs", () => {
  it("shows the card below the collapsed running band, once", () => {
    const turnId = "t-band-confirm";
    seedWaitingStep(turnId);
    const { container } = render(<TurnContainer runner={runner({ turnId, user: "send it", running: true })} />);
    expect(container.querySelector('[data-testid="running-band"]')).toBeTruthy();
    expect(cards(container)).toHaveLength(1);
    expect(cards(container)[0]!.closest('[data-testid="band-tool-confirmations"]')).toBeTruthy();
  });

  it("moves the card under its step when the band is expanded in place, without a second copy", () => {
    const turnId = "t-band-expanded-confirm";
    seedWaitingStep(turnId);
    const { container } = render(<TurnContainer runner={runner({ turnId, user: "send it", running: true })} />);
    const band = container.querySelector('[data-testid="running-band"]')!;
    fireEvent.click(band.querySelector("button[aria-expanded]")!);
    expect(container.querySelector('[data-testid="execution-disclosure"][data-placement="band-slot"]')).toBeTruthy();
    expect(cards(container)).toHaveLength(1);
    expect(cards(container)[0]!.closest('[data-testid="band-tool-confirmations"]')).toBeNull();
    expect(cards(container)[0]!.closest(".bh-step-list")).toBeTruthy();
  });
});
