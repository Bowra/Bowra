/**
 * chat-inline-figure-2026-08 review gap — **surfaces without renderFigureSlot** must not leak literals either.
 *
 * Why this is needed: Lead prose contains `[figure:]` only after batch 3 is enabled, but intermediate narrative bubbles
 * (`TurnContainer` narrative beat / fallbackNarrative) **do not pass** renderFigureSlot ⇒
 * no rehype plugin ⇒ without stripping, the anchor renders as ordinary text. Criterion 4 prohibits the
 * `[figure:` literal on **every** surface, not only the answer bubble.
 */
import "@testing-library/jest-dom/vitest";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LeadNarrative } from "./LeadNarrative";

afterEach(cleanup);

const WITH_ANCHOR = "结论一。\n[figure:sec_a]\n**图1**：说明\n\n结论二。";

describe("LeadNarrative — 锚点不得在任何面上渲成字面量", () => {
  it("中间 narrative（不传 renderFigureSlot，非流式）⇒ 锚点剥掉", () => {
    const { container } = render(<LeadNarrative text={WITH_ANCHOR} />);
    expect(container.textContent).not.toContain("[figure:");
    expect(container.textContent).not.toContain("sec_a");
    // Body text and caption survive; only the figure is absent.
    expect(container.textContent).toContain("结论一。");
    expect(container.textContent).toContain("图1：说明");
    expect(container.textContent).toContain("结论二。");
  });

  it("流式（不传回调）⇒ 锚点剥掉", () => {
    const { container } = render(<LeadNarrative text={WITH_ANCHOR} isStreaming />);
    expect(container.textContent).not.toContain("[figure:");
  });

  it("流式（传了回调）⇒ 锚点仍剥掉，不闪字面量", () => {
    const { container } = render(
      <LeadNarrative text={WITH_ANCHOR} isStreaming renderFigureSlot={() => <i>SLOT</i>} />,
    );
    expect(container.textContent).not.toContain("[figure:");
    expect(container.textContent).not.toContain("SLOT"); // Do not render figures during streaming
  });

  it("答案气泡（传了回调，非流式）⇒ 锚点渲成图", () => {
    const { container } = render(
      <LeadNarrative text={WITH_ANCHOR} renderFigureSlot={() => <i data-testid="slot">SLOT</i>} />,
    );
    expect(container.querySelectorAll('[data-testid="slot"]')).toHaveLength(1);
    expect(container.textContent).not.toContain("[figure:");
  });

  it("无锚点文本 ⇒ 传不传回调渲染结果一致（不碰今天的行为）", () => {
    const plain = "## 标题\n\n- 甲\n- 乙\n\n收尾**粗体**。";
    const a = render(<LeadNarrative text={plain} />).container.innerHTML;
    cleanup();
    const b = render(<LeadNarrative text={plain} renderFigureSlot={() => null} />).container.innerHTML;
    expect(a).toBe(b);
  });
});
