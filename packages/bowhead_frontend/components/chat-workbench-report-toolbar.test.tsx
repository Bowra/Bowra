/**
 * Chat workbench and report canvas batch 4b: sharing entry and status group in the results report toolbar.
 *   · Flag on: toolbar has a「分享」icon button (same visibility as「复制权限链接」in「更多」: report session exists, not shared read-only view);
 *     it opens the existing report share dialog.「复制权限链接」stays in「更多」; toolbar has a status group, passed to ReportView as its host.
 *   · Flag off: no share button/status group, and no status host passed to ReportView (same as before).
 */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ComponentProps } from "react";

const chartReferences = vi.hoisted(() => ({ read: vi.fn() }));
vi.mock("@/lib/chart/chart-followup", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  chartFollowupReference: chartReferences.read,
}));

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: null as string | null,
  canvasId: null as string | null,
  title: "华东 Q3 下滑归因" as string | null,
  status: "done" as string,
  currentTurnId: null as string | null,
  streamingSectionIds: new Set<string>(),
};

vi.mock("@/lib/canvas-store", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  subscribeCanvas: () => () => {},
  getCanvasSnapshot: () => canvasState,
  selectReportSurfaceSections: () => canvasState.sections,
  selectVisibleSections: () => canvasState.sections,
}));
const caps = vi.hoisted(() => ({ workspace: true, completeList: false, scaleNav: false, resolved: true, error: null }));
vi.mock("@/lib/use-results-capabilities", async (importOriginal) => {
  return {
    ...(await importOriginal<Record<string, unknown>>()),
    useResultsCapabilities: () => caps,
    useResultsCapabilitiesSnapshot: () => caps,
  };
});
const media = vi.hoisted(() => ({ matches: true }));
vi.mock("@/lib/use-media-query", () => ({ useMediaQuery: (query: string) => query.includes("min-width") ? true : media.matches }));
vi.mock("./canvas/data-tab-files", () => ({
  DataTabFiles: () => <div data-testid="data-tab-files" />,
  useArtifactItems: () => ({ items: [], error: null, loading: false, retry: vi.fn() }),
}));
const reportViewProps = vi.hoisted(() => ({ last: null as Record<string, unknown> | null, enterEditMode: null as null | (() => void), openHistory: vi.fn() }));
vi.mock("./canvas/report-view", async () => {
  const { forwardRef, useImperativeHandle } = await import("react");
  return {
    ReportView: forwardRef(function MockReportView(props: Record<string, unknown>, ref) {
      reportViewProps.last = props;
      useImperativeHandle(ref, () => ({ enterEditMode: () => reportViewProps.enterEditMode?.(), openHistory: reportViewProps.openHistory }));
      return <div data-testid="report-view" />;
    }),
  };
});
vi.mock("@/components/share/share-dialog", () => ({
  ShareDialog: (props: {
    kind: string;
    resourceId: string;
    label?: string;
    onClose: () => void;
    returnFocusRef?: { current: HTMLElement | null };
  }) => (
    <div
      data-return-focus={props.returnFocusRef?.current?.getAttribute("aria-label") ?? ""} role="dialog" aria-label="share-dialog" data-kind={props.kind} data-resource={props.resourceId} data-label={props.label}>
      <button type="button" onClick={props.onClose}>
        close-share
      </button>
    </div>
  ),
}));

import { renderIntl } from "@/__tests__/intl-render";

import { ChatWorkbench } from "./chat-workbench";
import { requestChartFollowup } from "@/lib/chart/chart-followup";

function renderWorkbench(opts: { reportOnly?: boolean; readOnly?: boolean; runner?: Record<string, unknown>; onSend?: ComponentProps<typeof ChatWorkbench>["onSend"] } = {}) {
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
    terminalStatus: "aborted",
    ...opts.runner,
  };
  return renderIntl(
    <ChatWorkbench runner={runner as never} onSend={opts.onSend ?? vi.fn()} onCancel={vi.fn()} readOnly={opts.readOnly ?? false} reportOnly={opts.reportOnly} />,
  );
}

