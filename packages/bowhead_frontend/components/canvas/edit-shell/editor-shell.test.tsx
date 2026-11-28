import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { MessageSquareText, Type } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";
import { EditorPanel, EditorRail, EditorShell, EditorToolbar, EditorTopBar, type ShellPanelId } from "./editor-shell";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const noop = () => undefined;

function Provider({ children }: { children: ReactNode }) {
  return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{children}</NextIntlClientProvider>;
}

describe("EditorTopBar (canvas GBar)", () => {
  it("says the save state in words and offers discard, save and exit", () => {
    const onSave = vi.fn();
    const onClose = vi.fn();
    renderIntl(
      <EditorTopBar kind="deck" filename="季度复盘.pptx" status={{ tone: "warning", icon: "dirty", text: "编辑中 · 基于第 3 版 · 2 处未保存", extra: "保存后不再保留第 1 版" }}
        discard={{ label: "放弃修改", onClick: noop }} primary={{ label: "保存为新版本", onClick: onSave }}
        onExit={noop} onCloseFullView={onClose} />,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("2 处未保存");
    expect(status).toHaveTextContent("保存后不再保留第 1 版");
    expect(status).toHaveAttribute("data-tone", "warning");
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    expect(onSave).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "关闭全屏" }));
    expect(onClose).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "退出编辑" })).toBeInTheDocument();
  });
});

describe("EditorToolbar (canvas GTools)", () => {
  const base = {
    undo: { label: "撤销（⌘Z）", onClick: noop, disabled: true },
    redo: { label: "重做（⇧⌘Z）", onClick: noop, disabled: true },
    zoom: "fit" as const,
    onZoom: noop,
    pageLabel: "第 2 页",
  };

  it("names the page and its action when nothing is selected", () => {
    const onReference = vi.fn();
    renderIntl(<EditorToolbar {...base} actions={[{ key: "page", label: "引用这一页", ariaLabel: "引用这一页 · 第 2 页", icon: MessageSquareText, pressed: false, onClick: onReference }]} hint="点页面上的文字开始修改" />);
    const group = screen.getByRole("group", { name: "这一页" });
    expect(group).toHaveTextContent("第 2 页");
    fireEvent.click(screen.getByRole("button", { name: "引用这一页 · 第 2 页" }));
    expect(onReference).toHaveBeenCalled();
    expect(screen.getByText("点页面上的文字开始修改")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "上一页" })).toBeNull();
  });

  it("says what is selected and keeps the place and hint off narrow rows", () => {
    renderIntl(<EditorToolbar {...base} selection={{ icon: Type, kind: "表格", name: "Q1 分产品线销售额", where: "第 3 行 · 产品线" }}
      actions={[{ key: "undo", label: "撤销这格", onClick: noop }]} hint="Tab 下一格" />);
    expect(screen.getByRole("group", { name: "选中的内容：表格 Q1 分产品线销售额" })).toHaveTextContent("第 3 行 · 产品线");
    cleanup();
    renderIntl(<EditorToolbar {...base} compact selection={{ icon: Type, kind: "表格", name: "Q1 分产品线销售额", where: "第 3 行 · 产品线" }}
      actions={[]} hint="Tab 下一格" />);
    expect(screen.queryByText("第 3 行 · 产品线")).toBeNull();
    expect(screen.queryByText("Tab 下一格")).toBeNull();
  });

  it("offers the pages when the strip is hidden", () => {
    const onNext = vi.fn();
    renderIntl(<EditorToolbar {...base} actions={[]} pages={{ page: 1, count: 6, onPrevious: noop, onNext, onShowStrip: noop }} />);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onNext).toHaveBeenCalled();
    expect(screen.getByText("1 / 6")).toBeInTheDocument();
  });
});

describe("EditorRail and EditorPanel (canvas GRail, GPanel*)", () => {
  function Harness({ onOuterKey }: { onOuterKey: () => void }) {
    const [open, setOpen] = useState<ShellPanelId | null>(null);
    return (
      <div onKeyDown={(event) => { if (event.key === "Escape") onOuterKey(); }}>
        <EditorRail active={open} changes={3} conflict={false} itemsTitle="本页可修改的内容" onToggle={(id) => setOpen((current) => current === id ? null : id)} />
        {open ? <EditorPanel id={open} title="修改记录" onClose={() => setOpen(null)}><p>rows</p></EditorPanel> : null}
      </div>
    );
  }

  it("opens one panel at a time, moves focus to its title and closes on Esc without leaving the editor", () => {
    const onOuterKey = vi.fn();
    renderIntl(<Harness onOuterKey={onOuterKey} />);
    const changes = screen.getByRole("button", { name: "修改记录，3 处未保存" });
    expect(changes).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(changes);
    expect(changes).toHaveAttribute("aria-expanded", "true");
    const title = screen.getByRole("heading", { name: "修改记录" });
    expect(title).toHaveFocus();
    expect(screen.getByRole("complementary", { name: "修改记录" })).toBeInTheDocument();
    fireEvent.keyDown(title, { key: "Escape" });
    expect(screen.queryByRole("complementary")).toBeNull();
    expect(onOuterKey).not.toHaveBeenCalled();
  });

  it("marks a conflict on the change count", () => {
    renderIntl(<EditorRail active={null} changes={2} conflict itemsTitle="x" onToggle={noop} />);
    expect(screen.getByRole("button", { name: "修改记录，2 处，有冲突要处理" })).toBeInTheDocument();
  });
});

describe("EditorShell layout", () => {
  function stubWidth(width: number) {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width, height: 800, top: 0, left: 0, right: width, bottom: 800, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
  }

  function Shell({ panel }: { panel: boolean }) {
    const [stripOpen, setStripOpen] = useState<boolean | null>(null);
    return (
      <EditorShell topBar={<header>bar</header>} rail={<nav>rail</nav>} stage={<main>stage</main>}
        strip={<nav aria-label="页面栏">strip</nav>} panel={panel ? <aside aria-label="panel">panel</aside> : null}
        stripOpen={stripOpen} onStripOpenChange={setStripOpen}
        toolbar={({ stripHidden }) => <div>{stripHidden ? <button type="button" onClick={() => setStripOpen(true)}>show pages</button> : "strip shown"}</div>} />
    );
  }

  it("hides the page strip while a panel is open below 1280 px, and lets the person bring it back", () => {
    stubWidth(1100);
    const { rerender } = renderIntl(<Shell panel={false} />);
    const wrap = (panel: boolean) => rerender(<Provider><Shell panel={panel} /></Provider>);
    expect(screen.getByRole("navigation", { name: "页面栏" })).toBeInTheDocument();
    wrap(true);
    expect(screen.queryByRole("navigation", { name: "页面栏" })).toBeNull();
    act(() => { fireEvent.click(screen.getByRole("button", { name: "show pages" })); });
    expect(screen.getByRole("navigation", { name: "页面栏" })).toBeInTheDocument();
    wrap(false);
    expect(screen.getByRole("navigation", { name: "页面栏" })).toBeInTheDocument();
  });

  it("keeps the page strip beside a panel on wide windows", () => {
    stubWidth(1440);
    renderIntl(<Shell panel />);
    expect(screen.getByRole("navigation", { name: "页面栏" })).toBeInTheDocument();
  });
});
