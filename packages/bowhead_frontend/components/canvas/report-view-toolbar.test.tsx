/**
 * Chat workbench and report canvas batch 4b: wiring ReportView to the results toolbar's status group.
 *   · Flag on with a status group: remove the streaming banner, put writing progress in the group; when not writing, show the status badge there instead of the report header;
 *     read-only / shared-view badges are not menus; do not dim the whole body during writing (aria-busy announces generation), but still dim during AI rewriting.
 *   · Flag on without a status group (fixture pages, archived nested views): retain banner and header-badge positions.
 *   · Flag off: original behavior (banner, header badge, inline body opacity 0.65).
 */
import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    // A non-default status: the toolbar hides the default (active) chip, and these tests place the chip.
    fetchCanvasStatus: vi.fn(async () => ({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 })),
    setCanvasStatus: vi.fn(),
  };
});

import { ReportView, type ReportViewHandle } from "./report-view";

vi.stubGlobal("matchMedia", (query: string) => ({
  matches: query.includes("hover: hover") || query.includes("pointer: fine"),
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

const SECTIONS = [
  { id: "sec_h1", kind: "h1", text: "华东 Q3 下滑归因" },
  { id: "sec_n1", kind: "narrative", markdown: "华东区 GMV 环比下降 8.4%。" },
] as unknown as ReportSection[];

let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  host.className = "results-report-status-group";
  document.body.appendChild(host);
});
afterEach(() => {
  cleanup();
  host.remove();
});

const article = () => document.querySelector<HTMLElement>("article.canvas-report-article");

describe("with a toolbar status group", () => {
  it("写作中：没有吸顶横幅，写作进度在状态组；正文不整体变暗、aria-busy", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" isStreaming statusHost={host} />);
    await waitFor(() => expect(host.querySelector("[data-testid='report-toolbar-streaming']")).not.toBeNull());
    expect(document.querySelector(".rc-stream-banner")).toBeNull();
    await waitFor(() => expect(article()).not.toBeNull());
    expect(article()).toHaveAttribute("aria-busy", "true");
    expect(article()!.getAttribute("style")).toBeNull();
    expect(article()).toHaveAttribute("data-streaming", "on");
  });

  it("不在写作：状态徽标在状态组，报告头部只剩「历史」", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit statusHost={host} />);
    await waitFor(() => expect(host.querySelector(".rc-status-trigger")).not.toBeNull());
    const meta = document.querySelector(".rc-report-header-meta")!;
    expect(meta.querySelector(".rc-status-control")).toBeNull();
    expect(meta.querySelector(".canvas-report-history-btn")).not.toBeNull();
    expect(article()!.hasAttribute("aria-busy")).toBe(false);
  });

  it("只读 / 分享视图：状态组里只显示状态，不是改状态的菜单", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" canEdit={false} statusHost={host} />);
    await waitFor(() => expect(host.querySelector("[data-testid='report-status-static']")).not.toBeNull());
    expect(host.querySelector(".rc-status-trigger")).toBeNull();
    expect(screen.queryByRole("button", { name: "更改状态" })).toBeNull();
  });

  it("状态组还没挂上（null）时两处都不渲染，不回落到横幅", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" isStreaming statusHost={null} />);
    await waitFor(() => expect(article()).not.toBeNull());
    expect(document.querySelector(".rc-stream-banner")).toBeNull();
  });
});

