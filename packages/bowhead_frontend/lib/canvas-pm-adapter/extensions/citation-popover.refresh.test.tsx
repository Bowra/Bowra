/**
 * Chat workbench and report canvas batch 4b: citation-source cards (body-mounted citation-popover)
 * use token-based layering and a dialog role/name; keyboard opening moves focus into the card, Esc closes and returns to the citation; narrow-screen clicks use a bottom sheet
 * (hover still uses a popover).
 */
import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { SourcePanel, SourcePanelProvider } from "@/components/canvas/source-panel";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import type { ReportSection } from "@/lib/types";
import { CanvasContextProvider } from "../canvas-context";
import { CitationHoverManager, POPOVER_OPEN_DELAY_MS } from "./citation-popover";
/** D13: hover previews open after a short dwell (WCAG 1.4.13). */
async function dwell() {
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, POPOVER_OPEN_DELAY_MS + 20)); });
}


vi.mock("../kb-chunk-cache", () => ({
  useKbChunkContent: (fileId?: string) =>
    fileId
      ? {
          status: "ready",
          content: "第三季度华东区 42 家门店实现营业额 3.86 亿元，同比增长 7.3%。",
          chunk: { chunk_id: "chunk_a", page: 3, section_title: "", chunk_index: 1 },
        }
      : { status: "idle" },
  evictKbChunks: () => {},
  clearKbChunkCache: () => {},
}));

const A = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

let narrow = false;
vi.stubGlobal("matchMedia", (query: string) => ({
  matches: narrow && query.includes("max-width"),
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

afterEach(() => {
  cleanup();
  narrow = false;
});

function Harness() {
  const root = useRef<HTMLDivElement | null>(null);
  const sections = [
    {
      id: "sec_src",
      kind: "sources",
      label: "数据来源",
      entries: [
        {
          source_alias: A,
          access_state: "linked_accessible",
          generic_type: "document",
          ordinal: 1,
          safe_label: "2025Q3 华东区门店经营月报.md",
          kb_file_id: "kbf_1",
          kb_chunk_id: "chunk_a",
          kb_scope: "space",
          kb_space_id: "ws_kb",
        },
      ],
    },
  ] as unknown as ReportSection[];
  return (
    <SourceAccessRegistryProvider>
      <SourcePanelProvider>
        <CanvasContextProvider sections={sections}>
          <div ref={root}>
            <p>
              华东区三季度营业额同比增长 7.3%
              <sup data-citation-kind="numbered" data-source-idx="0" tabIndex={0}>[1]</sup>
            </p>
          </div>
          <CitationHoverManager rootRef={root} />
          <SourcePanel />
        </CanvasContextProvider>
      </SourcePanelProvider>
    </SourceAccessRegistryProvider>
  );
}

const card = () => document.querySelector<HTMLElement>(".citation-popover");

describe("角标来源卡", () => {
  it("悬停出现的卡按 --z-menu 排层、是带名称的对话框；Esc 关闭", async () => {
    renderIntl(<Harness />);
    fireEvent.mouseOver(screen.getByText("[1]"));
    // Sweeping over the badge does not open the card; staying on it does.
    expect(card()).toBeNull();
    await dwell();
    expect(card()).not.toBeNull();
    expect(card()!.style.zIndex).toBe("var(--z-menu)");
    expect(card()).toHaveAttribute("role", "dialog");
    expect(card()).toHaveAttribute("aria-label", "[1]");
    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(card()).toBeNull());
  });

  it("键盘（回车）打开：焦点进卡片；Esc 关闭并回到角标", async () => {
    renderIntl(<Harness />);
    const chip = screen.getByText("[1]");
    chip.focus();
    fireEvent.keyDown(chip, { key: "Enter" });
    await waitFor(() => expect(card()).not.toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(card()));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(card()).toBeNull());
    expect(document.activeElement).toBe(chip);
  });

  it("D13: the hover card stays while the pointer moves onto it and closes after a short grace", async () => {
    renderIntl(<Harness />);
    const chip = screen.getByText("[1]");
    fireEvent.mouseOver(chip);
    await dwell();
    fireEvent.mouseOut(chip, { relatedTarget: card() });
    fireEvent.mouseEnter(card()!);
    await dwell();
    expect(card()).not.toBeNull();
    fireEvent.mouseLeave(card()!);
    expect(card()).not.toBeNull();
    await waitFor(() => expect(card()).toBeNull());
  });

  it("D13: keyboard focus opens the preview, Tab enters its link, Esc returns to the badge", async () => {
    renderIntl(<Harness />);
    const chip = screen.getByText("[1]");
    act(() => chip.focus());
    await waitFor(() => expect(card()).not.toBeNull());
    expect(document.activeElement).toBe(chip);
    fireEvent.keyDown(chip, { key: "Tab" });
    expect(card()!.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(card()).toBeNull());
    expect(document.activeElement).toBe(chip);
  });

  it("窄屏点开：改底部面板；悬停出现的仍是浮层", async () => {
    narrow = true;
    renderIntl(<Harness />);
    fireEvent.mouseOver(screen.getByText("[1]"));
    await dwell();
    expect(card()).not.toBeNull();
    expect(card()!.hasAttribute("data-dialog-presentation")).toBe(false);
    fireEvent.mouseOut(screen.getByText("[1]"));
    await waitFor(() => expect(card()).toBeNull());
    fireEvent.click(screen.getByText("[1]"));
    await waitFor(() => expect(card()).toHaveAttribute("data-dialog-presentation", "sheet"));
    expect(card()).toHaveClass("rc-prov--sheet");
    expect(screen.getByTestId("kb-cite-popover")).toHaveTextContent("2025Q3 华东区门店经营月报.md");
  });
});
