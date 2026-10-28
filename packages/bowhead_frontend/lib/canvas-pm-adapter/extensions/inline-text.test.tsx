/**
 * W12-14 (#85) InlineEditableText integration unit tests.
 *
 * Core contracts:
 *  - isEditMode=false: no contentEditable response; blur does not trigger onChange
 *  - isEditMode=true: contentEditable; changed blur content → onChange (only when changed)
 *  - Single line (multiline=false): Enter triggers blur (without inserting a newline)
 *  - Multiline (multiline=true): Enter uses default behavior (without forcing blur)
 *  - Changed value prop → synchronized DOM textContent (external SSE patch scenario)
 *  - as selects different rendered tags
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render as rtlRender } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";

// InlineEditableText now uses useTranslations("inline_edit") (design r2 tooltip),
// wrap a real zh-CN provider, preserving rerender semantics.
function render(ui: React.ReactElement) {
  const wrap = (node: React.ReactElement) => (
    <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{node}</NextIntlClientProvider>
  );
  const result = rtlRender(wrap(ui));
  return { ...result, rerender: (next: React.ReactElement) => result.rerender(wrap(next)) };
}
import React from "react";

import { InlineEditableText } from "./inline-text";
import {
  commitPendingInlineEdits,
  pendingInlineEditCount,
} from "../inline-edit-registry";

describe("InlineEditableText", () => {
  it("read-only mode: blur 不触发 onChange", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText
        value="hello"
        isEditMode={false}
        onChange={onChange}
        as="span"
      />,
    );
    const el = container.querySelector("span")!;
    expect(el.textContent).toBe("hello");
    expect(el.getAttribute("contenteditable")).toBe("false");
    fireEvent.blur(el);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("edit mode: 内容变后 blur → onChange 调一次, value 是新内容", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="hello" isEditMode onChange={onChange} as="span" />,
    );
    const el = container.querySelector("span")!;
    expect(el.getAttribute("contenteditable")).toBe("true");
    el.textContent = "hello world";
    fireEvent.blur(el);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("hello world");
  });

  it("edit mode: 内容未变 → onChange 不调 (avoid 无谓 transaction)", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="hello" isEditMode onChange={onChange} />,
    );
    const el = container.querySelector("span")!;
    fireEvent.blur(el);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("singleline (multiline=false): Enter preventDefault + 触 blur → onChange", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="x" isEditMode onChange={onChange} />,
    );
    const el = container.querySelector("span") as HTMLElement;
    el.focus();
    el.textContent = "y";
    const dispatched = fireEvent.keyDown(el, { key: "Enter", code: "Enter" });
    // fireEvent returning false means preventDefault was called
    expect(dispatched).toBe(false);
    expect(onChange).toHaveBeenCalledWith("y");
  });

  it("multiline=true: Enter 不触 blur, onChange 不调", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="x" isEditMode onChange={onChange} multiline />,
    );
    const el = container.querySelector("span") as HTMLElement;
    el.focus();
    el.textContent = "y";
    fireEvent.keyDown(el, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("multiline=true: blur preserves explicit block line breaks", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="Line A" isEditMode onChange={onChange} multiline />,
    );
    const el = container.querySelector("span") as HTMLElement;
    el.innerHTML = "<div>Line A</div><div>Line B</div>";
    fireEvent.blur(el);
    expect(onChange).toHaveBeenCalledWith("Line A\nLine B");
  });

  it("外部 value prop 变 → DOM textContent 同步 (SSE patch 场景)", () => {
    const { container, rerender } = render(
      <InlineEditableText value="initial" isEditMode={false} onChange={() => {}} />,
    );
    const el = container.querySelector("span")!;
    expect(el.textContent).toBe("initial");

    rerender(
      <InlineEditableText value="updated" isEditMode={false} onChange={() => {}} />,
    );
    expect(el.textContent).toBe("updated");
  });

  it("as='div' → 渲 div tag", () => {
    const { container } = render(
      <InlineEditableText value="x" isEditMode onChange={() => {}} as="div" />,
    );
    expect(container.querySelector("div")).not.toBeNull();
    expect(container.querySelector("span")).toBeNull();
  });
});

describe("批 B (P1-2): pending inline 编辑记账", () => {

  it("输入即登记 pending; blur 提交后清账", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="hello" isEditMode onChange={onChange} as="span" />,
    );
    const el = container.querySelector("span")!;
    el.textContent = "hello!";
    fireEvent.input(el);
    expect(pendingInlineEditCount()).toBe(1);
    fireEvent.blur(el);
    expect(onChange).toHaveBeenCalledWith("hello!");
    expect(pendingInlineEditCount()).toBe(0);
  });

  it("值改回原样 → pending 清除（不留幽灵未保存）", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="hello" isEditMode onChange={onChange} as="span" />,
    );
    const el = container.querySelector("span")!;
    el.textContent = "hellox";
    fireEvent.input(el);
    expect(pendingInlineEditCount()).toBe(1);
    el.textContent = "hello";
    fireEvent.input(el);
    expect(pendingInlineEditCount()).toBe(0);
  });

  it("commitPendingInlineEdits 把未 blur 的输入落进 onChange（flush 前收口）", () => {
    const onChange = vi.fn();
    const { container } = render(
      <InlineEditableText value="Total Revenue" isEditMode onChange={onChange} as="div" />,
    );
    const el = container.querySelector("div")!;
    el.textContent = "Total★ Revenue";
    fireEvent.input(el);
    expect(onChange).not.toHaveBeenCalled(); // No blur yet, so PM doc does not know
    commitPendingInlineEdits();
    expect(onChange).toHaveBeenCalledWith("Total★ Revenue");
    expect(pendingInlineEditCount()).toBe(0);
  });

  it("卸载时清 pending（不 commit——NodeView 卸载多为 setContent 重建）", () => {
    const onChange = vi.fn();
    const { container, unmount } = render(
      <InlineEditableText value="v" isEditMode onChange={onChange} as="span" />,
    );
    const el = container.querySelector("span")!;
    el.textContent = "v2";
    fireEvent.input(el);
    expect(pendingInlineEditCount()).toBe(1);
    unmount();
    expect(pendingInlineEditCount()).toBe(0);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("design r2 四态 (affordance r3 §5.4 补欠账)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("编辑态挂 rc-ed + data-tip「点击修改」; 只读态两者皆无", () => {
    const { container, rerender } = render(
      <InlineEditableText value="v" isEditMode onChange={() => {}} className="lbl" />,
    );
    const el = container.querySelector("span")!;
    expect(el.className).toBe("lbl rc-ed");
    expect(el.getAttribute("data-tip")).toBe("点击修改");
    rerender(
      <InlineEditableText value="v" isEditMode={false} onChange={() => {}} className="lbl" />,
    );
    expect(el.className).toBe("lbl");
    expect(el.hasAttribute("data-tip")).toBe(false);
  });

  it("blur 提交后 rc-ed-done 亮 300ms 熄灭（就近微光）", () => {
    const { container } = render(
      <InlineEditableText value="a" isEditMode onChange={() => {}} />,
    );
    const el = container.querySelector("span")!;
    el.textContent = "b";
    fireEvent.blur(el);
    expect(el.className).toContain("rc-ed-done");
    act(() => vi.advanceTimersByTime(299));
    expect(el.className).toContain("rc-ed-done");
    act(() => vi.advanceTimersByTime(2));
    expect(el.className).not.toContain("rc-ed-done");
  });

  it("内容未变的 blur 不触发微光（无提交无反馈）", () => {
    const { container } = render(
      <InlineEditableText value="a" isEditMode onChange={() => {}} />,
    );
    const el = container.querySelector("span")!;
    fireEvent.blur(el);
    expect(el.className).not.toContain("rc-ed-done");
  });
});
