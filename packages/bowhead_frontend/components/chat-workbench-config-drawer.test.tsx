/** ChatWorkbench × ConfigDrawer **seam** assertions (R2 review P2-7 closes the gap where two P1 issues escaped).
 *
 * Only ChatWorkbench knows hasCanvas for layout selection (P1-A):
 * - Canvas + desktop → overlay: scrim's parent must be canvas column (chat is never covered);
 * - No canvas + desktop → column: drawer in document flow, zero scrim, grid adds .with-config-drawer;
 * - <1200 → sheet.
 */

import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const canvasState = {
  sections: [] as unknown[],
  pyCells: [] as unknown[],
  sessionId: null as string | null,
  canvasId: null as string | null,
  // report-streaming B: real `CanvasState` always has this field (present in `initial`,
  // all hydration paths use `{...initial, ...}`). This mock omitted it,
  // crashing `selectStreamingSectionIdKey` on `.size`; **fix the shape, never add selector defenses**
  // that also hide genuine missing-store-field bugs.
  streamingSectionIds: new Set<string>(),
};
let desktop = true;
const resultsCapabilities = {
  workspace: true,
  completeList: false,
  scaleNav: false,
  resolved: true,
  error: null as null | "unavailable" | "timeout" | "auth",
};

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
let referenceInput = false;
// One stable session object: a fresh object per render would re-run every effect that depends on it.
const referenceSession = { me: { capabilities: { composer: { reference_input: true } } } };
vi.mock("@/lib/auth/auth-session-context", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth/auth-session-context")>()),
  useOptionalAuthSession: () => (referenceInput ? referenceSession : null),
}));
vi.mock("./canvas/data-tab-files", () => ({
  DataTabFiles: ({ focusArtifactId }: { focusArtifactId?: string | null }) => (
    <div data-testid="data-tab-files" data-focus={focusArtifactId ?? ""} />
  ),
  useArtifactItems: () => ({
    items: [],
    error: null,
    loading: false,
    retry: vi.fn(),
  }),
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

import { ChatWorkbench } from "./chat-workbench";

function renderWorkbench(
  drawerOpen: boolean,
  options: Parameters<typeof workbenchElement>[1] = {},
) {
  return renderIntl(workbenchElement(drawerOpen, options));
}

function workbenchElement(
  drawerOpen: boolean,
  options: {
    runner?: Record<string, unknown>;
    onCancel?: () => boolean | Promise<boolean>;
    onSteer?: (text: string) => boolean | Promise<boolean>;
    onReplay?: (turnId: string, text: string, attachments: unknown[]) => boolean | Promise<boolean>;
    replayRestoresDraft?: boolean;
    lastReferenceDocument?: unknown;
    creationPanel?: boolean;
    creationPanelTitle?: string;
    canvasObjectList?: React.ReactNode;
    resultsRevealRequest?: number;
  } = {},
) {
  // Minimal runner shape: only layout seams matter here; empty collections satisfy content surfaces.
  const runner = options.runner ?? {
    turnId: "t_1",
    sessionId: "s_1",
    started: true,
    messages: [],
    traceSteps: [],
    citations: [],
    steerAcks: [],
    artifacts: [],
    status: "idle",
  } as never;
  return (
    <ChatWorkbench
      runner={runner as never}
      onSend={vi.fn()}
      onCancel={options.onCancel ?? vi.fn()}
      onSteer={options.onSteer}
      onReplay={options.onReplay as never}
      replayRestoresDraft={options.replayRestoresDraft}
      lastReferenceDocument={options.lastReferenceDocument as never}
      readOnly={false}
      configDrawerOpen={drawerOpen}
      creationPanelOpen={options.creationPanel ?? false}
      creationPanelTitle={options.creationPanelTitle}
      canvasObjectList={options.canvasObjectList}
      resultsRevealRequest={options.resultsRevealRequest}
      creationPanel={options.creationPanel ? <section>创建智能体草稿</section> : null}
      configDrawer={(layout) => (
        <>
          {layout === "overlay" ? <div className="cc-drawer-scrim" data-testid="scrim" /> : null}
          <aside data-testid="config-drawer" data-layout={layout} />
        </>
      )}
    />
  );
}

beforeEach(() => {
  // Results navigation writes the browser URL; isolate each layout scenario.
  window.history.replaceState(null, "", "/");
  canvasState.sections = [];
  canvasState.canvasId = null;
  canvasState.sessionId = null;
  desktop = true;
  resultsCapabilities.workspace = true;
  // Local jsdom localStorage is a broken stub (getItem not a function); ChatWorkbench preference reads need it.
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
  // SplitPane (canvas branch) reads matchMedia.
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

describe("ChatWorkbench × ConfigDrawer 形态接缝", () => {
  it("有 canvas + 桌面 → overlay，且 scrim 挂在 canvas 列内（对话区永不被遮）", () => {
    canvasState.sections = [{ id: "sec_1", hidden: false }];
    const { container } = renderWorkbench(true);
    expect(screen.getByTestId("config-drawer").getAttribute("data-layout")).toBe("overlay");
    const scrim = screen.getByTestId("scrim");
    // Core P1-A assertion: scrim ancestor must be canvas column, never the entire workspace/main.
    expect(scrim.closest(".v2-ws-right")).toBeTruthy();
    expect(container.querySelector(".v2-workspace")?.contains(scrim)).toBe(true);
    expect(scrim.closest(".v2-ws-left, .v2-split-chat")).toBeNull();
  });

  it.each([
    ["成果工作区（生产形态）", true],
    ["旧画布（后端未开成果工作区）", false],
  ])("无 canvas + 桌面 → column：抽屉在文档流、零 scrim、grid 加 .with-config-drawer（%s）", (_label, workspace) => {
    resultsCapabilities.workspace = workspace;
    const { container } = renderWorkbench(true);
    expect(screen.getByTestId("config-drawer").getAttribute("data-layout")).toBe("column");
    expect(screen.queryByTestId("scrim")).toBeNull();
    expect(container.querySelector(".v2-workspace.with-config-drawer")).toBeTruthy();
    // Drawer is workspace's direct child (second grid/flex cell), not an overlay inside canvas column.
    expect(screen.getByTestId("config-drawer").parentElement?.className).toContain("v2-workspace");
    // Chat remains, neither replaced nor covered by drawer.
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.getByTestId("config-drawer").closest(".v2-ws-left, .v2-split-chat, .v2-ws-right")).toBeNull();
  });

  it("<1200 → sheet（移动/窄屏全屏）", () => {
    desktop = false;
    renderWorkbench(true);
    expect(screen.getByTestId("config-drawer").getAttribute("data-layout")).toBe("sheet");
  });

  it("抽屉未打开 → 零渲染、grid 类不变", () => {
    const { container } = renderWorkbench(false);
    expect(screen.queryByTestId("config-drawer")).toBeNull();
    expect(container.querySelector(".with-config-drawer")).toBeNull();
  });

  it("Stop 后恢复原问题时明确标记这是可编辑草稿", async () => {
    const onCancel = vi.fn(async () => true);
    renderWorkbench(false, {
      onCancel,
      runner: {
        turnId: "t_stop",
        sessionId: "s_stop",
        started: true,
        running: true,
        user: "原问题",
        messages: [],
        traceSteps: [],
        citations: [],
        steerAcks: [],
        artifacts: [],
        status: "running",
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value)
        .toBe("原问题");
      expect(screen.getByTestId("composer-state-note").textContent)
        .toContain("原问题已恢复");
    });
  });

  function failedRunner(user: string, userAttachments: unknown[] = []) {
    return {
      turnId: "t_failed",
      sessionId: "s_failed",
      started: true,
      running: false,
      user,
      userAttachments,
      errorReason: { source: "turn_failed", reasonCode: "lead_internal_error", recoveryAction: "retry" },
      messages: [],
      traceSteps: [],
      citations: [],
      steerAcks: [],
      artifacts: [],
      status: "error",
    };
  }
  const agentDocument = {
    schema_version: 1,
    nodes: [
      {
        type: "reference",
        node_id: "11111111-1111-4111-8111-111111111111",
        label: "Eddy",
        parameters: {},
        resource: { kind: "agent", source_kind: "system", id: "agent", version: "r1" },
      },
      { type: "text", text: " 帮我算一下" },
    ],
  };
  const note = () => screen.getByTestId("composer-state-note").textContent ?? "";
  /** D4: Retry lives only on the failure card. When Replay only restores the draft, the card offers it as Edit and send. */
  const cardRetry = async (restoresDraft: boolean) =>
    within(await screen.findByTestId("final-fail")).getByRole("button", { name: restoresDraft ? "编辑后发送" : "重试" });

  it("重放：服务端能重放时照常重放", async () => {
    const onReplay = vi.fn(async () => true);
    renderWorkbench(false, { onReplay, replayRestoresDraft: false, runner: failedRunner("算一下") });
    fireEvent.click(await cardRetry(false));
    await waitFor(() => expect(onReplay).toHaveBeenCalledWith("t_failed", "算一下", []));
  });

  it("重放：服务端不能重放时把原问题和附件放回输入框，不发请求", async () => {
    const onReplay = vi.fn(async () => true);
    const file = { fileId: "art_1", filename: "销售.csv", mime: "text/csv", size: 10 };
    renderWorkbench(false, { onReplay, replayRestoresDraft: true, runner: failedRunner("算一下", [file]) });
    const button = await cardRetry(true);
    const shownBefore = screen.queryAllByText("销售.csv").length;
    fireEvent.click(button);
    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("算一下");
      expect(screen.queryAllByText("销售.csv")).toHaveLength(shownBefore + 1);
      expect(note()).toContain("原问题已放回输入框，确认后重新发送");
    });
    expect(onReplay).not.toHaveBeenCalled();
  });

  it("重放：本页发出的 @ 引用按真实引用放回，不是只剩文字", async () => {
    referenceInput = true;
    try {
      renderWorkbench(false, {
        onReplay: vi.fn(async () => true),
        replayRestoresDraft: true,
        lastReferenceDocument: agentDocument,
        runner: failedRunner("@Eddy 帮我算一下"),
      });
      fireEvent.click(await cardRetry(true));
      await waitFor(() => {
        // Applied as a document (the fallback to text says "请重新选择"); the editor itself loads lazily.
        expect(note()).not.toContain("请重新选择");
        expect(document.querySelector("textarea")).toBeNull();
        expect(note()).toContain("原问题已放回输入框，确认后重新发送");
      });
    } finally {
      referenceInput = false;
    }
  });

  it.each(["@Eddy 帮我算一下", "/周报 本月汇总"])("重放：拿不回引用（%s）时明说要重新选择", async (question) => {
    renderWorkbench(false, { onReplay: vi.fn(async () => true), replayRestoresDraft: true, runner: failedRunner(question) });
    fireEvent.click(await cardRetry(true));
    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(question);
      expect(note()).toContain("请重新选择后发送");
    });
  });

  it("重放：引用放不进输入框时退回文字并明说要重新选择", async () => {
    // Reference input is off here, so the composer discards the document; the text must still come back.
    renderWorkbench(false, {
      onReplay: vi.fn(async () => true),
      replayRestoresDraft: true,
      lastReferenceDocument: agentDocument,
      runner: failedRunner("@Eddy 帮我算一下"),
    });
    fireEvent.click(await cardRetry(true));
    await waitFor(() => {
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("@Eddy 帮我算一下");
      expect(note()).toContain("请重新选择后发送");
    });
  });

  it("重放：只有附件的失败轮次也能放回", async () => {
    const file = { fileId: "art_2", filename: "图.png", mime: "image/png", size: 10 };
    renderWorkbench(false, { onReplay: vi.fn(async () => true), replayRestoresDraft: true, runner: failedRunner("", [file]) });
    const button = await cardRetry(true);
    const shownBefore = screen.queryAllByText("图.png").length;
    fireEvent.click(button);
    await waitFor(() => {
      expect(screen.queryAllByText("图.png")).toHaveLength(shownBefore + 1);
      expect(note()).toContain("原问题已放回输入框");
    });
  });

  it("Stop 不覆盖已有草稿，也不误报原问题已恢复", async () => {
    const onCancel = vi.fn(async () => true);
    renderWorkbench(false, {
      onCancel,
      onSteer: vi.fn(async () => true),
      runner: {
        turnId: "t_stop_existing",
        sessionId: "s_stop_existing",
        started: true,
        running: true,
        user: "原问题",
        messages: [],
        traceSteps: [],
        citations: [],
        steerAcks: [],
        artifacts: [],
        status: "running",
      },
    });

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "我正在写的草稿" } });
    fireEvent.click(screen.getByRole("button", { name: "停止" }));

    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(textarea.value).toBe("我正在写的草稿"));
    expect(screen.queryByTestId("composer-state-note")).toBeNull();
  });

  it("offers authorized document choices on manual Results entry while retaining unsent input", () => {
    const onOpen = vi.fn();
    const { container } = renderWorkbench(false, {
      resultsRevealRequest: 1,
      canvasObjectList: <button onClick={onOpen}>Review Quarterly</button>,
    });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Keep my next question" } });
    const chooser = screen.getByRole("region", { name: "选择成果" });
    expect(chooser.closest("[hidden]")).toBeNull();
    expect([...container.querySelectorAll(".v2-empty")].filter((element) => !element.closest("[hidden]"))).toEqual([]);
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Review Quarterly" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect((input as HTMLTextAreaElement).value).toBe("Keep my next question");
  });

  it("无报告时显式打开 sandbox artifact → 成果工作区打开该文件，不显示空报告", () => {
    const { container } = renderWorkbench(false);

    act(() => {
      window.dispatchEvent(new CustomEvent("bowhead:canvas-focus-data", {
        detail: { artifactId: "art_stdout" },
      }));
    });

    // Results workspace replaces Report/Data/SQL tabs with object surfaces; the currently visible surface must be this file.
    const files = screen.getByTestId("data-tab-files");
    expect(files.getAttribute("data-focus")).toBe("art_stdout");
    const artifactSurface = files.closest(".results-object-surface--artifact");
    expect(artifactSurface).toBeTruthy();
    expect(artifactSurface?.closest("[hidden]")).toBeNull();
    expect(container.querySelector(".v2-workspace.canvas-maxed")).toBeTruthy();
    // Empty-report placeholder may remain mounted only hidden; users must not see an empty report.
    const visibleEmpty = [...container.querySelectorAll(".v2-empty")]
      .filter((el) => el.closest("[hidden]") === null);
    expect(visibleEmpty).toEqual([]);
  });

  it("没有报告但用户打开了成果栏 → 抽屉浮在成果栏里，不挤成三栏", () => {
    const { container } = renderWorkbench(true);
    act(() => {
      window.dispatchEvent(new CustomEvent("bowhead:canvas-focus-data", {
        detail: { artifactId: "art_stdout" },
      }));
    });
    const drawer = screen.getByTestId("config-drawer");
    expect(drawer.getAttribute("data-layout")).toBe("overlay");
    expect(screen.getByTestId("scrim").closest(".v2-ws-right")).toBeTruthy();
    expect(container.querySelector(".v2-workspace.with-config-drawer")).toBeNull();
    expect(screen.getAllByTestId("config-drawer")).toHaveLength(1);
  });

  it("Results 中切换到 artifact 时保持同一个 Report 实例挂载", async () => {
    window.localStorage.setItem("bowhead.feature.results_workspace", "1");
    canvasState.canvasId = "cv_1";
    canvasState.sessionId = "s_1";
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    renderWorkbench(false);

    const report = await screen.findByTestId("report-view");
    act(() => {
      window.dispatchEvent(new CustomEvent("bowhead:canvas-focus-data", {
        detail: { artifactId: "art_1" },
      }));
    });

    await waitFor(() => expect(screen.getByTestId("data-tab-files")).toBeTruthy());
    expect(document.body.contains(report)).toBe(true);
    expect(report.closest(".results-object-surface")?.hasAttribute("hidden")).toBe(true);
  });

  it("无报告时把创建草稿放入右侧工作区，保留左侧 Chat", () => {
    const { container } = renderWorkbench(false, { creationPanel: true });
    const pane = screen.getByText("创建智能体草稿").closest("aside");
    expect(pane).toBeTruthy();
    expect(container.querySelector(".agent-creation-workspace .v2-ws-left")).toBeTruthy();
    expect(pane?.closest(".v2-split-canvas")).toBeTruthy();
  });
});

