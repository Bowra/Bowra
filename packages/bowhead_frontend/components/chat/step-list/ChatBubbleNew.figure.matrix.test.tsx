/**
 * chat-inline-figure — **all kinds × reference combinations** matrix, targeting duplicates/loss/misplacement.
 *
 * Core invariant (asserted in every case): **each artifact renders exactly once in the entire bubble**:
 * either inline (named by an anchor) or at the end, never in both places and never missing.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, render } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatBubbleNew } from "./ChatBubbleNew";
import type { ChatInlineArtifact } from "./ChatBubbleNew";

vi.mock("./ChatInlineChart", () => ({
  ChatInlineChart: () => <div data-testid="a" data-k="chart" />,
}));
vi.mock("./ChatInlineKpi", () => ({ ChatInlineKpi: () => <div data-testid="a" data-k="kpi" /> }));
// After consolidation, chat tables directly use `DataTableSection` (`ChartTable`'s adapter wrapper),
// without the `ChatInlineTable` layer. Move the mock too; otherwise it misses its target,
// the real component receives empty data and renders null, losing an artifact in that matrix cell.
vi.mock("@/components/canvas/sections/data-table", () => ({
  DataTableSection: () => <div data-testid="a" data-k="table" />,
}));
vi.mock("./ChatInlineFile", () => ({ ChatInlineFile: () => <div data-testid="a" data-k="file" /> }));
vi.mock("./ChatInlineImage", () => ({ ChatInlineImage: () => <div data-testid="a" data-k="image" /> }));

afterEach(cleanup);

const chart = (id: string) => ({ kind: "chart", id, spec: {} }) as unknown as ChatInlineArtifact;
const kpi = (id: string) => ({ kind: "kpi", id, data: {} }) as unknown as ChatInlineArtifact;
const table = (id: string) => ({ kind: "table", id, data: {} }) as unknown as ChatInlineArtifact;
const file = (id: string) => ({ kind: "file", id, data: { id, filename: "f.xlsx", download_url: "/f" } }) as unknown as ChatInlineArtifact;
const image = (artId: string, secId: string) =>
  ({ kind: "image", id: artId, sectionId: secId, data: { id: artId, filename: "i.png", url: "/i", downloadUrl: "/i" } }) as unknown as ChatInlineArtifact;

/** Total artifacts rendered in the whole bubble (inline + trailing). */
const total = (c: HTMLElement) => c.querySelectorAll('[data-testid="a"]').length;
/** Artifact count in the trailing container. */
const inTail = (c: HTMLElement) =>
  c.querySelector(".chat-bubble-artifacts")?.querySelectorAll('[data-testid="a"]').length ?? 0;

/** ★Conservation: each artifact exactly once; inline count = total count - trailing count. */
function expectConserved(c: HTMLElement, artifacts: ChatInlineArtifact[], expectedInline: number) {
  expect(total(c)).toBe(artifacts.length); // No extras (duplicates) or omissions (loss).
  expect(total(c) - inTail(c)).toBe(expectedInline);
  expect(c.textContent).not.toContain("[figure:");
}

describe("矩阵 · 全 kind 单独引用", () => {
  const cases: Array<[string, ChatInlineArtifact, string]> = [
    ["chart", chart("sec_c"), "sec_c"],
    ["kpi", kpi("sec_k"), "sec_k"],
    ["table", table("sec_t"), "sec_t"],
    ["image(别名 sec)", image("art_i", "sec_i"), "sec_i"],
    ["image(主键 art)", image("art_i2", "sec_i2"), "art_i2"],
    ["file(art 主键)", file("art_f"), "art_f"],
  ];
  for (const [name, art, anchorId] of cases) {
    it(`${name} ⇒ 渲在文中且只有一次`, () => {
      const { container } = render(
        <ChatBubbleNew text={`前文。\n[figure:${anchorId}]\n后文。`} artifacts={[art]} />,
      );
      expectConserved(container, [art], 1);
      expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();
    });
  }
});