beforeEach(() => {
  chartReferences.read.mockReset();
  canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
  canvasState.canvasId = "cv_1";
  canvasState.sessionId = "s_1";
  canvasState.status = "done";
  canvasState.currentTurnId = null;
  reportViewProps.last = null;
  reportViewProps.enterEditMode = null;
  caps.scaleNav = false;
  media.matches = true;
  paneWidth = 0;
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
  vi.clearAllMocks();
});

const toolbar = () => document.querySelector<HTMLElement>(".results-report-object-toolbar");

describe("chart reference draft admission", () => {
  const reference = { fileId: "art_chart", filename: "monthly-results.xlsx", mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", size: 1532, referenceKind: "result" as const };
  it("fills exact metadata through the existing Composer without submitting", async () => {
    const onSend = vi.fn();
    chartReferences.read.mockResolvedValue(reference);
    renderWorkbench({ onSend });
    act(() => { requestChartFollowup({ title: "Quarterly results", dataArtifactId: reference.fileId }); });
    await waitFor(() => expect(screen.getByText(reference.filename)).toBeInTheDocument());
    expect(chartReferences.read).toHaveBeenCalledWith("s_1", reference.fileId, expect.any(AbortSignal));
    expect(onSend).not.toHaveBeenCalled();
  });
  it("replaces a repeated file's selected scope and sends it only after explicit Send", async () => {
    const onSend = vi.fn();
    chartReferences.read.mockResolvedValue(reference);
    renderWorkbench({ onSend });
    const scope = { version: 1 as const, sectionId: "sec", sectionRev: 2, viewId: "trend", bindingId: "time.month", datasetDigest: "a".repeat(64), measureKeys: ["revenue"], tuples: [["2026-01"]] };
    act(() => { requestChartFollowup({ title: "Quarterly results", dataArtifactId: reference.fileId, selectedContext: scope }); });
    await waitFor(() => expect(screen.getByText(reference.filename)).toBeInTheDocument());
    const second = { ...scope, tuples: [["2026-02"]] };
    act(() => { requestChartFollowup({ title: "Quarterly results", dataArtifactId: reference.fileId, selectedContext: second }); });
    await waitFor(() => expect(document.body).toHaveTextContent("2026-02"));
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getAllByText(reference.filename)).toHaveLength(1);
    fireEvent.click(screen.getByLabelText("发送"));
    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][1]).toEqual([{ ...reference, selectedContext: second }]);
    expect(onSend.mock.calls[0][0]).toContain("2026-02");
    expect(onSend.mock.calls[0][0]).not.toContain("2026-01");
  });
  it.each([{ readOnly: true }, { reportOnly: true }])("retains existing entry guards: %j", (opts) => {
    renderWorkbench(opts);
    act(() => { requestChartFollowup({ title: "Quarterly results", dataArtifactId: reference.fileId }); });
    expect(chartReferences.read).not.toHaveBeenCalled();
  });
  it("aborts and fences a late metadata reply when the same mounted workbench changes session", async () => {
    let resolve!: (value: typeof reference) => void;
    chartReferences.read.mockReturnValue(new Promise((done) => { resolve = done; }));
    let switchSession!: (value: string) => void;
    function Host() {
      const [sessionId, setSessionId] = useState("s_1");
      switchSession = setSessionId;
      return <ChatWorkbench runner={{ sessionId, turnId: "t_1", messages: [], traceSteps: [], citations: [], steerAcks: [], artifacts: [], status: "idle" } as never} onSend={vi.fn()} readOnly={false} />;
    }
    renderIntl(<Host />);
    act(() => { requestChartFollowup({ title: "Quarterly results", dataArtifactId: reference.fileId }); });
    const signal = chartReferences.read.mock.calls[0][2] as AbortSignal;
    act(() => { switchSession("s_2"); });
    expect(signal.aborted).toBe(true);
    await act(async () => { resolve(reference); });
    expect(screen.queryByText(reference.filename)).not.toBeInTheDocument();
  });
});

