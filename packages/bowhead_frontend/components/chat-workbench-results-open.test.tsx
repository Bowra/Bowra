/**
 * Chat running state R2.4–R2.7 at the workbench seam: when a Turn ends, Results opens by itself only on the file
 * Lead's delivery review named (`open_for_reader`), on desktop, when it was closed. Otherwise it stays closed and a
 * one-time reminder beside the strip says what the Turn added; 查看 opens Results and records the choice.
 */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: null as string | null,
  canvasId: null as string | null,
  streamingSectionIds: new Set<string>(),
  // Real `CanvasState` fields the live report card reads (chat running state R2.1).
  bufferedLeadSectionIds: new Set<string>(),
  turnPatched: null,
};
let desktop = true;
const resultsCapabilities = {
  workspace: true,
  completeList: false,
  scaleNav: false,
  resolved: true,
  error: null as null | "unavailable" | "timeout" | "auth",
};
const DECK = {
  id: "art_deck",
  filename: "季度复盘.pptx",
  mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  size: 2048,
  source: "sandbox",
  created_at: "2026-11-25T00:00:00Z",
  preview_available: true,
  kind: "deliverable",
  created_by_turn: "t_ro",
};
const artifacts = { items: [] as unknown[] };
const refresh = vi.fn(async () => artifacts.items);

vi.mock("@/lib/canvas-store", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return {
    ...actual,
    subscribeCanvas: () => () => {},
    getCanvasSnapshot: () => canvasState,
    selectReportSurfaceSections: () => canvasState.sections,
    selectVisibleSections: () => canvasState.sections,
  };
});
vi.mock("@/lib/use-results-capabilities", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useResultsCapabilities: () => resultsCapabilities,
  useResultsCapabilitiesSnapshot: () => resultsCapabilities,
}));
vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: () => desktop }));
vi.mock("./canvas/data-tab-files", () => ({
  DataTabFiles: () => <div data-testid="data-tab-files" />,
  useArtifactItems: () => ({ items: artifacts.items, error: null, loading: false, retry: vi.fn(), refresh }),
}));
vi.mock("./canvas/report-view", async () => {
  const { forwardRef } = await import("react");
  return {
    ReportView: forwardRef(function MockReportView() {
      return (
        <div data-testid="report-view">
          {(canvasState.sections as { id: string }[]).map((section) => <section key={section.id} data-section-id={section.id} />)}
        </div>
      );
    }),
  };
});

import { NextIntlClientProvider } from "next-intl";

import zhMessages from "@/messages/zh-CN.json";
import { requestCanvasResultOpen, requestResultsTurnFiles } from "@/lib/canvas-workspace-focus";

import { ChatWorkbench } from "./chat-workbench";

const STORE_KEY = "bowhead.results_open.v1";

function runner(running: boolean, finalPayload?: unknown) {
  return {
    turnId: "t_ro",
    sessionId: "s_ro",
    started: true,
    running,
    messages: [],
    traceSteps: [],
    citations: [],
    steerAcks: [],
    artifacts: [],
    status: running ? "running" : "done",
    finalPayload,
  };
}

function wrap(ui: ReactElement) {
  return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{ui}</NextIntlClientProvider>;
}

function workbench(r: Record<string, unknown>) {
  return wrap(<ChatWorkbench runner={r as never} onSend={vi.fn()} onCancel={vi.fn()} readOnly={false} />);
}

/** A Turn runs with Results closed, then ends with this final payload. */
async function endTurn(finalPayload: unknown) {
  const view = render(workbench(runner(true)));
  view.rerender(workbench(runner(false, finalPayload)));
  await waitFor(() => expect(refresh).toHaveBeenCalledWith("turn-terminal"));
  return view;
}

