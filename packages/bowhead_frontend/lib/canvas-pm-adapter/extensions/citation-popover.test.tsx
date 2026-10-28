import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FilePreview } from "@/lib/api";
import type { SourceRef } from "../citation-types";
import {
  BiSourceCard,
  KbChunkPopoverContent,
  sourceRefFromNumberedEntry,
} from "./citation-popover";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vals?: Record<string, unknown>) => {
    if (key === "rows_shown") return `显示 ${vals?.shown} / ${vals?.total} 行`;
    return key;
  },
}));

const kbChunk = vi.hoisted(() => ({
  calls: [] as Array<[string | undefined, string | undefined, string | undefined]>,
  value: { status: "idle" } as Record<string, unknown>,
}));

vi.mock("../kb-chunk-cache", () => ({
  useKbChunkContent: (fileId?: string, chunkId?: string, spaceId?: string) => {
    kbChunk.calls.push([fileId, chunkId, spaceId]);
    return fileId ? kbChunk.value : { status: "idle" };
  },
  evictKbChunks: () => {},
  clearKbChunkCache: () => {},
}));

afterEach(() => {
  cleanup();
  kbChunk.calls = [];
  kbChunk.value = { status: "idle" };
});

const ALIAS = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

describe("sourceRefFromNumberedEntry viewer-safe contract", () => {
  it("adapts only an accessible opaque alias and safe presentation fields", () => {
    const out = sourceRefFromNumberedEntry({
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 4,
      safe_label: "Q1 revenue",
      safe_query: "Revenue by quarter",
      safe_preview_columns: ["quarter", "revenue"],
      safe_preview_rows: [{ quarter: "Q1", revenue: "42" }],
      safe_total_rows: 12,
      safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
      safe_url: "/api/v1/report-artifacts/art-safe/download",
    }, 0);

    expect(out.quality).toBe("artifact_only");
    expect(out.sourceRef).toEqual({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 4,
      safe_label: "Q1 revenue",
      safe_query: "Revenue by quarter",
      safe_preview_columns: ["quarter", "revenue"],
      safe_preview_rows: [{ quarter: "Q1", revenue: "42" }],
      safe_total_rows: 12,
      safe_preview_url: "/api/v1/report-artifacts/art-safe/preview",
      safe_url: "/api/v1/report-artifacts/art-safe/download",
    });
  });

  it("keeps an unavailable slot but drops every business detail and action", () => {
    const out = sourceRefFromNumberedEntry({
      source_alias: ALIAS,
      access_state: "linked_unavailable",
      generic_type: "document",
      ordinal: 2,
      safe_label: "must not survive",
      safe_query: "secret query",
      safe_preview: "secret body",
      safe_url: "/secret",
    }, 1);

    expect(out.quality).toBe("unavailable");
    expect(out.sourceRef).toEqual({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_unavailable",
      generic_type: "document",
      ordinal: 2,
    });
  });

  it("rejects canonical and legacy resource identities", () => {
    for (const entry of [
      { id: "art_secret", access_state: "linked_accessible" },
      { artifact_id: "art_secret", tool: "bi_query", query: "secret" },
      undefined,
    ]) {
      const out = sourceRefFromNumberedEntry(entry, 0);
      expect(out).toEqual({ quality: "unavailable" });
    }
  });
});

function renderBiCard(ref: SourceRef, preview: FilePreview | null = null) {
  return render(
    <BiSourceCard
      ref_={ref}
      preview={preview}
      valueDisplay={null}
      sourceId={null}
      callIdLookup={new Map()}
      rootRef={{ current: null }}
    />,
  );
}

describe("BiSourceCard viewer-safe presentation", () => {
  it("renders inline rows carried by an authorized projection", () => {
    const { getByTestId, container } = renderBiCard({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 1,
      safe_label: "Revenue source",
      safe_query: "Revenue by region",
      safe_preview_columns: ["region", "revenue"],
      safe_preview_rows: [{ region: "East", revenue: "42" }],
      safe_total_rows: 8,
    });

    expect(getByTestId("prov-data-table").textContent).toContain("East");
    expect(container.textContent).toContain("显示 1 / 8 行");
    expect(container.querySelector("a")).toBeNull();
  });

  it("renders a freshly authorized preview response without resource identities", () => {
    const preview = {
      family: "table",
      mode: "rows",
      text: "",
      columns: ["brand", "amount"],
      rows: [{ brand: "A", amount: 606 }],
      total_rows: 10,
    } as FilePreview;
    const { getByTestId, container } = renderBiCard({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_accessible",
      generic_type: "query_result",
      ordinal: 1,
    }, preview);

    expect(getByTestId("prov-data-table").textContent).toContain("606");
    expect(container.textContent).not.toContain("art_secret");
  });
});