// Results pane width (workbench measures .results-workspace-body with ResizeObserver): jsdom has no layout; assign widths by class.
let paneWidth = 0;
const originalRect = HTMLElement.prototype.getBoundingClientRect;
beforeEach(() => {
  HTMLElement.prototype.getBoundingClientRect = function rect(this: HTMLElement) {
    if (this.classList.contains("results-workspace-body")) {
      return { x: 0, y: 0, top: 0, left: 0, right: paneWidth, bottom: 800, width: paneWidth, height: 800, toJSON: () => ({}) } as DOMRect;
    }
    return originalRect.call(this);
  };
});
afterEach(() => {
  HTMLElement.prototype.getBoundingClientRect = originalRect;
});

describe("第 4b 批余项：写作中导出不可用、窄工具条、手机阅读工具条", () => {
  const exportButton = () => toolbar()!.querySelector<HTMLButtonElement>(".ws-export");
  const editHost = () => toolbar()!.querySelector(".results-report-edit-controls");

  it("allows a desktop reader to change and restore report width without editing the report", async () => {
    media.matches = false;
    renderWorkbench();
    await waitFor(() => expect(toolbar()).not.toBeNull());
    expect(reportViewProps.last?.readingWidth).toBe("standard");
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "缩小边距并加宽正文" }));
    await waitFor(() => expect(reportViewProps.last?.readingWidth).toBe("wide"));
    expect(window.localStorage.getItem("bowhead.report.reading-width")).toBe("wide");
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "恢复默认边距和正文宽度" }));
    await waitFor(() => expect(reportViewProps.last?.readingWidth).toBe("standard"));
  });

  it("export is disabled while writing with the reason on line two (aria-describedby); it returns when writing ends", async () => {
    media.matches = false;
    canvasState.status = "streaming";
    renderWorkbench();
    await waitFor(() => expect(exportButton()).not.toBeNull());
    expect(exportButton()).toBeDisabled();
    const hint = toolbar()!.querySelector(".results-report-writing-hint")!;
    expect(hint).toHaveTextContent("写作完成后可以编辑和导出");
    expect(exportButton()!.getAttribute("aria-describedby")).toBe(hint.id);
    cleanup();
    canvasState.status = "done";
    renderWorkbench();
    await waitFor(() => expect(exportButton()).not.toBeNull());
    expect(exportButton()).not.toBeDisabled();
    expect(exportButton()!.hasAttribute("aria-describedby")).toBe(false);
    expect(toolbar()!.querySelector(".results-report-writing-hint")).toBeNull();
  });

  it("below a 560 pane width export moves into the More submenu; while writing that item is disabled with a reason", async () => {
    media.matches = false;
    paneWidth = 500;
    renderWorkbench();
    await waitFor(() => expect(toolbar()).not.toBeNull());
    await waitFor(() => expect(exportButton()).toBeNull());
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    const exportItem = await screen.findByRole("menuitem", { name: "导出" });
    fireEvent.click(exportItem);
    expect(await screen.findByRole("menuitem", { name: /PDF/ })).toBeTruthy();
    expect(await screen.findByRole("menuitem", { name: /导出记录/ })).toBeTruthy();
    cleanup();
    canvasState.status = "streaming";
    renderWorkbench();
    await waitFor(() => expect(toolbar()).not.toBeNull());
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    const disabled = await screen.findByRole("menuitem", { name: "导出" });
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    expect(document.getElementById(disabled.getAttribute("aria-describedby")!)).toHaveTextContent("报告写完后可以导出");
  });

  it("phone reading (<= 640, not editing, not writing): the edit group leaves the toolbar and Edit this report in More enters edit mode", async () => {
    media.matches = true;
    const enter = vi.fn();
    reportViewProps.enterEditMode = enter;
    renderWorkbench();
    await waitFor(() => expect(toolbar()).not.toBeNull());
    expect(toolbar()).toHaveAttribute("data-phone-reading", "true");
    expect(editHost()).toBeNull();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "编辑此报告" }));
    expect(enter).toHaveBeenCalledTimes(1);
    // ReportView reports entering edit mode: edit group returns to the toolbar (「完成」lives there).
    const onEditModeChange = reportViewProps.last!.onEditModeChange as (editing: boolean) => void;
    act(() => onEditModeChange(true));
    await waitFor(() => expect(editHost()).not.toBeNull());
    expect(toolbar()).not.toHaveAttribute("data-phone-reading");
  });

  it("phone reading merges the results header and the report toolbar into one bar; Back returns to the results list and splits them again", async () => {
    media.matches = true;
    renderWorkbench();
    await waitFor(() => expect(toolbar()).not.toBeNull());
    const pane = document.querySelector(".v2-ws-right")!;
    expect(pane).toHaveAttribute("data-phone-reading-bar", "true");
    const back = toolbar()!.querySelector<HTMLButtonElement>(".results-report-phone-back")!;
    expect(back).toHaveAttribute("aria-label", "返回成果列表");
    const title = toolbar()!.querySelector<HTMLElement>(".results-report-phone-title")!;
    expect(title.textContent).toBeTruthy();
    expect(title.getAttribute("title")).toBe(title.textContent);
    // Header no longer duplicates Back/title (the same refs have a single host).
    expect(document.querySelector(".v2-ws-right-head .results-mobile-back")).toBeNull();
    expect(document.querySelector(".v2-ws-right-head .results-current-title")).toBeNull();
    // Order: Back → title → status group → Share / More.
    const order = Array.from(toolbar()!.children).map((el) => el.className);
    expect(order.indexOf("results-mobile-back results-report-phone-back")).toBeLessThan(order.indexOf("results-current-title results-report-phone-title"));
    expect(order.indexOf("results-current-title results-report-phone-title")).toBeLessThan(order.indexOf("results-report-status-group"));
    fireEvent.click(back);
    await waitFor(() => expect(pane).not.toHaveAttribute("data-phone-reading-bar"));
    expect(document.querySelector(".results-report-phone-back")).toBeNull();
  });

  it("scale navigation shares the compact reading bar and keeps History in More", async () => {
    caps.scaleNav = true;
    renderWorkbench();
    await waitFor(() => expect(document.querySelector(".v2-ws-right")).toHaveAttribute("data-phone-reading-bar", "true"));
    expect(reportViewProps.last?.hideHistoryControl).toBe(true);
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "历史" }));
    expect(reportViewProps.openHistory).toHaveBeenCalled();
    fireEvent.click(document.querySelector(".results-report-phone-back")!);
    await waitFor(() => expect(screen.getByRole("dialog", { name: "成果" })).toBeInTheDocument());
  });

  it("phone reading: read-only accounts and shared read-only views have no Edit this report in More", async () => {
    renderWorkbench({ readOnly: true });
    await waitFor(() => expect(toolbar()).not.toBeNull());
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    await screen.findByRole("menuitem", { name: "复制权限链接" });
    expect(screen.queryByRole("menuitem", { name: "编辑此报告" })).toBeNull();
    cleanup();
    renderWorkbench({ reportOnly: true });
    await waitFor(() => expect(toolbar()).not.toBeNull());
    expect(screen.queryByRole("button", { name: "更多操作" })).toBeNull();
  });

  it("wide screen: the edit group stays in the toolbar and More has no Edit this report", async () => {
    media.matches = false;
    renderWorkbench();
    await waitFor(() => expect(editHost()).not.toBeNull());
    expect(toolbar()).not.toHaveAttribute("data-phone-reading");
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    await screen.findByRole("menuitem", { name: "复制权限链接" });
    expect(screen.queryByRole("menuitem", { name: "编辑此报告" })).toBeNull();
  });

  it.each([
    [{ streamingState: "error", running: true }, "reconnecting"],
    [{ streamingState: "error", running: false, terminalStatus: null }, "lost"],
    // Existing terminal state (including runner's failed after an unreadable terminal frame) is not a connection loss; terminal explanation handles it (same as F5).
    [{ streamingState: "error", running: false, terminalStatus: "failed" }, null],
    [{ streamingState: "paused", running: true }, null],
  ])("runner %o => ReportView streamConnection = %s and receives the turn id", async (runner, expected) => {
    renderWorkbench({ runner });
    await waitFor(() => expect(reportViewProps.last).not.toBeNull());
    expect(reportViewProps.last!.streamConnection).toBe(expected);
    expect(reportViewProps.last!.turnTerminalTurnId).toBe("t_1");
  });

  it.each([
    [3, 3],
    [undefined, null],
  ])("runner.reportSectionsSaved=%s => ReportView receives %s", async (saved, expected) => {
    renderWorkbench({ runner: saved === undefined ? {} : { reportSectionsSaved: saved } });
    await waitFor(() => expect(reportViewProps.last).not.toBeNull());
    expect(reportViewProps.last!.turnReportSectionsSaved).toBe(expected);
  });
});