let store: Map<string, string>;
beforeEach(() => {
  window.history.replaceState(null, "", "/?session=s_ro");
  canvasState.sections = [];
  canvasState.canvasId = null;
  canvasState.sessionId = null;
  desktop = true;
  artifacts.items = [DECK];
  refresh.mockClear();
  store = new Map<string, string>();
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
      matches: desktop,
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

describe("Results when a Turn ends (chat running state R2.4–R2.7)", () => {
  it("opens on the file Lead named for the reader", async () => {
    const { container } = await endTurn({ open_for_reader: "art_deck", outputs: [{ kind: "presentation", count: 1 }] });
    await waitFor(() => expect(window.location.search).toContain("art_deck"));
    await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).toBeNull());
    expect(screen.queryByTestId("results-turn-bubble")).toBeNull();
  });

  it("stays closed when Lead named nothing, and the reminder says what the Turn added", async () => {
    const { container } = await endTurn({ outputs: [{ kind: "presentation", count: 1 }], work_files: { count: 2 } });
    const bubble = await screen.findByTestId("results-turn-bubble");
    expect(bubble).toHaveTextContent("本轮新增：1 份演示文稿 · 2 个工作文件");
    expect(window.location.search).not.toContain("art_deck");
    expect(container.querySelector(".v2-ws-edge--canvas")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    await waitFor(() => expect(screen.queryByTestId("results-turn-bubble")).toBeNull());
    await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).toBeNull());
    expect(JSON.parse(store.get(STORE_KEY) ?? "{}").s_ro?.state).toBe("open");
  });

  it("ignores a named file that is not among the conversation's results", async () => {
    // The refreshed list holds the deck, not the file named.
    await endTurn({ open_for_reader: "art_gone", outputs: [{ kind: "presentation", count: 1 }] });
    expect(await screen.findByTestId("results-turn-bubble")).toBeInTheDocument();
    expect(window.location.search).not.toContain("art_gone");
  });

  it("opens nothing on a phone and leaves the count to the Results tab", async () => {
    desktop = false;
    await endTurn({ open_for_reader: "art_deck", outputs: [{ kind: "presentation", count: 1 }] });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(window.location.search).not.toContain("art_deck");
    expect(screen.queryByTestId("results-turn-bubble")).toBeNull();
  });
});

describe("a chat report card (chat running state R2.2)", () => {
  it("opens the report in Results at the section it named", async () => {
    canvasState.canvasId = "cv_ro";
    canvasState.sessionId = "s_ro";
    canvasState.sections = [{ id: "sec_a", kind: "narrative", text: "A" }, { id: "sec_b", kind: "narrative", text: "B" }];
    const scrolled: (string | null)[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
      scrolled.push(this.getAttribute("data-section-id"));
    };
    try {
      const { container } = render(workbench(runner(false)));
      await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).not.toBeNull());
      act(() => {
        requestCanvasResultOpen({ type: "report", sectionId: "sec_b" }, { entryPoint: "chat" });
      });
      await waitFor(() => expect(scrolled).toContain("sec_b"));
      expect(scrolled).not.toContain("sec_a");
      expect(JSON.parse(store.get(STORE_KEY) ?? "{}").s_ro?.state).toBe("open");
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});

describe("a Turn's work-file line (chat running state R2.3)", () => {
  it("opens Results and records the choice", async () => {
    const { container } = render(workbench(runner(false)));
    await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).not.toBeNull());
    act(() => {
      requestResultsTurnFiles("t_ro");
    });
    await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).toBeNull());
    expect(JSON.parse(store.get(STORE_KEY) ?? "{}").s_ro?.state).toBe("open");
  });

  it("lists only that Turn's files when Results has its list", async () => {
    resultsCapabilities.scaleNav = true;
    resultsCapabilities.completeList = true;
    try {
      const { container } = render(workbench(runner(false)));
      await waitFor(() => expect(container.querySelector(".v2-ws-edge--canvas")).not.toBeNull());
      act(() => {
        requestResultsTurnFiles("t_ro");
      });
      await waitFor(() => expect(window.location.search).toContain("results=all"));
      expect(JSON.stringify(window.history.state)).toContain('"turn_id":"t_ro"');
    } finally {
      resultsCapabilities.scaleNav = false;
      resultsCapabilities.completeList = false;
    }
  });
});

describe("the strip's count (chat running state R2.6)", () => {
  it("counts a changed report once and each new file, as Results lists them", async () => {
    // 116, 1.7.89: a Turn that wrote five sections and saved two files read "11 项新内容" while its reminder
    // listed five sections, a chart and two work files.
    artifacts.items = [];
    canvasState.canvasId = "cv_ro";
    canvasState.sessionId = "s_ro";
    canvasState.sections = [];
    const view = render(workbench(runner(true)));
    canvasState.sections = ["a", "b", "c", "d", "e"].map((id) => ({ id: `sec_${id}`, kind: "narrative", text: id }));
    artifacts.items = [DECK, { ...DECK, id: "art_csv", filename: "data.csv", kind: "sandbox_output" }];
    view.rerender(workbench(runner(false, {})));
    await waitFor(() => expect(screen.getByRole("button", { name: /重开成果，有 3 项新内容/ })).toBeInTheDocument());
  });
});

