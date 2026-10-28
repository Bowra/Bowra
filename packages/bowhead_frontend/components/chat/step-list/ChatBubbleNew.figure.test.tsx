/**
 * chat-inline-figure-2026-08 batch 2 — move referenced artifacts up; keep unreferenced ones at the end.
 *
 * Condition comes from `docs/sprints/chat-inline-figure-2026-08/tech-design.md` §7.
 * Assert **DOM order rendered by actual components**, not intermediate structures.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatBubbleNew } from "./ChatBubbleNew";
import type { ChatInlineArtifact } from "./ChatBubbleNew";

// Charts/tables depend on canvas/store; only **position** matters here, so render recognizable stubs.
vi.mock("./ChatInlineChart", () => ({
  ChatInlineChart: () => <div data-testid="chart">CHART</div>,
}));
vi.mock("./ChatInlineKpi", () => ({
  ChatInlineKpi: () => <div data-testid="kpi">KPI</div>,
}));
vi.mock("./ChatInlineFile", () => ({
  ChatInlineFile: () => <div data-testid="file">FILE</div>,
}));
vi.mock("./ChatInlineImage", () => ({
  ChatInlineImage: () => <div data-testid="image">IMAGE</div>,
}));

afterEach(cleanup);

const chart = (id: string): ChatInlineArtifact =>
  ({ kind: "chart", id, spec: {} }) as unknown as ChatInlineArtifact;

/** Marker sequence in DOM document order, for asserting figures' positions relative to prose. */
function sequence(container: HTMLElement): string[] {
  const out: string[] = [];
  container.querySelectorAll("p, [data-testid]").forEach((el) => {
    const tid = el.getAttribute("data-testid");
    if (tid) out.push(`<${tid}>`);
    else if (el.textContent?.trim()) out.push(el.textContent.trim());
  });
  return out;
}

describe("ChatBubbleNew — 行内 figure 位置", () => {
  it("判据1 ★ 无锚点 ⇒ 与今天一致：全部产物堆在散文之后", () => {
    const { container } = render(
      <ChatBubbleNew text={"结论一。\n\n结论二。"} artifacts={[chart("sec_a"), chart("sec_b")]} />,
    );
    expect(sequence(container)).toEqual(["结论一。", "结论二。", "<chart>", "<chart>"]);
    expect(container.querySelector(".chat-bubble-artifacts")).not.toBeNull();
  });

  it("判据2 ★ 被点名的图上移到论点旁，且不在末尾重复", () => {
    const { container } = render(
      <ChatBubbleNew
        text={"结论一。\n[figure:sec_a]\n**图1**：说明\n\n结论二。"}
        artifacts={[chart("sec_a")]}
      />,
    );
    expect(sequence(container)).toEqual(["结论一。", "<chart>", "图1：说明", "结论二。"]);
    // Exactly one chart in the entire text: no duplicate at the end.
    expect(container.querySelectorAll('[data-testid="chart"]')).toHaveLength(1);
    // No tail container at all (no unreferenced artifacts).
    expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();
    expect(container.textContent).not.toContain("[figure:");
  });

  it("判据10 ★★ 只点名一部分 ⇒ 其余仍在末尾，一张不少", () => {
    const { container } = render(
      <ChatBubbleNew
        text={"结论。\n[figure:sec_b]\n收尾。"}
        artifacts={[chart("sec_a"), chart("sec_b"), chart("sec_c")]}
      />,
    );
    // All three charts must remain.
    expect(container.querySelectorAll('[data-testid="chart"]')).toHaveLength(3);
    // Referenced sec_b is in prose; two charts remain in tail (sec_a / sec_c).
    const tail = container.querySelector(".chat-bubble-artifacts");
    expect(tail).not.toBeNull();
    expect(tail!.querySelectorAll('[data-testid="chart"]')).toHaveLength(2);
  });

  it("判据4 ★ 未知 id ⇒ 锚点消失，产物全留末尾，不显示字面量", () => {
    const { container } = render(
      <ChatBubbleNew text={"结论。\n[figure:sec_NOPE]\n收尾。"} artifacts={[chart("sec_a")]} />,
    );
    expect(container.textContent).not.toContain("[figure:");
    expect(container.textContent).not.toContain("sec_NOPE");
    expect(container.querySelectorAll('[data-testid="chart"]')).toHaveLength(1);
    expect(container.querySelector(".chat-bubble-artifacts")).not.toBeNull();
  });

  it("判据5 ★ 流式期锚点不以字面量闪现", () => {
    const { container } = render(
      <ChatBubbleNew text={"结论一。\n[figure:sec_a]\n**图1**：说"} isStreaming artifacts={[chart("sec_a")]} />,
    );
    expect(container.textContent).not.toContain("[figure:");
    // Artifacts never appear while streaming (existing unchanged behavior).
    expect(container.querySelectorAll('[data-testid="chart"]')).toHaveLength(0);
  });

  it("判据11 同一 id 点名两次 ⇒ 只渲一次", () => {
    const { container } = render(
      <ChatBubbleNew
        text={"甲。\n[figure:sec_a]\n乙。\n[figure:sec_a]\n丙。"}
        artifacts={[chart("sec_a")]}
      />,
    );
    expect(container.querySelectorAll('[data-testid="chart"]')).toHaveLength(1);
    expect(container.textContent).not.toContain("[figure:");
  });

  it("★ 图片图用 sectionId 别名也能内联（dogfood 逮到的 art_*/sec_* 错位）", () => {
    // Backend `_inline_image_artifact`: id=art_* (ChatInlineImage opens Results with it),
    // section_id=sec_* (anchor namespace). A sec_* anchor must resolve.
    const img = {
      kind: "image",
      id: "art_a25c9105b3cb",
      sectionId: "sec_3b2fd54d1c2d",
      data: { id: "art_a25c9105b3cb", filename: "热力图.png", url: "/x", downloadUrl: "/y" },
    } as unknown as ChatInlineArtifact;
    const { container } = render(
      <ChatBubbleNew text={"各省分布如下。\n[figure:sec_3b2fd54d1c2d]\n**图1**：热力图"} artifacts={[img]} />,
    );
    // Figure renders in prose, not at the end.
    expect(container.querySelectorAll('[data-testid="image"]')).toHaveLength(1);
    expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();
    expect(container.textContent).not.toContain("[figure:");
  });

  it("图片图：artifact id 也仍然可用（不破坏既有 by-id 查找）", () => {
    const img = {
      kind: "image",
      id: "art_a",
      sectionId: "sec_a",
      data: { id: "art_a", filename: "x.png", url: "/x", downloadUrl: "/y" },
    } as unknown as ChatInlineArtifact;
    const { container } = render(
      <ChatBubbleNew text={"图。\n[figure:art_a]\n尾。"} artifacts={[img]} />,
    );
    expect(container.querySelectorAll('[data-testid="image"]')).toHaveLength(1);
  });

  it("file 交付物 chip 不参与行内（art_* 不是 figure 命名空间）", () => {
    const file = {
      kind: "file",
      id: "art_x",
      data: { id: "art_x", filename: "a.xlsx", download_url: "/x" },
    } as unknown as ChatInlineArtifact;
    const { container } = render(
      <ChatBubbleNew text={"结论。\n[figure:sec_a]\n尾。"} artifacts={[chart("sec_a"), file]} />,
    );
    // Figure moves up; file chip stays at the end.
    const tail = container.querySelector(".chat-bubble-artifacts");
    expect(tail!.querySelectorAll('[data-testid="file"]')).toHaveLength(1);
    expect(tail!.querySelectorAll('[data-testid="chart"]')).toHaveLength(0);
  });
});
