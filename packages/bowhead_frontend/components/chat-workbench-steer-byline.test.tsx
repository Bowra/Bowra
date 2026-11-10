/**
 * The steering bubble above the composer formats its time in the interface locale and leaves the segment
 * out when the acknowledgement carries none (2026-11-08 dogfood F10: "You · · interjected", fixed zh-CN).
 */

import { act, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Stable snapshot: the canvas store is read through useSyncExternalStore.
const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: "s_1" as string | null,
  canvasId: "cv_1" as string | null,
  title: null as string | null,
  streamingSectionIds: new Set<string>(),
};

vi.mock("@/lib/canvas-store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  subscribeCanvas: () => () => {},
  getCanvasSnapshot: () => canvasState,
  selectReportSurfaceSections: () => canvasState.sections,
  selectVisibleSections: () => canvasState.sections,
}));
vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: () => true }));
vi.mock("@/lib/use-results-capabilities", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useResultsCapabilities: () => ({
    workspace: false,
    completeList: false,
    scaleNav: false,
    resolved: true,
    error: null,
  }),
}));
vi.mock("./canvas/data-tab-files", () => ({
  DataTabFiles: () => <div data-testid="data-tab-files" />,
  useArtifactItems: () => ({ items: [], error: null, loading: false, retry: vi.fn() }),
}));
vi.mock("./canvas/report-view", async () => {
  const { forwardRef } = await import("react");
  return {
    ReportView: forwardRef(function MockReportView() {
      return <div data-testid="report-view" />;
    }),
  };
});

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, applyEmojiAck } from "@/lib/turn-store";

import { ChatWorkbench } from "./chat-workbench";

const TURN_ID = "t_steer_byline";
const runner = {
  turnId: TURN_ID,
  sessionId: "s_1",
  started: true,
  running: true,
  messages: [],
  traceSteps: [],
  citations: [],
  steerAcks: [],
  artifacts: [],
  status: "running",
};
/** 14:05 local time, so the expectations below do not depend on the machine's time zone. */
const AT_14_05 = new Date(2026, 10, 8, 14, 5).toISOString();

function bylineOf(container: HTMLElement, suffix: string): string {
  const mount = container.querySelector(".bh-steer-mount");
  return Array.from(mount?.querySelectorAll("div") ?? [])
    .find((node) => node.children.length === 0 && node.textContent?.endsWith(suffix))
    ?.textContent ?? "";
}

beforeEach(() => {
  _resetAllTurnsForTesting();
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: () => null,
      length: 0,
    },
  });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: true,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }),
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  _resetAllTurnsForTesting();
});

function steer(ts?: string) {
  act(() => {
    applyEmojiAck({ turnId: TURN_ID, ack: { msgId: "m_1", emoji: "🤝", text: "Only East China", ts } });
  });
}

describe("ChatWorkbench steering byline", () => {
  it("formats the time in the interface locale, not a fixed zh-CN", () => {
    // Mutation: format with `toLocaleTimeString("zh-CN", …)` again → "14:05" in the English interface.
    // Same calendar day, so the byline shows the time alone (an earlier day adds the date, like every chat byline).
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 10, 8, 20, 0));
    steer(AT_14_05);
    const { container } = renderIntl(
      <ChatWorkbench runner={runner as never} onSend={vi.fn()} onCancel={vi.fn()} readOnly={false} />,
      { locale: "en-US" },
    );
    expect(bylineOf(container, "interjected")).toMatch(/^You · 2:05\sPM · interjected$/u);
  });

  it("reads 'You · interjected' when the acknowledgement has no time", () => {
    steer(undefined);
    const { container } = renderIntl(
      <ChatWorkbench runner={runner as never} onSend={vi.fn()} onCancel={vi.fn()} readOnly={false} />,
      { locale: "en-US" },
    );
    expect(bylineOf(container, "interjected")).toBe("You · interjected");
  });
});
