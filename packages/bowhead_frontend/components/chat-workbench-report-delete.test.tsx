/** Workbench top-bar「删除报告」uses an in-app confirmation: cancel never deletes, confirm does; copy includes the block count. */

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: null as string | null,
  canvasId: null as string | null,
  streamingSectionIds: new Set<string>(),
};

const api = vi.hoisted(() => ({
  deleteCanvas: vi.fn(),
  clearCanvas: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteCanvas: api.deleteCanvas,
  clearCanvas: api.clearCanvas,
}));
vi.mock("@/lib/canvas-store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  subscribeCanvas: () => () => {},
  getCanvasSnapshot: () => canvasState,
  selectReportSurfaceSections: () => canvasState.sections,
  selectVisibleSections: () => canvasState.sections,
  resetCanvas: vi.fn(),
}));
vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: () => true }));
// Classic report top bar (outside the Results workspace shell):「更多」menu mounts directly in the report header.
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
import { ToastContainer } from "@/components/chat/toast-container";
import { resetCanvas } from "@/lib/canvas-store";
import { _clearAllToasts } from "@/lib/use-toast";

import { ChatWorkbench } from "./chat-workbench";

function renderWorkbench() {
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
  return renderIntl(
    <ChatWorkbench runner={runner as never} onSend={vi.fn()} onCancel={vi.fn()} readOnly={false} />,
  );
}

function openDeleteConfirm() {
  fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
  fireEvent.click(screen.getByRole("menuitem", { name: "删除报告…" }));
  return screen.findByRole("dialog", { name: "删除这份报告？" });
}

beforeEach(() => {
  canvasState.sections = [
    { id: "sec_1", hidden: false },
    { id: "sec_2", hidden: false },
  ];
  canvasState.canvasId = "cv_1";
  canvasState.sessionId = "s_1";
  api.deleteCanvas.mockResolvedValue(undefined);
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
  _clearAllToasts();
  vi.clearAllMocks();
});

describe("ChatWorkbench 顶栏删除报告", () => {
  it("取消不删除；确认后才调用 deleteCanvas", async () => {
    renderWorkbench();

    let dialog = await openDeleteConfirm();
    expect(within(dialog).getByText("删除后无法恢复，报告中的 2 个区块会一起删除。")).toBeTruthy();
    const cancel = within(dialog).getByRole("button", { name: "取消" });
    expect(document.activeElement).toBe(cancel);
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "删除这份报告？" })).toBeNull());
    expect(api.deleteCanvas).not.toHaveBeenCalled();
    // Its opening menu item disappeared with the closed menu; return focus to「更多操作」instead of the page.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "更多操作" })));

    dialog = await openDeleteConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除报告" }));
    await waitFor(() => expect(api.deleteCanvas).toHaveBeenCalledWith("cv_1"));
  });

  it("确认框开着时当前报告换成了别的（切换会话）：不删、不清空眼前的报告，并说明", async () => {
    renderIntl(<ToastContainer />);
    renderWorkbench();

    const dialog = await openDeleteConfirm();
    // For example, browser Back switches sessions, leaving a different report in view.
    canvasState.canvasId = "cv_2";
    canvasState.sessionId = "s_2";
    fireEvent.click(within(dialog).getByRole("button", { name: "删除报告" }));

    expect(await screen.findByText("报告已切换，未删除。请在当前报告上重新操作。")).toBeTruthy();
    expect(api.deleteCanvas).not.toHaveBeenCalled();
    expect(resetCanvas).not.toHaveBeenCalled();
  });

  it("确认后报告没变：删除确认时那一份并清空视图", async () => {
    renderWorkbench();
    const dialog = await openDeleteConfirm();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除报告" }));
    await waitFor(() => expect(api.deleteCanvas).toHaveBeenCalledWith("cv_1"));
    await waitFor(() => expect(resetCanvas).toHaveBeenCalledTimes(1));
  });

  it("「更多操作」菜单：键盘打开进第一项，Esc 关闭并回焦，点外面关闭", async () => {
    renderWorkbench();
    const more = screen.getAllByRole("button", { name: "更多操作" })[0];

    fireEvent.click(more, { detail: 0 });
    const items = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(more));

    fireEvent.click(more);
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.pointerDown(document.body);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("「导出」菜单：键盘打开进第一个格式，Esc 回到「导出」按钮", async () => {
    renderWorkbench();
    const exportButton = screen.getAllByRole("button", { name: "导出" })[0];

    fireEvent.click(exportButton, { detail: 0 });
    const [first] = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(first));
    fireEvent.keyDown(first!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(exportButton));
  });
});

// Behavior batch A: 「删除报告」 carries 「…」 because it confirms first; confirmation button copy stays unchanged.
describe("ChatWorkbench report deletion menu items", () => {
  it("marks Delete report with an ellipsis and still confirms first", async () => {
    renderWorkbench();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    expect(screen.queryByRole("menuitem", { name: "删除报告" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "删除报告…" }));
    const dialog = await screen.findByRole("dialog", { name: "删除这份报告？" });
    expect(within(dialog).getByRole("button", { name: "删除报告" })).toBeTruthy();
    expect(api.deleteCanvas).not.toHaveBeenCalled();
  });

  it("marks Clear report as danger, confirms first, and cancel keeps the report", async () => {
    renderWorkbench();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    const clear = screen.getByRole("menuitem", { name: "清空报告…" });
    expect(clear.classList.contains("danger")).toBe(true);
    fireEvent.click(clear);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "清空报告" })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.clearCanvas).not.toHaveBeenCalled();
  });
});