describe("report toolbar share and status", () => {
  it("有「分享」图标按钮，点开是这份报告的分享弹窗，回焦目标是这个按钮；「复制权限链接」仍在「更多」", async () => {
    renderWorkbench();
    const share = await screen.findByRole("button", { name: "分享报告" });
    expect(toolbar()!.contains(share)).toBe(true);
    expect(share.getAttribute("aria-haspopup")).toBe("dialog");
    fireEvent.click(share);
    const dialog = await screen.findByRole("dialog", { name: "share-dialog" });
    expect(dialog.getAttribute("data-kind")).toBe("report");
    expect(dialog.getAttribute("data-resource")).toBe("s_1");
    // Share dialog handles return focus after closing (returnFocusRef points to this button; Safari clicks do not focus buttons).
    expect(dialog.getAttribute("data-return-focus")).toBe("分享报告");
    fireEvent.click(screen.getByRole("button", { name: "close-share" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "share-dialog" })).toBeNull());
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    expect(await screen.findByRole("menuitem", { name: "复制权限链接" })).toBeTruthy();
  });

  it("工具条有状态组，ReportView 拿到它作状态组宿主", async () => {
    renderWorkbench();
    await waitFor(() => expect(toolbar()!.querySelector(".results-report-status-group")).not.toBeNull());
    await waitFor(() => expect(reportViewProps.last?.statusHost).toBe(toolbar()!.querySelector(".results-report-status-group")));
    // Writing-completion announcement uses only this turn's own terminal state (review N1): workbench passes runner terminalStatus to ReportView.
    expect(reportViewProps.last?.turnTerminalStatus).toBe("aborted");
  });

  it("只读账号：没有分享按钮（审查 #3）", async () => {
    renderWorkbench({ readOnly: true });
    await waitFor(() => expect(toolbar()).not.toBeNull());
    expect(screen.queryByRole("button", { name: "分享报告" })).toBeNull();
  });

  it("写作中且有轮次 ID：「更多」里有「复制轮次 ID」，复制后菜单不关、原位写「已复制」；不在写作时没有这一项", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    canvasState.status = "streaming";
    canvasState.currentTurnId = "t_abcdef123456";
    renderWorkbench();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    const item = await screen.findByRole("menuitem", { name: "复制轮次 ID" });
    fireEvent.click(item);
    expect(writeText).toHaveBeenCalledWith("t_abcdef123456");
    expect(await screen.findByRole("menuitem", { name: "已复制" })).toBeTruthy();
    expect(screen.getByRole("menu")).toBeTruthy();
    cleanup();
    canvasState.status = "done";
    renderWorkbench();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    await screen.findByRole("menuitem", { name: "复制权限链接" });
    expect(screen.queryByRole("menuitem", { name: "复制轮次 ID" })).toBeNull();
  });

  it("「复制轮次 ID」：浏览器没有 navigator.clipboard 时走兜底（弹出可手动复制的对话框）；兜底也没复制成功时写「复制失败」", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(null);
    canvasState.status = "streaming";
    canvasState.currentTurnId = "t_abcdef123456";
    renderWorkbench();
    fireEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    fireEvent.click(await screen.findByRole("menuitem", { name: "复制轮次 ID" }));
    await waitFor(() => expect(prompt).toHaveBeenCalledWith("复制轮次 ID", "t_abcdef123456"));
    expect(await screen.findByRole("menuitem", { name: "复制失败" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "已复制" })).toBeNull();
    prompt.mockRestore();
  });

  it("分享只读视图：没有分享按钮（与「复制权限链接」同一可见条件）", async () => {
    renderWorkbench({ reportOnly: true });
    await waitFor(() => expect(toolbar()).not.toBeNull());
    expect(screen.queryByRole("button", { name: "分享报告" })).toBeNull();
  });
});
