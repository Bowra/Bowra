/**
 * Knowledge-base answer citations (design 08): A1 markers and inline source cards with three states (viewable / original updated / read failed);
 * restricted and unavailable sources do not expand. Fetch originals with the viewer's current permissions, sending the file's space header for excerpts.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextIntlClientProvider } from "next-intl";
import { renderIntl } from "@/__tests__/intl-render";
import zh from "@/messages/zh-CN.json";
import { clearKbChunkCache } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import type { Citation } from "@/lib/types";

import { CitationsProvider, LeadNarrative } from "./LeadNarrative";

const KB: Citation = {
  seq: 1,
  artifactId: "source-ref-v1:kb",
  label: "门店日销售_9月.csv",
  kind: "kb_chunk",
  accessState: "linked_accessible",
  kb: { fileId: "kbf_1", chunkId: "chunk_a", scope: "space", spaceId: "ws_kb" },
};

const TABLE = [
  "| 日期 | 门店 | 品类 | 销售额 |",
  "| --- | --- | --- | --- |",
  "| 2025-09-17 | 徐汇天钥桥路店 | 生鲜 | 45,210 |",
  "| 2025-09-17 | 滨江龙湖天街店 | 进口水果 | 236,480 |",
].join("\n");

function render(citations: Citation[]) {
  return renderIntl(
    <CitationsProvider citations={citations}>
      <LeadNarrative text="9 月 17 日滨江龙湖天街店进口水果销售额为 236,480 元[cite:1]。" />
    </CitationsProvider>,
  );
}

function mockChunks(body: unknown, ok = true) {
  const fetchMock = vi.fn(async () => ({ ok, status: ok ? 200 : 403, json: async () => body }) as Response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("知识库引用角标", () => {
  beforeEach(() => clearKbChunkCache());
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("可查看：展开来源卡，按文件所在空间取片段，显示位置、高亮原文与打开入口", async () => {
    const fetchMock = mockChunks({
      chunks: [{ chunk_id: "chunk_a", content: TABLE, page: null, section_title: null, role: "table", chunk_index: 5 }],
      total: 1,
    });
    const { container } = render([KB]);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    expect(chip.querySelector(".bh-kb-doc-icon")).not.toBeNull();
    expect(chip.getAttribute("data-tip")).toBe("门店日销售_9月.csv");

    fireEvent.click(chip);

    await screen.findByText("知识库 · 空间 · 第 5 段");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/kb/files/kbf_1/chunks?chunk_id=chunk_a");
    expect(new Headers(init.headers).get("X-Space-Id")).toBe("ws_kb");
    const mark = container.querySelector(".bh-kb-source-quote mark");
    expect(mark?.textContent?.trim()).toBe("236,480");
    expect(mark?.parentElement?.textContent).toContain("滨江龙湖天街店");
    const open = screen.getByText("在知识库中打开") as HTMLAnchorElement;
    expect(open.getAttribute("href")).toBe("/knowledge/?scope=space&space=ws_kb&file_id=kbf_1&chunk_id=chunk_a");
  });

  it("来源卡展开在段落末尾：句末标点留在正文里，不被卡片劈开；收起后不留空槽", async () => {
    mockChunks({ chunks: [{ chunk_id: "chunk_a", content: TABLE, page: null, section_title: null, role: "table", chunk_index: 5 }], total: 1 });
    const { container } = render([KB]);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    await screen.findByText("知识库 · 空间 · 第 5 段");
    const para = chip.closest("p") as HTMLElement;
    const card = container.querySelector(".bh-kb-source-card") as HTMLElement;
    expect(para.lastElementChild?.contains(card)).toBe(true);
    // Body text before the card ends with “。” (a complete sentence); no body text follows the card.
    const before = para.textContent!.slice(0, para.textContent!.indexOf("门店日销售_9月.csv"));
    expect(before.trim().endsWith("。")).toBe(true);
    fireEvent.click(chip);
    await waitFor(() => expect(container.querySelector(".bh-chat-source-slot")).toBeNull());
  });

  it("父组件重渲染时来源卡保持展开（116 1.3.53：卡片打开几秒后被轮询重渲染关掉）", async () => {
    mockChunks({ chunks: [{ chunk_id: "chunk_a", content: TABLE, page: null, section_title: null, role: "table", chunk_index: 5 }], total: 1 });
    const text = "9 月 17 日滨江龙湖天街店进口水果销售额为 236,480 元[cite:1]。";
    const { container, rerender } = renderIntl(
      <CitationsProvider citations={[KB]}>
        <LeadNarrative text={text} />
      </CitationsProvider>,
    );
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    fireEvent.click(chip);
    await screen.findByText("知识库 · 空间 · 第 5 段");
    // Same content, new citations array reference (common in polling / replay)
    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <CitationsProvider citations={[{ ...KB }]}>
          <LeadNarrative text={text} />
        </CitationsProvider>
      </NextIntlClientProvider>,
    );
    await new Promise((r) => setTimeout(r, 50));
    expect(container.querySelector(".bh-artifact-chip")).toBe(chip);
    expect(container.querySelector(".bh-kb-source-card")).not.toBeNull();
  });

  it("表格单元格里的角标：来源卡展开在整张表之后，不撑宽表格", async () => {
    mockChunks({ chunks: [{ chunk_id: "chunk_a", content: TABLE, page: null, section_title: null, role: "table", chunk_index: 5 }], total: 1 });
    const text = "| 门店 | 销售额 |\n| --- | --- |\n| 滨江龙湖天街店 | 236,480 元[cite:1] |\n\n表后正文。";
    const { container } = renderIntl(
      <CitationsProvider citations={[KB]}>
        <LeadNarrative text={text} />
      </CitationsProvider>,
    );
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    const table = chip.closest("table") as HTMLTableElement;
    expect(table).not.toBeNull();
    fireEvent.click(chip);
    await screen.findByText("知识库 · 空间 · 第 5 段");
    const card = container.querySelector(".bh-kb-source-card") as HTMLElement;
    expect(table.contains(card)).toBe(false);
    expect(table.nextElementSibling?.contains(card)).toBe(true);
    fireEvent.click(chip);
    await waitFor(() => expect(container.querySelector(".bh-chat-source-slot")).toBeNull());
  });

  it("回答还在生成时点角标不展开来源卡、也不取片段；生成完成后照常展开（对照）", async () => {
    const fetchMock = mockChunks({ chunks: [{ chunk_id: "chunk_a", content: TABLE, page: null, section_title: null, role: "table", chunk_index: 5 }], total: 1 });
    const text = "9 月 17 日滨江龙湖天街店进口水果销售额为 236,480 元[cite:1]。";
    const { container, rerender } = renderIntl(
      <CitationsProvider citations={[KB]}>
        <LeadNarrative text={text} isStreaming />
      </CitationsProvider>,
    );
    fireEvent.click(container.querySelector(".bh-artifact-chip") as HTMLElement);
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelector(".bh-kb-source-card")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    rerender(
      <NextIntlClientProvider locale="zh-CN" messages={zh}>
        <CitationsProvider citations={[KB]}>
          <LeadNarrative text={text} />
        </CitationsProvider>
      </NextIntlClientProvider>,
    );
    fireEvent.click(container.querySelector(".bh-artifact-chip") as HTMLElement);
    await screen.findByText("知识库 · 空间 · 第 5 段");
  });

  it("片段已不在：提示原文已更新，不显示任何原文", async () => {
    mockChunks({ chunks: [{ chunk_id: "chunk_other", content: "重新处理后的另一段", page: null, section_title: null, role: "intro", chunk_index: 1 }], total: 1 });
    const { container } = render([KB]);
    fireEvent.click(container.querySelector(".bh-artifact-chip") as HTMLElement);
    await screen.findByText(/原文已更新/);
    expect(container.textContent).not.toContain("另一段");
    expect(screen.getByText("在知识库中打开")).toBeTruthy();
  });

  it("读取失败：只给提示与打开入口", async () => {
    mockChunks({ detail: "forbidden" }, false);
    const { container } = render([KB]);
    fireEvent.click(container.querySelector(".bh-artifact-chip") as HTMLElement);
    await screen.findByText(/暂时读不到原文/);
    expect(container.querySelector(".bh-kb-source-quote")).toBeNull();
  });

  it("受限：置灰、悬停只写来源受限、点击不展开也不取片段", async () => {
    const fetchMock = mockChunks({ chunks: [], total: 0 });
    const { container } = render([{ ...KB, label: null, accessState: "linked_restricted", kb: undefined }]);
    const chip = container.querySelector(".bh-artifact-chip") as HTMLElement;
    expect(chip.getAttribute("data-tip")).toBe("来源受限");
    expect(chip.classList.contains("is-denied")).toBe(true);
    fireEvent.click(chip);
    await waitFor(() => expect(container.querySelector(".bh-kb-source-card")).toBeNull());
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