describe("矩阵 · 混合 kind", () => {
  const arts = [chart("sec_c"), table("sec_t"), kpi("sec_k"), image("art_i", "sec_i")];

  it("★ 全部引用 ⇒ 4 个全在文中，末尾空，无重复", () => {
    const text = ["讲图。", "[figure:sec_c]", "讲表。", "[figure:sec_t]", "讲KPI。", "[figure:sec_k]", "讲图片。", "[figure:sec_i]"].join("\n");
    const { container } = render(<ChatBubbleNew text={text} artifacts={arts} />);
    expectConserved(container, arts, 4);
    expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();
    // Order = anchor order.
    expect([...container.querySelectorAll('[data-testid="a"]')].map((e) => e.getAttribute("data-k")))
      .toEqual(["chart", "table", "kpi", "image"]);
  });

  it("★ 只引用一半 ⇒ 2 在文中 2 在末尾，总数仍是 4", () => {
    const { container } = render(
      <ChatBubbleNew text={"甲。\n[figure:sec_c]\n乙。\n[figure:sec_i]\n丙。"} artifacts={arts} />,
    );
    expectConserved(container, arts, 2);
    expect(inTail(container)).toBe(2);
  });

  it("★ 一个都不引用 ⇒ 全在末尾（今天的行为），总数不变", () => {
    const { container } = render(<ChatBubbleNew text={"没有锚点。"} artifacts={arts} />);
    expectConserved(container, arts, 0);
    expect(inTail(container)).toBe(4);
  });
});

describe("矩阵 · 重复与异常引用", () => {
  const arts = [chart("sec_a"), chart("sec_b")];

  it("★ 同一 id 引用三次 ⇒ 只渲一次，另一张仍在末尾", () => {
    const { container } = render(
      <ChatBubbleNew text={"1。\n[figure:sec_a]\n2。\n[figure:sec_a]\n3。\n[figure:sec_a]\n4。"} artifacts={arts} />,
    );
    expectConserved(container, arts, 1);
    expect(inTail(container)).toBe(1);
  });

  it("★ 图片用两个键各引用一次 ⇒ 仍只渲一次（别名不能造出第二份）", () => {
    const img = image("art_i", "sec_i");
    const { container } = render(
      <ChatBubbleNew text={"甲。\n[figure:sec_i]\n乙。\n[figure:art_i]\n丙。"} artifacts={[img]} />,
    );
    expectConserved(container, [img], 1);
  });

  it("引用上一轮的图（本轮 artifacts 里没有）⇒ 锚点消失，本轮产物不受影响", () => {
    const { container } = render(
      <ChatBubbleNew text={"甲。\n[figure:sec_FROM_LAST_TURN]\n乙。\n[figure:sec_a]\n丙。"} artifacts={arts} />,
    );
    expectConserved(container, arts, 1);
    expect(container.textContent).not.toContain("sec_FROM_LAST_TURN");
  });

  it("全是未知 id ⇒ 产物一个不少地留在末尾", () => {
    const { container } = render(
      <ChatBubbleNew text={"甲。\n[figure:sec_X]\n乙。\n[figure:sec_Y]"} artifacts={arts} />,
    );
    expectConserved(container, arts, 0);
    expect(inTail(container)).toBe(2);
  });

  it("锚点在列表项里（非独占行）⇒ 剥掉，不切列表，产物落末尾", () => {
    const { container } = render(
      <ChatBubbleNew text={"- 甲 [figure:sec_a]\n- 乙\n- 丙\n- 丁"} artifacts={arts} />,
    );
    expect(container.querySelectorAll("li")).toHaveLength(4);
    expectConserved(container, arts, 0);
  });
});

describe("矩阵 · 扫描=渲染单一真源（review F1：旧线扫在这些形态上会让图消失）", () => {
  const arts = [chart("sec_a")];

  it("★ 宽松列表项里的锚点行 ⇒ 不渲槽位，图守恒落末尾，无字面量", () => {
    const { container } = render(
      <ChatBubbleNew text={"- 结论\n\n  [figure:sec_a]\n\n- 次条"} artifacts={arts} />,
    );
    expectConserved(container, arts, 0);
    expect(inTail(container)).toBe(1);
  });

  it("★ 紧凑列表的惰性续行锚点 ⇒ 同上守恒", () => {
    const { container } = render(
      <ChatBubbleNew text={"- 甲项\n[figure:sec_a]\n- 乙项"} artifacts={arts} />,
    );
    expectConserved(container, arts, 0);
    expect(inTail(container)).toBe(1);
  });

  it("★ 行尾锚点撞 setext 标题（下一行 ---）⇒ 变标题不渲，图守恒落末尾", () => {
    const { container } = render(
      <ChatBubbleNew text={"营收下行。[figure:sec_a]\n---\n后文。"} artifacts={arts} />,
    );
    expectConserved(container, arts, 0);
    expect(inTail(container)).toBe(1);
  });

  it("引用块里的锚点行 ⇒ 扫描与插件同判（渲在引用块内），守恒", () => {
    const { container } = render(
      <ChatBubbleNew text={"> 引用结论。\n> [figure:sec_a]\n\n后文。"} artifacts={arts} />,
    );
    expectConserved(container, arts, 1);
    expect(inTail(container)).toBe(0);
  });
});

