import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";
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
const B = "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";

vi.stubGlobal("matchMedia", (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

afterEach(cleanup);

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
        { source_alias: B, access_state: "linked_restricted", generic_type: "document", ordinal: 2 },
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
              ，上海贡献最大
              <sup data-citation-kind="numbered" data-source-idx="1" tabIndex={0}>[2]</sup>
            </p>
          </div>
          <CitationHoverManager rootRef={root} />
          <SourcePanel />
        </CanvasContextProvider>
      </SourcePanelProvider>
    </SourceAccessRegistryProvider>
  );
}

describe("report [N] citation on a knowledge-base source", () => {
  it("hover shows the knowledge-base card with file name, location, passage and deep link", async () => {
    renderIntl(<Harness />);
    fireEvent.mouseOver(screen.getByText("[1]"));
    await dwell();
    const card = screen.getByTestId("kb-cite-popover");
    expect(card).toHaveTextContent("2025Q3 华东区门店经营月报.md");
    expect(card).toHaveTextContent("知识库 · 空间 · 第 3 页");
    expect(card.querySelector("mark")?.textContent).toContain("同比增长 7.3%");
    expect(screen.getByText("在知识库中打开").closest("a")?.getAttribute("href")).toContain("chunk_id=chunk_a");
  });

  it("click opens the same knowledge-base card instead of the unavailable source panel", () => {
    renderIntl(<Harness />);
    fireEvent.click(screen.getByText("[1]"));
    const card = screen.getByTestId("kb-cite-popover");
    expect(card).toHaveTextContent("2025Q3 华东区门店经营月报.md");
    // The only dialog is the citation card itself, not the source panel.
    expect(screen.getAllByRole("dialog").every((dialog) => dialog.contains(card))).toBe(true);
  });

  it("restricted citation says only members with access can see it", async () => {
    renderIntl(<Harness />);
    fireEvent.mouseOver(screen.getByText("[2]"));
    await dwell();
    const card = screen.getByTestId("kb-cite-popover");
    expect(card).toHaveTextContent("来源受限");
    expect(card).toHaveTextContent("该来源仅对有权限的成员可见");
    expect(card.querySelector("a")).toBeNull();
  });
});
