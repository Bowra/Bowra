/** The browser tab follows the open session's display title, so a rename shows at once (116 dogfood 1.5.25). */

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: "s_1" as string | null,
  canvasId: "cv_1" as string | null,
  title: "Canvas title" as string | null,
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

import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";
import { getPageTitleSnapshot } from "@/lib/page-title-store";

import { ChatWorkbench } from "./chat-workbench";

const runner = {
  turnId: "t_1",
  sessionId: "s_1",
  started: true,
  messages: [],
  traceSteps: [],
  citations: [],
  steerAcks: [],
  artifacts: [],
  status: "idle",
};

function workbench(documentTitle?: string | null) {
  return (
    <ChatWorkbench
      runner={runner as never}
      onSend={vi.fn()}
      onCancel={vi.fn()}
      readOnly={false}
      documentTitle={documentTitle}
    />
  );
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

describe("ChatWorkbench tab title", () => {
  it("follows the session's display title and updates on rename without a reload", () => {
    const { rerender } = renderIntl(workbench("[df-1.5.22] 双轴图验收"));
    expect(getPageTitleSnapshot()).toBe("[df-1.5.22] 双轴图验收");

    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        {workbench("[df-1.5.22] 双轴图验收（已重命名）")}
      </NextIntlClientProvider>,
    );
    expect(getPageTitleSnapshot()).toBe("[df-1.5.22] 双轴图验收（已重命名）");
  });

  it("falls back to the canvas title when the page gives no session title", () => {
    renderIntl(workbench(null));
    expect(getPageTitleSnapshot()).toBe("Canvas title");
  });
});
