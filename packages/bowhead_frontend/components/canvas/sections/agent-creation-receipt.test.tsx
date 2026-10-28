import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { AgentCreationReceipt } from "./agent-creation-receipt";

const SUMMARY = { resources: "1 个插件 · 2 个工具", iterations: 12, visibility: "creator_only" };

function renderReceipt(props: Partial<Parameters<typeof AgentCreationReceipt>[0]> = {}, locale: "zh-CN" | "en-US" = "zh-CN") {
  const handlers = { onRetry: vi.fn(), onManage: vi.fn(), onUse: vi.fn(), onOpenEvaluate: vi.fn(), onOpenWho: vi.fn() };
  renderIntl(<AgentCreationReceipt name="销售汇总助手" visibility="creator_only" summary={SUMMARY} readFailed={false} {...handlers} {...props} />, { locale });
  return handlers;
}

describe("AgentCreationReceipt (Item 2: creating releases v1)", () => {
  afterEach(cleanup);

  it("says a private Agent is released as v1 for the creator and links both next steps", () => {
    const handlers = renderReceipt({}, "en-US");
    expect(screen.getByText("Released v1 · Only you can use it")).toBeTruthy();
    const steps = screen.getByRole("list", { name: "Next steps" });
    expect(steps.textContent).toBe(
      "Try it: Start using opens a chat with it selected."
      + "Want confidence: add cases on Evaluate. To share: change who can use it under Configure › Who can use it; team-wide access may need administrator approval.",
    );
    fireEvent.click(within(steps).getByRole("button", { name: "Evaluate" }));
    fireEvent.click(within(steps).getByRole("button", { name: "Configure › Who can use it" }));
    expect(handlers.onOpenEvaluate).toHaveBeenCalledOnce();
    expect(handlers.onOpenWho).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "Start using" })).toBeTruthy();
  });

  it("names the @-mention instead of a Start using button that isn't rendered", () => {
    renderReceipt({ onUse: undefined });
    expect(screen.queryByRole("button", { name: "开始使用" })).toBeNull();
    const steps = screen.getByRole("list", { name: "下一步" });
    expect(steps.textContent).toContain("先试一试：在对话输入框里输入 @，选择「销售汇总助手」，再提问。");
    expect(steps.textContent).not.toContain("开始使用");
  });

  it("shows what the Agent can use by kind, and the work-turn limit", () => {
    renderReceipt();
    expect(screen.getByText("能用什么")).toBeTruthy();
    expect(screen.getByText("1 个插件 · 2 个工具")).toBeTruthy();
    expect(screen.getByText("最多工作轮次")).toBeTruthy();
    expect(screen.getByText("12 轮")).toBeTruthy();
  });

  it("drops the sharing hint for an Agent created for selected people", () => {
    renderReceipt({ visibility: "custom", summary: { ...SUMMARY, visibility: "custom" } });
    expect(screen.getByText("已发布 v1 · 指定成员可用")).toBeTruthy();
    const steps = screen.getByRole("list", { name: "下一步" });
    expect(steps.textContent).toContain("想更放心：在「评测」里加几条用例。");
    expect(steps.textContent).not.toContain("想分享");
  });

  it("keeps the release line while the saved configuration can't be read", () => {
    renderReceipt({ summary: null, readFailed: true });
    expect(screen.getByText("已发布 v1 · 只有你能用")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重新读取配置" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始使用" })).toBeNull();
  });

  it("shows no release line after editing an existing Agent", () => {
    renderReceipt({ created: false });
    expect(screen.queryByText(/已发布 v1/)).toBeNull();
    expect(screen.queryByRole("list", { name: "下一步" })).toBeNull();
  });
});
