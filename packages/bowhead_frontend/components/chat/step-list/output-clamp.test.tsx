/**
 * chat-ux-2026-08 batch A — constrain evidence-drawer and peer-narration heights.
 *
 * Background (116 prod v1.0.71 observations): `.code` previously had **no height cap**, so each
 * python_sandbox stdout rendered its full length; the peer-narration lane likewise had no truncation,
 * letting model-written internal reasoning fill the entire left column. One turn's left column grew to 6664px.
 *
 * Both groups use **behavioral assertions** (collapsed marker + presence of an exit button), rather than reading CSS text—
 * CSS-reading tests would still pass after replacing the implementation, effectively testing nothing.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";
import { Code } from "./tool-results";
import { Prose } from "./block-frames";

// Repository convention: RTL has no global cleanup; each file cleans up itself (see AgentBlock.test.tsx:9).
afterEach(() => cleanup());

function wrap(ui: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      {ui}
    </NextIntlClientProvider>,
  );
}

const SHORT_CODE = "print(1)\nprint(2)";
const LONG_CODE = Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n");

describe("Code — 输出高度收口", () => {
  it("短输出不折叠，也不出「查看全部」", () => {
    const { container } = wrap(<Code code={SHORT_CODE} />);
    expect(container.querySelector(".codewrap.is-clipped")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看全部" })).toBeNull();
  });

  it("长输出默认折叠并给出口", () => {
    const { container } = wrap(<Code code={LONG_CODE} />);
    expect(container.querySelector(".codewrap.is-clipped")).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看全部" })).toBeTruthy();
  });

  it("点「查看全部」解除折叠，再点收起", () => {
    const { container } = wrap(<Code code={LONG_CODE} />);
    const btn = screen.getByRole("button", { name: "查看全部" });
    fireEvent.click(btn);
    expect(container.querySelector(".codewrap.is-clipped")).toBeNull();
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(container.querySelector(".codewrap.is-clipped")).not.toBeNull();
  });

  it("流式中（caret）不折叠——用户正盯着它往外吐", () => {
    const { container } = wrap(<Code code={LONG_CODE} caret />);
    expect(container.querySelector(".codewrap.is-clipped")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看全部" })).toBeNull();
  });

  it("内容本身不被丢弃——折叠只是视觉裁剪", () => {
    const { container } = wrap(<Code code={LONG_CODE} />);
    expect(container.querySelector(".code")?.textContent).toContain("line 39");
  });
});

describe("Prose — peer 自述收口", () => {
  // Excerpt of internal monologue observed on 116, exactly what leaked through this lane.
  const MONOLOGUE =
    "让我换个方式——去掉 delivery_surface 字段，走标准 report payload（带 insights），" +
    "或者尝试不同的 payload 结构。根据铁律 8，我需要改变 query shape。" +
    "让我尝试用标准 report 格式，soft_refuse 似乎是因为同样的 subagent_return 被调用了太多次。";

  it("短自述不折叠", () => {
    const { container } = wrap(<Prose>数据已汇总完毕。</Prose>);
    expect(container.querySelector(".prosewrap.is-clipped")).toBeNull();
    expect(screen.queryByRole("button", { name: "查看全部" })).toBeNull();
  });

  it("长自述（内部独白）默认折叠并给出口", () => {
    const { container } = wrap(<Prose>{MONOLOGUE}</Prose>);
    expect(container.querySelector(".prosewrap.is-clipped")).not.toBeNull();
    expect(screen.getByRole("button", { name: "查看全部" })).toBeTruthy();
  });

  it("展开后可见全文——收口的是权重不是内容", () => {
    const { container } = wrap(<Prose>{MONOLOGUE}</Prose>);
    fireEvent.click(screen.getByRole("button", { name: "查看全部" }));
    expect(container.querySelector(".prosewrap.is-clipped")).toBeNull();
    expect(container.querySelector(".prose")?.textContent).toContain("subagent_return");
  });
});
