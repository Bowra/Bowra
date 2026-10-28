import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";
import { SourceAccessRegistryProvider } from "@/lib/source-access-registry";
import { SourcesSection } from "./sources";

const chunkState = vi.hoisted(() => ({
  calls: [] as Array<[string | undefined, string | undefined, string | undefined]>,
  value: { status: "idle" } as Record<string, unknown>,
}));

vi.mock("@/lib/canvas-pm-adapter/kb-chunk-cache", () => ({
  useKbChunkContent: (fileId?: string, chunkId?: string, spaceId?: string) => {
    chunkState.calls.push([fileId, chunkId, spaceId]);
    return fileId ? chunkState.value : { status: "idle" };
  },
  evictKbChunks: () => {},
  clearKbChunkCache: () => {},
}));

const A = "source-ref-v1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const B = "source-ref-v1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
const C = "source-ref-v1:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC";

afterEach(() => {
  cleanup();
  chunkState.calls = [];
  chunkState.value = { status: "idle" };
});

function renderSources(entries: unknown[]) {
  const data = { id: "sources", kind: "sources", label: "数据来源", entries } as Extract<ReportSection, { kind: "sources" }>;
  return renderIntl(
    <SourceAccessRegistryProvider>
      <SourcesSection data={data} />
    </SourceAccessRegistryProvider>,
  );
}

describe("SourcesSection knowledge-base entries", () => {
  it("shows file name, location, passage and a knowledge-base deep link for a readable entry", () => {
    chunkState.value = {
      status: "ready",
      content: "第三季度华东区 42 家门店实现营业额 3.86 亿元，同比增长 7.3%。",
      chunk: { chunk_id: "chunk_a", page: 3, section_title: "一、总体情况", chunk_index: 5 },
    };
    const { container, getByText } = renderSources([
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
    ]);
    expect(getByText("2025Q3 华东区门店经营月报.md")).toBeInTheDocument();
    expect(getByText("知识库 · 空间 · 第 3 页")).toBeInTheDocument();
    expect(container.textContent).toContain("同比增长 7.3%");
    const link = getByText("在知识库中打开").closest("a");
    expect(link?.getAttribute("href")).toContain("file_id=kbf_1");
    expect(link?.getAttribute("href")).toContain("chunk_id=chunk_a");
    expect(link?.getAttribute("href")).toContain("space=ws_kb");
    expect(chunkState.calls).toContainEqual(["kbf_1", "chunk_a", "ws_kb"]);
    expect(container.textContent).not.toContain("来源当前不可查看");
  });

  it("says restricted or missing without any file detail and never fetches the passage", () => {
    const { container } = renderSources([
      { source_alias: B, access_state: "linked_restricted", generic_type: "document", ordinal: 1 },
      { source_alias: C, access_state: "linked_missing", generic_type: "document", ordinal: 2 },
    ]);
    expect(container.textContent).toContain("该来源仅对有权限的成员可见");
    expect(container.textContent).toContain("该来源已失效");
    expect(container.querySelector("a")).toBeNull();
    expect(chunkState.calls.every(([fileId]) => fileId === undefined)).toBe(true);
  });

  it("keeps non-knowledge-base entries on the existing path", () => {
    const { container } = renderSources([
      { source_alias: A, access_state: "linked_accessible", generic_type: "query_result", ordinal: 1, safe_label: "BI 查询" },
      { source_alias: B, access_state: "linked_unavailable", generic_type: "document", ordinal: 2 },
    ]);
    expect(container.textContent).toContain("BI 查询");
    expect(container.textContent).toContain("来源当前不可查看");
    expect(container.textContent).not.toContain("该来源仅对有权限的成员可见");
  });
});
