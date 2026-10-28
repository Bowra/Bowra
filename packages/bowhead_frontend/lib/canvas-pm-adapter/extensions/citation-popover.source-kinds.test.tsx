import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { RUN_CODE_STDOUT_ALIGNED, RUN_CODE_STDOUT_WITH_CSV } from "@/lib/__fixtures__/source-content";
import type { FilePreview } from "@/lib/api";
import zhMessages from "@/messages/zh-CN.json";
import enMessages from "@/messages/en-US.json";
import deMessages from "@/messages/de-DE.json";
import frMessages from "@/messages/fr-FR.json";
import { CanvasContextProvider } from "../canvas-context";
import type { CallIdLookupEntry } from "../call-id-lookup";
import type { SourceRef } from "../citation-types";
import { BiSourceCard, KbChunkPopoverContent, distinctSub, sourceRefFromNumberedEntry } from "./citation-popover";

vi.mock("../kb-chunk-cache", () => ({
  useKbChunkContent: () => ({ status: "idle" }),
  evictKbChunks: () => {},
  clearKbChunkCache: () => {},
}));

afterEach(cleanup);

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const ZH_DEFINITION = zhMessages.provenance.source_link_definition;
const EN_DEFINITION = enMessages.provenance.source_link_definition;

/** Projected shape of a numbered `[1]` entry for a sandbox ("Run Code") whole-source citation. */
const RUN_CODE_ENTRY = {
  source_alias: ALIAS,
  access_state: "linked_accessible",
  generic_type: "generated_artifact",
  ordinal: 1,
  safe_label: "Run Code: 利润率与退款率的月度环比变化及两序列相关性",
  safe_query: "利润率与退款率的月度环比变化及两序列相关性",
  safe_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x",
  safe_preview_url: "/api/v1/canvas/by-session/s_x/artifacts/art_x/preview",
};

function runCodeRef(): SourceRef {
  return sourceRefFromNumberedEntry(RUN_CODE_ENTRY, 0).sourceRef as SourceRef;
}

function stdoutPreview(text: string): FilePreview {
  return { family: "text", mode: "text", text, truncated: false };
}

function renderCard(
  ref: SourceRef,
  preview: FilePreview | null,
  opts: {
    locale?: "zh-CN" | "en-US" | "de-DE" | "fr-FR";
    sourceId?: string | null;
    lookup?: Map<string, CallIdLookupEntry>;
    currentTurnId?: string | null;
  } = {},
) {
  const root = document.createElement("div");
  const code = document.createElement("div");
  code.dataset.sectionId = "sec_code";
  code.scrollIntoView = vi.fn();
  root.appendChild(code);
  const view = renderIntl(
    <CanvasContextProvider sections={[]} currentTurnId={opts.currentTurnId ?? null}>
      <BiSourceCard
        ref_={ref}
        preview={preview}
        valueDisplay={null}
        sourceId={opts.sourceId ?? null}
        callIdLookup={opts.lookup ?? new Map()}
        rootRef={{ current: root }}
      />
    </CanvasContextProvider>,
    { locale: opts.locale },
  );
  return { ...view, code };
}

function countOccurrences(haystack: string, needle: string): number {
  return haystack.split(needle).length - 1;
}