describe("第 4b 批余项接线：写作的暂停与中断、手机阅读的「编辑」入口", () => {
  const TURN_SECTIONS = [
    { id: "sec_h1", kind: "h1", text: "华东 Q3 下滑归因", turn_id: "t_old" },
    { id: "sec_n1", kind: "narrative", markdown: "第一轮写的。", turn_id: "t_old" },
    { id: "sec_n2", kind: "narrative", markdown: "被停止那一轮写的。", turn_id: "t_stop" },
  ] as unknown as ReportSection[];

  it.each([
    ["aborted", "t_stop", "已停止，已写的部分已保存"],
    ["failed", "t_stop", "写作中断，已写的部分已保存"],
  ])("turn outcome %s on a report this turn wrote ⇒ the status group keeps a note", async (status, turn, text) => {
    renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} turnTerminalStatus={status} turnTerminalTurnId={turn} />,
    );
    await waitFor(() => expect(host.querySelector("[data-testid='report-toolbar-outcome']")).not.toBeNull());
    expect(host.querySelector("[data-testid='report-toolbar-outcome']")).toHaveTextContent(text);
  });

  it("the saved section count of this turn reaches the note; a revision turn (0 sections, nothing new this turn) shows no note", async () => {
    const { unmount } = renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} turnTerminalStatus="aborted" turnTerminalTurnId="t_stop" turnReportSectionsSaved={2} />,
    );
    await waitFor(() => expect(host.querySelector("[data-testid='report-toolbar-outcome']")).toHaveTextContent("已停止，已保存 2 节"));
    unmount();
    renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} turnTerminalStatus="aborted" turnTerminalTurnId="t_revise" turnReportSectionsSaved={0} />,
    );
    await waitFor(() => expect(host.querySelector(".rc-status-trigger")).not.toBeNull());
    expect(host.querySelector("[data-testid='report-toolbar-outcome']")).toBeNull();
  });

  it.each([
    ["被停止的一轮没写过这份报告", "aborted", "t_chat_only"],
    ["本轮正常完成", "completed", "t_stop"],
    ["不知道是哪一轮", "aborted", null],
  ])("%s ⇒ no note", async (_name, status, turn) => {
    renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} turnTerminalStatus={status} turnTerminalTurnId={turn} />,
    );
    await waitFor(() => expect(host.querySelector(".rc-status-trigger")).not.toBeNull());
    expect(host.querySelector("[data-testid='report-toolbar-outcome']")).toBeNull();
  });

  it("connection lost (reconnect given up) ⇒ the status group shows the lost-connection note and Refresh report", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" isStreaming statusHost={host} streamConnection="lost" />);
    await waitFor(() => expect(host.querySelector("[data-outcome='lost']")).not.toBeNull());
    expect(host.querySelector("[data-testid='report-toolbar-streaming']")).toBeNull();
    expect(screen.getByRole("button", { name: "刷新报告" })).toBeInTheDocument();
  });

  it("a lost connection attaches to the report only while it is writing or when this turn wrote it (same rule as the interruption note)", async () => {
    const { unmount } = renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} streamConnection="lost" turnTerminalTurnId="t_chat_only" />,
    );
    await waitFor(() => expect(host.querySelector(".rc-status-trigger")).not.toBeNull());
    expect(host.querySelector("[data-outcome='lost']")).toBeNull();
    unmount();
    renderIntl(
      <ReportView sections={TURN_SECTIONS} canvasId="cv_1" canEdit statusHost={host} streamConnection="lost" turnTerminalTurnId="t_stop" />,
    );
    await waitFor(() => expect(host.querySelector("[data-outcome='lost']")).not.toBeNull());
  });

  it("reconnecting ⇒ the writing progress turns into a pause marker", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" isStreaming statusHost={host} streamConnection="reconnecting" />);
    await waitFor(() => expect(host.querySelector("[data-state='paused']")).not.toBeNull());
  });

  it("enterEditMode 与点「编辑此报告」同一条路，并经 onEditModeChange 告诉调用方；不能编辑时什么都不做", async () => {
    const ref = createRef<ReportViewHandle>();
    const onEditModeChange = vi.fn();
    renderIntl(<ReportView ref={ref} sections={SECTIONS} canvasId="cv_1" canEdit onEditModeChange={onEditModeChange} />);
    await waitFor(() => expect(onEditModeChange).toHaveBeenLastCalledWith(false));
    act(() => ref.current!.enterEditMode());
    await waitFor(() => expect(onEditModeChange).toHaveBeenLastCalledWith(true));
    expect(document.querySelector(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "on");
    cleanup();
    const onChange2 = vi.fn();
    renderIntl(<ReportView ref={ref} sections={SECTIONS} canvasId="cv_1" canEdit={false} onEditModeChange={onChange2} />);
    act(() => ref.current!.enterEditMode());
    await waitFor(() => expect(document.querySelector(".canvas-report-shell")).toHaveAttribute("data-edit-mode", "off"));
    expect(onChange2).not.toHaveBeenCalledWith(true);
  });
});

describe("without a status group (fixture pages, archived nested views)", () => {
  it("横幅与头部徽标原位", async () => {
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" isStreaming />);
    await waitFor(() => expect(document.querySelector(".rc-stream-banner")).not.toBeNull());
    await waitFor(() => expect(document.querySelector(".rc-report-header-meta .rc-status-control")).not.toBeNull());
  });

  it("hides the default active status in the header too, so the report ⋯ is the only place for its actions", async () => {
    const api = await import("@/lib/api");
    vi.mocked(api.fetchCanvasStatus).mockResolvedValue({ status: "active", effectiveStatus: "active", quarantinedCount: 0 } as never);
    renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" />);
    await waitFor(() => expect(api.fetchCanvasStatus).toHaveBeenCalled());
    await waitFor(() => expect(document.querySelector(".rc-report-header-meta")).not.toBeNull());
    expect(document.querySelector(".rc-report-header-meta .rc-status-control")).toBeNull();
    vi.mocked(api.fetchCanvasStatus).mockResolvedValue({ status: "verified", effectiveStatus: "verified", quarantinedCount: 0 } as never);
  });
});

describe("section menu wiring: Split here is disabled by the section holding the editor cursor", () => {
  const TWO = [
    { id: "sec_n1", kind: "narrative", markdown: "第一段正文。" },
    { id: "sec_n2", kind: "narrative", markdown: "第二段正文。" },
  ] as unknown as ReportSection[];

  it("光标在第一节：第一节的「在此处分割」可用，第二节的禁用并写出原因", async () => {
    renderIntl(<ReportView sections={TWO} canvasId="cv_1" canEdit isEditMode />);
    const triggers = await screen.findAllByRole("button", { name: "操作菜单 (narrative)" });
    expect(triggers.length).toBeGreaterThanOrEqual(2);
    // The editor's initial cursor is at the document start (inside the first section)
    fireEvent.click(triggers[1]);
    const disabled = await screen.findByRole("menuitem", { name: "在此处分割" });
    expect(disabled).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(disabled, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    fireEvent.click(triggers[0]);
    const enabled = await screen.findByRole("menuitem", { name: "在此处分割" });
    expect(enabled).not.toHaveAttribute("aria-disabled");
  });
});

describe("report content language (P2 batch 6)", () => {
  const ENGLISH = [
    { id: "sec_h1", kind: "h1", text: "East region Q3 decline" },
    { id: "sec_n1", kind: "narrative", markdown: "GMV in the East region fell 8.4% quarter over quarter." },
  ] as unknown as ReportSection[];

  it("marks a Chinese report with lang=zh and leaves Latin reports to the UI language", async () => {
    const { unmount } = renderIntl(<ReportView sections={SECTIONS} canvasId="cv_1" statusHost={host} />);
    await waitFor(() => expect(article()).toHaveAttribute("lang", "zh"));
    unmount();
    renderIntl(<ReportView sections={ENGLISH} canvasId="cv_1" statusHost={host} />);
    await waitFor(() => expect(article()).not.toBeNull());
    expect(article()).not.toHaveAttribute("lang");
  });
});
