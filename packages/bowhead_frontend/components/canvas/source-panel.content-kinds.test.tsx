import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { RUN_CODE_STDOUT_ALIGNED, RUN_CODE_STDOUT_WITH_CSV } from "@/lib/__fixtures__/source-content";
import { CanvasContextProvider } from "@/lib/canvas-pm-adapter/canvas-context";
import { sourceRefFromNumberedEntry } from "@/lib/canvas-pm-adapter/extensions/citation-popover";
import type { SourceRef } from "@/lib/canvas-pm-adapter/citation-types";
import type { ReportSection } from "@/lib/types";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";
import { SourcePanel, SourcePanelProvider, useSourcePanel } from "./source-panel";

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mockMatchMedia() {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  });
}

function stubPreview(body: Record<string, unknown>) {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => body }));
}

/** Persisted projection of the `[1]` entry behind a sandbox whole-source citation. */
const RUN_CODE_ENTRY: Record<string, unknown> = {
  source_alias: ALIAS,
  access_state: "linked_accessible",
  generic_type: "generated_artifact",
  ordinal: 1,
  safe_label: "Run Code: 计算营业额/客流量月度环比",
  safe_query: "计算营业额/客流量月度环比",
  safe_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x",
  safe_preview_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x/preview",
};

function openPanel(
  sourceRef: SourceRef,
  opts: { locale?: "zh-CN" | "en-US"; sections?: ReportSection[] } = {},
) {
  function Opener() {
    const { open } = useSourcePanel();
    return (
      <button
        type="button"
        onClick={(event) => open({
          sourceRef,
          sourceQuality: "artifact_only",
          sourceIndex: 0,
          display: "[1]",
          value: "",
          unit: "",
          highlight: "",
        }, event.currentTarget)}
      >
        open
      </button>
    );
  }
  const view = renderIntl(
    <SourceAccessRegistryProvider>
      <CanvasContextProvider sections={opts.sections ?? []} sourceRefs={[]}>
        <SourcePanelProvider>
          <Opener />
          <SourcePanel />
        </SourcePanelProvider>
      </CanvasContextProvider>
    </SourceAccessRegistryProvider>,
    { locale: opts.locale },
  );
  fireEvent.click(screen.getByRole("button", { name: "open" }));
  return view;
}

function runCodeRef(entry: Record<string, unknown> = RUN_CODE_ENTRY): SourceRef {
  return sourceRefFromNumberedEntry(entry, 0).sourceRef as SourceRef;
}

describe("source drawer: Run Code stdout", () => {
  it("renders printed DataFrames and the CSV dump as tables, prose as preformatted text, caveat once", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: RUN_CODE_STDOUT_WITH_CSV });
    openPanel(runCodeRef());
    const dialog = screen.getByRole("dialog");
    const tables = await within(dialog).findAllByTestId("source-panel-table");
    expect(tables).toHaveLength(2);
    // Shared report table; source values keep their own decimals (trailing zeros trimmed); months stay labels.
    const firstRow = tables[0]!.querySelectorAll("tbody tr")[0]!;
    expect([...firstRow.querySelectorAll("td")].map((td) => td.textContent)).toEqual(["2026年1月", "18.25", "220.5"]);
    expect(within(tables[1]!).getByText("csv")).toBeInTheDocument();
    const text = within(dialog).getByTestId("source-panel-text");
    expect(text.tagName).toBe("PRE");
    expect(text.textContent).toBe("营业额 1月 18.25亿 3月 13.90亿 累计 -23.84%\n客流量 1月 220.50万 3月 190.75万 累计 -13.49%");
    expect(within(dialog).queryByTestId("source-panel-markdown")).toBeNull();
    const caveat = zhMessages.source_panel.artifact_level_summary;
    expect(dialog.textContent!.split(caveat)).toHaveLength(2);
    expect(dialog.textContent).toContain(RUN_CODE_ENTRY.safe_query as string);
  });

  it("shows missing cells as a dash and never multiplies sandbox values as percentages", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: RUN_CODE_STDOUT_ALIGNED });
    openPanel(runCodeRef());
    const table = await screen.findByTestId("source-panel-table");
    const rows = table.querySelectorAll("tbody tr");
    expect([...rows[0]!.querySelectorAll("td")].map((td) => td.textContent)).toEqual(["1", "0.1801", "0.1512", "—", "—"]);
    // Exact source decimals, no rounding and no ×100 percent guess from the `_pct` column name.
    expect(rows[1]!.querySelectorAll("td")[4]!.textContent).toBe("20.3704");
    expect(table.textContent).not.toContain("%");
  });

  it("old persisted shape (inline safe_preview stdout, id without source_alias) renders the same table", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: null });
    const legacy: Record<string, unknown> = { ...RUN_CODE_ENTRY, id: ALIAS, safe_preview: RUN_CODE_STDOUT_ALIGNED };
    delete legacy.source_alias;
    openPanel(runCodeRef(legacy));
    const table = await screen.findByTestId("source-panel-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(4);
  });
});