describe("hover card for a Run Code (sandbox) source", () => {
  it("says the linkage caveat once, shows a table glance and no dead View code button", () => {
    const { container } = renderCard(runCodeRef(), stdoutPreview(RUN_CODE_STDOUT_ALIGNED));
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
    const table = screen.getByTestId("prov-data-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(4);
    expect(table.textContent).toContain("利润率_环比pp");
    // Missing cells read as a dash; numbers keep the printed decimals.
    expect([...table.querySelectorAll("tbody tr")[0]!.querySelectorAll("td")].map((td) => td.textContent))
      .toEqual(["1", "0.1801", "0.1512", "—", "—"]);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByTestId("citation-popover-2hop-footer")).toBeNull();
  });

  it("does not repeat the query under a label that already contains it", () => {
    const { container } = renderCard(runCodeRef(), stdoutPreview(RUN_CODE_STDOUT_ALIGNED));
    expect(container.querySelector(".rc-prov-head .t-sub")).toBeNull();
    expect(screen.getByText(RUN_CODE_ENTRY.safe_label)).toBeInTheDocument();
  });

  it("caps the glance at five rows and reports the real total", () => {
    renderCard(runCodeRef(), stdoutPreview(RUN_CODE_STDOUT_WITH_CSV.replace(
      "2026年3月  13.900000  190.7500",
      ["2026年3月  13.900000  190.7500", "3  2026年4月  13.000000  188.0000", "4  2026年5月  12.900000  187.0000",
        "5  2026年6月  12.500000  186.0000"].join("\n"),
    )));
    const table = screen.getByTestId("prov-data-table");
    expect(table.querySelectorAll("tbody tr")).toHaveLength(5);
    expect(table.textContent).toContain("显示 5 / 6 行");
  });

  it("falls back to a line-preserving excerpt when the stdout has no table", () => {
    renderCard(runCodeRef(), stdoutPreview("rows=8\ncorr=0.41\nmax month=4"));
    const excerpt = screen.getByTestId("prov-excerpt");
    expect(excerpt.textContent).toBe("rows=8\ncorr=0.41\nmax month=4");
    expect(screen.queryByTestId("prov-data-table")).toBeNull();
  });

  it("renders only the header and one caveat when nothing was previewed", () => {
    const { container } = renderCard(runCodeRef(), null);
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
    expect(container.querySelector(".rc-prov-rows")).toBeNull();
  });

  it("enables View code only when the cited call has a same-turn code block on this canvas", () => {
    const lookup = new Map<string, CallIdLookupEntry>([
      ["call_same", { sectionId: "sec_code", turnId: "t_1", inputArtifactId: undefined }],
      ["call_other", { sectionId: "sec_code", turnId: "t_0", inputArtifactId: undefined }],
    ]);
    const { code } = renderCard(runCodeRef(), null, { sourceId: "call_same", lookup, currentTurnId: "t_1" });
    const button = screen.getByRole("button", { name: zhMessages.popover.view_code });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(code.scrollIntoView).toHaveBeenCalledTimes(1);
    cleanup();
    renderCard(runCodeRef(), null, { sourceId: "call_other", lookup, currentTurnId: "t_1" });
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("hover card for other source kinds", () => {
  it("BI query: carried rows render as a table with the caveat once", () => {
    const ref: SourceRef = {
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 2,
      safe_label: "2026年1-8月 利润率 每月",
      safe_query: "2026年1-8月 利润率 每月",
      safe_preview_columns: ["每月", "利润率"],
      safe_preview_rows: [{ 每月: "2026年1月", 利润率: "0.1744" }],
      safe_total_rows: 8,
    };
    const { container } = renderCard(ref, null);
    expect(screen.getByTestId("prov-data-table").textContent).toContain("2026年1月");
    expect(container.textContent).toContain("显示 1 / 8 行");
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
  });

  it("file: a lazily fetched CSV preview renders as a table", () => {
    const ref: SourceRef = { id: ALIAS, source_alias: ALIAS, access_state: "linked_accessible", generic_type: "generated_artifact", safe_label: "profit.csv" };
    renderCard(ref, { family: "table", mode: "rows", text: null, columns: ["月份", "利润率"], rows: [{ 月份: "1", 利润率: 0.17 }], total_rows: 1 });
    expect(screen.getByTestId("prov-data-table").textContent).toContain("0.17");
  });

  it("web: an inline text excerpt shows with the query as a distinct sub line", () => {
    const ref: SourceRef = {
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "web_source",
      safe_label: "Retail outlook 2026",
      safe_query: "retail margin outlook",
      safe_preview: "Margins held steady.\nRefunds fell for two months.",
    };
    const { container } = renderCard(ref, null);
    expect(container.querySelector(".rc-prov-head .t-sub")?.textContent).toBe("retail margin outlook");
    expect(screen.getByTestId("prov-excerpt").textContent).toBe("Margins held steady.\nRefunds fell for two months.");
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
  });

  it("knowledge base (legacy shape without locator): caveat once, never as the sub line", () => {
    const { container } = renderIntl(
      <KbChunkPopoverContent
        ref={{ id: ALIAS, source_alias: ALIAS, access_state: "linked_accessible", generic_type: "document", safe_label: "Board notes" }}
        valueDisplay={null}
      />,
    );
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
    expect(container.querySelector(".rc-prov-head .t-sub")).toBeNull();
  });

  it("knowledge base unavailable: caveat once", () => {
    const { container } = renderIntl(
      <KbChunkPopoverContent
        ref={{ id: ALIAS, source_alias: ALIAS, access_state: "linked_unavailable", generic_type: "document" }}
        valueDisplay={null}
      />,
    );
    expect(countOccurrences(container.textContent ?? "", ZH_DEFINITION)).toBe(1);
  });
});

describe("hover card language follows the viewer interface locale", () => {
  it.each([
    ["zh-CN", zhMessages],
    ["en-US", enMessages],
    ["de-DE", deMessages],
    ["fr-FR", frMessages],
  ] as const)("%s chrome copy", (locale, messages) => {
    const { container } = renderCard(runCodeRef(), stdoutPreview(RUN_CODE_STDOUT_ALIGNED), { locale });
    expect(container.textContent).toContain(messages.provenance.source_linked);
    expect(container.textContent).toContain(messages.provenance.source_link_definition);
    if (locale !== "en-US") expect(container.textContent).not.toContain(EN_DEFINITION);
  });
});

describe("distinctSub", () => {
  it.each([
    ["Run Code: q", "q", null],
    ["Revenue source", "Revenue by region", "Revenue by region"],
    [undefined, "q", "q"],
    ["label", undefined, null],
  ] as const)("%j / %j", (label, query, expected) => {
    expect(distinctSub(label, query)).toBe(expected);
  });
});