describe("矩阵 · 多轮（每个气泡各管各的）", () => {
  it("★ 两轮同时在页面上 ⇒ 各渲各的，不串台、不重复", () => {
    const t1 = [chart("sec_1a"), chart("sec_1b")];
    const t2 = [chart("sec_2a")];
    const { container } = render(
      <div>
        <ChatBubbleNew text={"第一轮。\n[figure:sec_1a]\n讲完。"} artifacts={t1} />
        <ChatBubbleNew text={"第二轮。\n[figure:sec_2a]\n讲完。"} artifacts={t2} />
      </div>,
    );
    // Three total across both turns, no extras or omissions.
    expect(total(container)).toBe(3);
    expect(container.textContent).not.toContain("[figure:");
    const bubbles = container.querySelectorAll(":scope > div > div");
    expect(bubbles.length).toBeGreaterThanOrEqual(2);
  });

  it("★ 第二轮引用第一轮的 id ⇒ 第二轮不渲它（跨轮不借用），第一轮不受影响", () => {
    const t1 = [chart("sec_1a")];
    const t2 = [chart("sec_2a")];
    const { container } = render(
      <div>
        <ChatBubbleNew text={"一。\n[figure:sec_1a]"} artifacts={t1} />
        <ChatBubbleNew text={"二。\n[figure:sec_1a]\n[figure:sec_2a]"} artifacts={t2} />
      </div>,
    );
    expect(total(container)).toBe(2); // Cross-turn references must not render an extra copy.
    expect(container.textContent).not.toContain("[figure:");
  });

  it("跟进轮没有产物（只改了上一轮的图）⇒ 气泡不出末尾容器，也不漏字面量", () => {
    const { container } = render(
      <ChatBubbleNew text={"已为两张图补充说明。\n[figure:sec_prev]"} artifacts={[]} />,
    );
    expect(total(container)).toBe(0);
    expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();
    expect(container.textContent).not.toContain("[figure:");
  });
});

describe("矩阵 · markdown 构造（扫描与渲染必须给同一答案）", () => {
  // ★Added during review: `scanFigureAnchors` scans **raw markdown lines**,
  // while the plugin uses **hast parsed by remark**. Disagreeing about which line is an anchor causes
  // the worst failure: scan says referenced → remove from tail, but plugin does not render → **figure disappears entirely**.
  // Code fences and 4-space indentation each lost a figure in measurements. Conservation assertions detect such divergence generally.
  const arts = [chart("sec_a")];
  const cases: Array<[string, string, boolean]> = [
    // [name, markdown, whether the literal **should** remain inside a code block]
    ["代码围栏", "说明。\n\n```\n[figure:sec_a]\n```\n\n尾。", true],
    ["缩进代码块", "说明。\n\n    [figure:sec_a]\n\n尾。", true],
    ["引用块", "说明。\n\n> [figure:sec_a]\n\n尾。", false],
    ["列表项独占一行", "- 甲\n- [figure:sec_a]\n- 丙", false],
    ["标题行", "# [figure:sec_a]\n\n尾。", false],
    ["表格单元格", "| a |\n|---|\n| [figure:sec_a] |\n\n尾。", false],
  ];
  for (const [name, md, literalIsCode] of cases) {
    it(`${name} ⇒ 图不丢（守恒）`, () => {
      const { container } = render(<ChatBubbleNew text={md} artifacts={arts} />);
      // Wherever it renders, the artifact must appear exactly once; scanner/renderer disagreement must never make it disappear.
      expect(total(container)).toBe(1);
      if (!literalIsCode) {
        expect(container.textContent).not.toContain("[figure:");
      } else {
        // Brackets in a code block are **code**, not references, and should display unchanged.
        // (Same existing ruling as rehypeArtifactChips preserving `a[1]` inside fences.)
        expect(container.textContent).toContain("[figure:sec_a]");
      }
    });
  }
});