describe("source drawer: other content kinds", () => {
  it("markdown documents use the safe Markdown renderer", async () => {
    mockMatchMedia();
    stubPreview({ family: "document", mode: "text", text: "## 摘要\n\n- 门店 **42** 家\n- 营业额 3.86 亿元" });
    openPanel(runCodeRef({ ...RUN_CODE_ENTRY, generic_type: "document" }));
    const md = await screen.findByTestId("source-panel-markdown");
    expect(within(md).getByRole("heading", { name: "摘要" })).toBeInTheDocument();
    expect(screen.queryByTestId("source-panel-text")).toBeNull();
  });

  it("plain text keeps every line break in a preformatted block", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: "Loaded 2 files\nrows=8\ncolumns=['a', 'b']" });
    openPanel(runCodeRef());
    const pre = await screen.findByTestId("source-panel-text");
    expect(pre.tagName).toBe("PRE");
    expect(pre.textContent).toBe("Loaded 2 files\nrows=8\ncolumns=['a', 'b']");
  });

  it("structured rows from the preview endpoint render as a table, not tab-joined text", async () => {
    mockMatchMedia();
    stubPreview({ family: "table", mode: "rows", columns: ["region", "revenue"], rows: [{ region: "East", revenue: 42000 }], total_rows: 30 });
    openPanel(runCodeRef());
    const table = await screen.findByTestId("source-panel-table");
    expect([...table.querySelectorAll("tbody td")].map((td) => td.textContent)).toEqual(["East", "42,000"]);
    expect(table.textContent).toContain(zhMessages.source_panel.preview_rows_note.replace("{shown}", "1").replace("{total}", "30"));
  });

  it("image sources render the artifact inline instead of the API hint", async () => {
    mockMatchMedia();
    stubPreview({ family: "image", text: null, note: "use <img> to render" });
    openPanel(runCodeRef());
    const image = await screen.findByTestId("source-panel-image");
    expect(image.querySelector("img")?.getAttribute("src")).toBe(
      "/api/v1/canvas/by-session/s_x/artifacts/art_x?disposition=inline",
    );
    expect(screen.getByRole("dialog").textContent).not.toContain("use <img>");
  });

  it("charts in the report drawn from this source render with the chart renderer", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: "ok" });
    const sections = [
      {
        id: "sec_chart",
        kind: "chart",
        spec: {
          kind: "line",
          title: "退款率月度走势",
          columns: [
            { key: "月份", header: "月份", type: "categorical", role: "x" },
            { key: "退款率", header: "退款率", type: "numeric", role: "y" },
          ],
          rows: [{ 月份: "1月", 退款率: 0.15 }, { 月份: "2月", 退款率: 0.18 }],
          source_meta: { id: ALIAS, source_alias: ALIAS, access_state: "linked_accessible" },
        },
      },
      {
        id: "sec_other",
        kind: "chart",
        spec: { kind: "line", title: "Other", columns: [], rows: [], source_meta: { id: "source-ref-v1:other" } },
      },
    ] as unknown as ReportSection[];
    openPanel(runCodeRef(), { sections });
    const charts = await screen.findByTestId("source-panel-charts");
    expect(within(charts).getByText(zhMessages.source_panel.related_charts)).toBeInTheDocument();
    expect(within(charts).getByText("退款率月度走势")).toBeInTheDocument();
    expect(within(charts).queryByText("Other")).toBeNull();
  });
});

describe("source drawer language follows the viewer interface locale", () => {
  it("English interface: English chrome around Chinese source data", async () => {
    mockMatchMedia();
    stubPreview({ family: "text", mode: "text", text: RUN_CODE_STDOUT_WITH_CSV });
    openPanel(runCodeRef(), { locale: "en-US" });
    const dialog = screen.getByRole("dialog");
    await within(dialog).findAllByTestId("source-panel-table");
    expect(dialog.textContent).toContain(enMessages.source_panel.artifact_level_summary);
    expect(dialog.textContent).toContain(enMessages.source_panel.query_label);
    expect(dialog.textContent).not.toContain(zhMessages.source_panel.artifact_level_summary);
  });
});
