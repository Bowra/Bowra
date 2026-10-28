/**
 * Numbered "Run Code" citations: the hover card offers the run result, which opens the source
 * drawer on the Run Code view (structured output first, then the run's code, label and time).
 * The link is the code block's viewer `output_aliases`; old canvases without it keep the plain drawer.
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { SourcePanel, SourcePanelProvider } from "@/components/canvas/source-panel";
import { RUN_CODE_STDOUT_ALIGNED } from "@/lib/__fixtures__/source-content";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import type { ReportSection } from "@/lib/types";
import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";
import { CanvasContextProvider } from "../canvas-context";
import { CitationHoverManager } from "./citation-popover";

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const OTHER = "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
const CODE = "import pandas as pd\nprint(df.round(4).to_string(index=False))";

vi.stubGlobal("matchMedia", (query: string) => ({
  matches: false,
  media: query,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
}));

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ family: "text", mode: "text", text: RUN_CODE_STDOUT_ALIGNED }),
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
  }));
});

function sections(codeBlock: Record<string, unknown> | null): ReportSection[] {
  const out: Record<string, unknown>[] = [
    {
      id: "sec_src",
      kind: "sources",
      label: "数据来源",
      entries: [
        {
          source_alias: ALIAS,
          access_state: "linked_accessible",
          generic_type: "generated_artifact",
          ordinal: 1,
          safe_label: "Run Code: 利润率与退款率的月度环比变化",
          safe_query: "利润率与退款率的月度环比变化",
          safe_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x",
          safe_preview_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x/preview",
        },
      ],
    },
  ];
  if (codeBlock) out.push(codeBlock);
  return out as unknown as ReportSection[];
}

const LINKED_CODE_BLOCK = {
  id: "sec_code",
  kind: "code_block",
  call_id: "call_1",
  turn_id: "t_1",
  lang: "python",
  code: CODE,
  ran_at: "2026-10-01T12:48:40Z",
  output_artifact_ids: ["art_x"],
  output_aliases: [ALIAS],
};

function Harness({ codeBlock }: { codeBlock: Record<string, unknown> | null }) {
  const root = useRef<HTMLDivElement | null>(null);
  return (
    <SourceAccessRegistryProvider>
      <SourcePanelProvider>
        <CanvasContextProvider sections={sections(codeBlock)}>
          <div ref={root}>
            <p>
              利润率均值约 17.30%
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

async function hoverCard() {
  fireEvent.mouseOver(screen.getByText("[1]"));
  await waitFor(() => expect(screen.getByTestId("prov-data-table")).toBeInTheDocument());
  return document.querySelector<HTMLElement>(".citation-popover")!;
}

describe("Run Code citation: hover card", () => {
  it("offers the run result instead of a dead View code button", async () => {
    renderIntl(<Harness codeBlock={LINKED_CODE_BLOCK} />);
    const card = await hoverCard();
    expect(within(card).getByRole("button", { name: zhMessages.popover.view_run_result })).toBeEnabled();
    expect(within(card).queryByRole("button", { name: zhMessages.popover.view_code })).toBeNull();
  });

  it("old canvases without output aliases keep the card without an action", async () => {
    const { output_aliases: _dropped, ...legacy } = LINKED_CODE_BLOCK;
    renderIntl(<Harness codeBlock={legacy} />);
    const card = await hoverCard();
    // Only “View all in source panel” remains; no run or code action.
    expect(within(card).getAllByRole("button").map((button) => button.textContent)).toEqual([zhMessages.provenance.view_all_in_panel]);
  });

  it("a code block for another source is not linked", async () => {
    renderIntl(<Harness codeBlock={{ ...LINKED_CODE_BLOCK, output_aliases: [OTHER] }} />);
    const card = await hoverCard();
    expect(within(card).getAllByRole("button").map((button) => button.textContent)).toEqual([zhMessages.provenance.view_all_in_panel]);
  });
});

describe("Run Code citation: drawer", () => {
  it("opens on the run result: label and time, output table, then the code collapsed", async () => {
    renderIntl(<Harness codeBlock={LINKED_CODE_BLOCK} />);
    const card = await hoverCard();
    fireEvent.click(within(card).getByRole("button", { name: zhMessages.popover.view_run_result }));
    const dialog = await screen.findByRole("dialog");
    const meta = await within(dialog).findByTestId("source-panel-run-meta");
    expect(meta).toHaveTextContent(zhMessages.source_panel.run_result);
    // The run label is the drawer title; the run header does not repeat it.
    expect(dialog).toHaveAccessibleName("Run Code: 利润率与退款率的月度环比变化");
    expect(meta).not.toHaveTextContent("Run Code: 利润率与退款率的月度环比变化");
    expect(meta).toHaveTextContent(zhMessages.code_block.lang_label_python);
    expect(meta.textContent).toMatch(/运行于 .*2026/);
    const table = await within(dialog).findByTestId("source-panel-table");
    const code = within(dialog).getByTestId("source-panel-run-code");
    expect(code.tagName).toBe("DETAILS");
    expect(code).not.toHaveAttribute("open");
    expect(code.querySelector("code")?.textContent).toBe(CODE);
    // Output first, code after it.
    expect(meta.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(table.compareDocumentPosition(code) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(document.querySelector(".citation-popover")).toBeNull();
  });

  it("old canvases: clicking the citation still shows the structured output, without the code", async () => {
    const { output_aliases: _dropped, ...legacy } = LINKED_CODE_BLOCK;
    renderIntl(<Harness codeBlock={legacy} />);
    fireEvent.click(screen.getByText("[1]"));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByTestId("source-panel-table");
    expect(within(dialog).queryByTestId("source-panel-run-meta")).toBeNull();
    expect(within(dialog).queryByTestId("source-panel-run-code")).toBeNull();
  });

  it("follows the interface language", async () => {
    renderIntl(<Harness codeBlock={LINKED_CODE_BLOCK} />, { locale: "en-US" });
    fireEvent.click(screen.getByText("[1]"));
    const dialog = await screen.findByRole("dialog");
    const meta = await within(dialog).findByTestId("source-panel-run-meta");
    expect(meta).toHaveTextContent(enMessages.source_panel.run_result);
    expect(meta.textContent).toMatch(/Ran .*2026/);
    expect(within(dialog).getByText(enMessages.source_panel.run_code)).toBeInTheDocument();
  });
});