describe("矩阵 · 流式半截锚点与畸形 id", () => {
  const arts = [chart("sec_a")];

  // Character-by-character streaming could visibly type out the anchor; no intermediate frame may expose it.
  for (const tail of ["[", "[fi", "[fig", "[figure:", "[figure:sec_", "[figure:sec_a"]) {
    it(`流式中途 ${JSON.stringify(tail)} 不闪字面量`, () => {
      const { container } = render(
        <ChatBubbleNew text={`结论。\n${tail}`} isStreaming artifacts={arts} />,
      );
      expect(container.textContent).not.toContain("[fi");
    });
  }

  // Malformed id: the renderer should neither give it a slot nor expose it to the user.
  for (const bad of ["[figure:]", "[figure:  ]", "[figure:sec-a!]", "[figure:sec a]"]) {
    it(`畸形 ${bad} ⇒ 不渲槽位也不露字面量，图仍在末尾`, () => {
      const { container } = render(
        <ChatBubbleNew text={`结论。\n${bad}\n尾。`} artifacts={arts} />,
      );
      expect(container.textContent).not.toContain("[figure");
      expectConserved(container, arts, 0);
    });
  }
});

describe("figure anchors attached to paragraph endings", () => {
  // ★The peer was told to use a standalone line, but actually wrote an **end-of-sentence anchor**:
  //   `…整体呈单边下行趋势，未见明显反弹。[figure:sec_72df6d281cbd]`
  // Semantics are correct (figure beside its explanatory paragraph); only the form differs from the instruction.
  // The criterion must accept actual output shapes, or the feature does not work on the real device.
  const arts = [chart("sec_72df6d281cbd"), chart("sec_ba89e11a6271")];
  const REAL = [
    "2026年各月营业额整体呈下行趋势：1月最高（约19.9亿元）。 [cite:1]",
    "",
    "**2026年各月营业额持续走低** 营业额从年初高位逐月下滑。整体呈单边下行趋势，未见明显反弹。[figure:sec_72df6d281cbd]",
    "",
    "**品牌营业额TOP10：金辉福断层领先** 金辉福以绝对优势居首。远航（钟表）位列第七。[figure:sec_ba89e11a6271]",
    "",
    "> 营业额月度数据仅返回2026年1-8月。",
  ].join("\n");

  it("★★ 句尾锚点 ⇒ 两张图都渲在**对应那段之后**，末尾不再堆图", () => {
    const { container } = renderIntl(<ChatBubbleNew text={REAL} artifacts={arts} />);
    expectConserved(container, arts, 2);
    expect(container.querySelector(".chat-bubble-artifacts")).toBeNull();

    // The figure must follow its paragraph and precede the next.
    const nodes = [...container.querySelectorAll("p, [data-testid=a]")];
    const idx = (pred: (t: string) => boolean) =>
      nodes.findIndex((n) => n.getAttribute("data-testid") !== "a" && pred(n.textContent ?? ""));
    const chartIdx = nodes.reduce<number[]>((acc, n, i) => (n.getAttribute("data-testid") === "a" ? [...acc, i] : acc), []);
    const trendPara = idx((s) => s.includes("未见明显反弹"));
    const brandPara = idx((s) => s.includes("金辉福以绝对优势居首"));
    expect(chartIdx[0]).toBeGreaterThan(trendPara); // First figure follows the trend paragraph.
    expect(chartIdx[0]).toBeLessThan(brandPara); //    And precedes the brand paragraph.
    expect(chartIdx[1]).toBeGreaterThan(brandPara); // Second figure follows the brand paragraph.
  });

  it("句尾锚点前面的正文不能被切丢", () => {
    const { container } = renderIntl(<ChatBubbleNew text={REAL} artifacts={arts} />);
    expect(container.textContent).toContain("未见明显反弹。");
    expect(container.textContent).toContain("远航（钟表）位列第七。");
    expect(container.textContent).not.toContain("[figure:");
  });
});