describe("mobile chat / results switch labels", () => {
  it("reveals an opened settings editor without letting report initialization select Chat", () => {
    desktop = false;
    renderWorkbench(false, { creationPanel: true, creationPanelTitle: "Space preferences" });
    expect(screen.getByRole("tab", { name: "Space preferences" }).getAttribute("aria-selected")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "对话" }));
    expect(screen.getByRole("tab", { name: "对话" }).getAttribute("aria-selected")).toBe("true");
  });

  function mobileTabLabels(): string[] {
    return Array.from(document.querySelectorAll(".v2-ws-mobile-tab"), (el) => el.textContent?.trim() ?? "");
  }

  it("names the restore edge after closing the right pane on the legacy canvas", async () => {
    resultsCapabilities.workspace = false;
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    renderWorkbench(false);
    fireEvent.click(screen.getByRole("button", { name: "关闭报告并返回对话独占视图" }));
    const edge = await waitFor(() => {
      const el = document.querySelector(".v2-ws-edge--canvas");
      expect(el).toBeTruthy();
      return el as HTMLElement;
    });
    await waitFor(() => expect(edge.getAttribute("aria-label")).toBe("重开成果"));
  });

  it("labels the legacy canvas tabs as chat / results", async () => {
    desktop = false;
    resultsCapabilities.workspace = false;
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    renderWorkbench(false);
    await waitFor(() => expect(mobileTabLabels()).toEqual(["对话", "成果"]));
  });
});