describe("KbChunkPopoverContent viewer-safe presentation", () => {
  it("renders only server-approved document detail", () => {
    const { container } = render(
      <KbChunkPopoverContent
        ref={{
          id: ALIAS,
          source_alias: ALIAS,
          access_state: "linked_accessible",
          generic_type: "document",
          ordinal: 3,
          safe_label: "Board notes",
          safe_query: "cash flow",
          safe_preview: "Approved excerpt",
        }}
        valueDisplay={null}
      />,
    );
    expect(container.textContent).toContain("Board notes");
    expect(container.textContent).toContain("Approved excerpt");
  });

  it("does not render stale detail when access is unavailable", () => {
    const { container } = render(
      <KbChunkPopoverContent
        ref={{
          id: ALIAS,
          source_alias: ALIAS,
          access_state: "linked_unavailable",
          generic_type: "document",
          ordinal: 3,
          safe_label: "stale title",
          safe_query: "stale query",
          safe_preview: "stale excerpt",
        }}
        valueDisplay={null}
      />,
    );
    expect(container.textContent).toContain("source_unavailable");
    expect(container.textContent).not.toContain("stale title");
    expect(container.textContent).not.toContain("stale query");
    expect(container.textContent).not.toContain("stale excerpt");
  });
});

describe("report knowledge-base citation popover (知识库回答引用批 3)", () => {
  const readable = {
    source_alias: ALIAS,
    access_state: "linked_accessible",
    generic_type: "document",
    ordinal: 3,
    safe_label: "2025Q3 华东区门店经营月报.md",
    kb_file_id: "kbf_1",
    kb_chunk_id: "chunk_a",
    kb_scope: "space",
    kb_space_id: "ws_kb",
  };

  it("numbered entries keep knowledge-base locators only when readable", () => {
    const out = sourceRefFromNumberedEntry(readable, 2);
    expect(out.sourceRef).toMatchObject({
      access_state: "linked_accessible",
      safe_label: "2025Q3 华东区门店经营月报.md",
      kb_file_id: "kbf_1",
      kb_chunk_id: "chunk_a",
      kb_scope: "space",
      kb_space_id: "ws_kb",
    });
    const restricted = sourceRefFromNumberedEntry(
      { source_alias: ALIAS, access_state: "linked_restricted", generic_type: "document", kb_file_id: "kbf_1", safe_label: "x" },
      0,
    );
    expect(restricted.sourceRef).toEqual({
      id: ALIAS,
      source_alias: ALIAS,
      access_state: "linked_restricted",
      generic_type: "document",
      ordinal: 1,
    });
  });

  it("shows file name, location, highlighted passage and the knowledge-base link", () => {
    kbChunk.value = {
      status: "ready",
      content: "第三季度华东区 42 家门店实现营业额 3.86 亿元，同比增长 7.3%，环比增长 2.1%。",
      chunk: { chunk_id: "chunk_a", page: null, section_title: "一、总体情况", chunk_index: 2 },
    };
    const ref = sourceRefFromNumberedEntry(readable, 2).sourceRef as SourceRef;
    const { container, getByText } = render(
      <KbChunkPopoverContent ref={ref} valueDisplay={null} context="华东区三季度营业额同比增长 7.3%" />,
    );
    expect(getByText("2025Q3 华东区门店经营月报.md")).toBeTruthy();
    expect(container.textContent).toContain("cite_kb_prefix · cite_kb_scope_space · 一、总体情况");
    expect(container.querySelector("mark")?.textContent).toContain("同比增长 7.3%");
    expect(container.textContent).toContain("kb_passage_affirm");
    const href = getByText("cite_kb_open").closest("a")?.getAttribute("href") ?? "";
    expect(href).toContain("file_id=kbf_1");
    expect(href).toContain("chunk_id=chunk_a");
    expect(kbChunk.calls).toContainEqual(["kbf_1", "chunk_a", "ws_kb"]);
  });

  it("says the passage was updated when the chunk is gone", () => {
    kbChunk.value = { status: "empty" };
    const ref = sourceRefFromNumberedEntry(readable, 2).sourceRef as SourceRef;
    const { container } = render(<KbChunkPopoverContent ref={ref} valueDisplay={null} />);
    expect(container.textContent).toContain("cite_kb_updated");
    expect(container.textContent).toContain("cite_kb_open");
  });

  it("restricted shows only the restricted notice and fetches nothing", () => {
    const { container } = render(
      <KbChunkPopoverContent
        ref={{ id: ALIAS, source_alias: ALIAS, access_state: "linked_restricted", generic_type: "document" }}
        valueDisplay={null}
      />,
    );
    expect(container.textContent).toContain("cite_kb_restricted_tip");
    expect(container.textContent).toContain("cite_kb_restricted");
    expect(container.querySelector("a")).toBeNull();
    expect(kbChunk.calls.every(([fileId]) => fileId === undefined)).toBe(true);
  });
});
