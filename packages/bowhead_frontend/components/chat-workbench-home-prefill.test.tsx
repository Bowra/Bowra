/**
 * A prefill taken by the home composer while `homePane` stands in for the conversation (an editor open before the
 * first message, e.g. a trial beside the Agent editor) must not come back in the conversation's composer after the
 * send (2026-11-27 dogfood F28: a sent case question stayed in the input, active, and in the saved draft).
 */

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: "s_1" as string | null,
  canvasId: "cv_1" as string | null,
  title: null as string | null,
  streamingSectionIds: new Set<string>(),
  bufferedLeadSectionIds: new Set<string>(),
  turnPatched: null,
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

import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";

import { ChatWorkbench } from "./chat-workbench";

const QUESTION = "本周销售怎么样？";

function runner(started: boolean) {
  return {
    turnId: started ? "t_1" : null,
    sessionId: started ? "s_1" : null,
    started,
    messages: [],
    traceSteps: [],
    citations: [],
    steerAcks: [],
    artifacts: [],
    status: "idle",
  };
}

function workbench(props: { started: boolean; prefillText: string | null; homePane?: ReactNode }) {
  return (
    <ChatWorkbench
      runner={runner(props.started) as never}
      onSend={vi.fn()}
      onCancel={vi.fn()}
      readOnly={false}
      prefillText={props.prefillText}
      homePane={props.homePane}
    />
  );
}

function composerText(container: HTMLElement): string {
  return container.querySelector("textarea")?.value ?? "";
}

beforeEach(() => {
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

afterEach(() => cleanup());

describe("ChatWorkbench prefill with a home pane", () => {
  it("leaves a prefill the home composer took out of the conversation's composer", () => {
    // Mutation: latch `prefillText` while `homePane` is shown → the sent question is back in the input.
    const { container, rerender } = renderIntl(workbench({ started: false, prefillText: QUESTION, homePane: <div data-testid="home" /> }));
    // The home composer consumed it; the page clears the prefill and the send starts the conversation.
    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        {workbench({ started: false, prefillText: null, homePane: <div data-testid="home" /> })}
      </NextIntlClientProvider>,
    );
    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        {workbench({ started: true, prefillText: null })}
      </NextIntlClientProvider>,
    );
    expect(container.querySelector("textarea")).not.toBeNull();
    expect(composerText(container)).toBe("");
  });

  it("still fills its own composer when the conversation is shown", () => {
    const { container } = renderIntl(workbench({ started: true, prefillText: QUESTION }));
    expect(composerText(container)).toBe(QUESTION);
  });
});