describe("mobile results new-content dot and the maximize shortcut", () => {
  const runningRunner = {
    turnId: "t_1",
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

  it("marks the mobile results tab when new results arrive on the chat page and clears it after viewing", async () => {
    desktop = false;
    resultsCapabilities.workspace = false;
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    renderWorkbench(false, { runner: runningRunner });
    const tab = await waitFor(() => {
      const el = screen.getByRole("tab", { name: /成果/ });
      expect(el.getAttribute("data-has-new")).toBe("1");
      return el;
    });
    expect(tab.getAttribute("aria-label")).toBe("成果，有新内容");
    expect(tab.querySelector(".v2-ws-new-dot")?.getAttribute("aria-hidden")).toBe("true");
    fireEvent.click(tab);
    await waitFor(() => expect(screen.getByRole("tab", { name: /成果/ }).hasAttribute("data-has-new")).toBe(false));
    fireEvent.click(screen.getByRole("tab", { name: "对话" }));
    expect(screen.getByRole("tab", { name: /成果/ }).hasAttribute("data-has-new")).toBe(false);
    expect(screen.getByRole("tab", { name: /成果/ }).hasAttribute("aria-label")).toBe(false);
  });

  const shortcut = { key: "|", code: "Backslash", metaKey: true, shiftKey: true };

  it("maximizes and restores the results pane with the desktop shortcut, and names it in the hint", async () => {
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    const { container } = renderWorkbench(false);
    await waitFor(() => expect(container.querySelector(".v2-split-divider")).not.toBeNull());
    fireEvent.keyDown(window, shortcut);
    await waitFor(() => expect(container.querySelector(".v2-workspace.canvas-maxed")).not.toBeNull());
    fireEvent.keyDown(window, { ...shortcut, metaKey: false, ctrlKey: true });
    await waitFor(() => expect(container.querySelector(".v2-workspace.canvas-maxed")).toBeNull());
    // ⌘\ without Shift belongs to the sidebar; do not handle it here.
    fireEvent.keyDown(window, { ...shortcut, key: "\\", shiftKey: false });
    expect(container.querySelector(".v2-workspace.canvas-maxed")).toBeNull();
    const maxBtn = screen.getByRole("button", { name: "放大报告（隐藏对话）" });
    // jsdom has no keyboard layout: hint specifies the US-layout \ key position.
    expect(maxBtn.getAttribute("title")).toMatch(/\((Ctrl\+Shift\+|⌘⇧)\\ · 美式键盘的 \\ 键位\)$/);
    expect(maxBtn.getAttribute("aria-keyshortcuts")).toMatch(/Shift\+\\$/);
  });

  it("uses the same key character in the hint and aria-keyshortcuts on non-US layouts", async () => {
    const nav = navigator as Navigator & { keyboard?: unknown };
    Object.defineProperty(nav, "keyboard", {
      configurable: true,
      value: { getLayoutMap: async () => new Map([["Backslash", "#"]]) },
    });
    try {
      canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
      const { container } = renderWorkbench(false);
      await waitFor(() => expect(container.querySelector(".v2-split-divider")).not.toBeNull());
      const maxBtn = screen.getByRole("button", { name: "放大报告（隐藏对话）" });
      await waitFor(() => expect(maxBtn.getAttribute("title")).toMatch(/\((Ctrl\+Shift\+|⌘⇧)#\)$/));
      expect(maxBtn.getAttribute("aria-keyshortcuts")).toMatch(/^(Meta|Control)\+Shift\+#$/);
    } finally {
      Reflect.deleteProperty(nav, "keyboard");
    }
  });

  it.each([
    ["modal dialog", { role: "dialog", "aria-modal": "true" }],
    ["non-modal popover", { role: "dialog" }],
    ["menu", { role: "menu" }],
    ["completion list", { role: "listbox" }],
  ])("yields the desktop shortcut while a %s is open and handles it again after it closes", async (_label, attrs) => {
    canvasState.sections = [{ id: "sec_1", kind: "narrative", text: "Report" }];
    const { container } = renderWorkbench(false);
    await waitFor(() => expect(container.querySelector(".v2-split-divider")).not.toBeNull());
    const overlay = document.createElement("div");
    for (const [k, v] of Object.entries(attrs)) overlay.setAttribute(k, v);
    document.body.appendChild(overlay);
    fireEvent.keyDown(window, shortcut);
    await act(async () => { await Promise.resolve(); });
    expect(container.querySelector(".v2-workspace.canvas-maxed")).toBeNull();
    overlay.remove();
    fireEvent.keyDown(window, shortcut);
    await waitFor(() => expect(container.querySelector(".v2-workspace.canvas-maxed")).not.toBeNull());
  });
});

describe("ChatWorkbench desktop layout switch", () => {
  it("keeps the composer input and focus when a settings page closes back to Results", async () => {
    window.history.replaceState(null, "", "/");
    const view = renderWorkbench(false, { creationPanel: true, creationPanelTitle: "Space preferences" });
    expect(screen.getByRole("complementary", { name: "Space preferences" })).toBeTruthy();
    const column = view.container.querySelector<HTMLElement>(".v2-ws-left")!;
    await waitFor(() => expect(column.querySelector("textarea")).not.toBeNull());
    const input = column.querySelector("textarea")!;
    input.focus();
    fireEvent.change(input, { target: { value: "Preserve my conversation input" } });
    view.rerender(<NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{workbenchElement(false)}</NextIntlClientProvider>);
    expect(view.container.querySelector(".v2-ws-left")).toBe(column);
    expect(view.container.querySelector(".v2-ws-left textarea")).toBe(input);
    expect(input.value).toBe("Preserve my conversation input");
    expect(document.activeElement).toBe(input);
  });

  it("keeps the chat column instance, draft, focus and scroll when the split layout falls back to the grid", async () => {
    // Opening a session renders the split layout while the results capability is unresolved; once it resolves
    // off, the workbench falls back to the chat-only grid. The chat column must survive that switch.
    resultsCapabilities.workspace = false;
    resultsCapabilities.resolved = false;
    // Earlier cases leave results/maximize state in the URL; start from a plain session page.
    window.history.replaceState(null, "", "/");
    try {
      const view = renderWorkbench(false);
      expect(view.container.querySelector(".v2-workspace.v2-split-flex")).not.toBeNull();
      const column = view.container.querySelector<HTMLElement>(".v2-ws-left")!;
      const scroller = view.container.querySelector<HTMLElement>(".v2-ws-left-body")!;
      await waitFor(() => expect(column.querySelector("textarea")).not.toBeNull());
      const textbox = column.querySelector("textarea")!;
      textbox.focus();
      fireEvent.change(textbox, { target: { value: "draft typed before the layout settles" } });
      scroller.scrollTop = 120;

      resultsCapabilities.resolved = true;
      view.rerender(
        <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
          {workbenchElement(false)}
        </NextIntlClientProvider>,
      );
      await waitFor(() => expect(view.container.querySelector(".v2-workspace.no-canvas")).not.toBeNull());

      expect(view.container.querySelector(".v2-ws-left")).toBe(column);
      expect(view.container.querySelector(".v2-ws-left-body")).toBe(scroller);
      expect(scroller.scrollTop).toBe(120);
      expect(view.container.querySelector(".v2-ws-left textarea")).toBe(textbox);
      expect(textbox.value).toBe("draft typed before the layout settles");
      expect(document.activeElement).toBe(textbox);
    } finally {
      resultsCapabilities.resolved = true;
    }
  });
});
