import { act, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { describe, expect, it, vi } from "vitest";

import { VerificationPanel } from "@/components/settings/models/verification-panel";
import { copyTextWithFallback } from "@/lib/clipboard";
import type { VerificationBatch, VerificationDiagnostics, VerificationPlan } from "@/lib/llm-config-v2-api";
import messagesZh from "@/messages/zh-CN.json";

vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: vi.fn() }));

const diagnostics: VerificationDiagnostics = {
  request: { model: "test", protocol: "anthropic_compatible", max_tokens: 8, thinking: { type: "disabled" } },
  response_summary: { model: "test-returned", stop_reason: "max_tokens", usage: { output_tokens: 8 }, content_types: ["thinking"] },
  response_body: '{"content": [], "stop_reason": "max_tokens", "api_key": "[REDACTED]"}',
  response_truncated: true,
};

function show(details: VerificationDiagnostics | null = diagnostics) {
  const plan: VerificationPlan = {
    plan_id: "plan", confirmation_token: "confirmation", expires_at: "2026-11-03T00:00:00Z",
    item_count: 1, provider_request_count: 1, max_concurrency: 1, notices: [],
    items: [{ provider_id: "provider", provider_display_name: "Service", model_id: "test", capability: "chat", testability: "testable", provider_request_count: 1, testability_contract_version: "v1", probe_contract_version: "v1", probe_execution_policy_version: "v1" }],
  };
  const batch: VerificationBatch = {
    batch_id: "batch", plan_id: "plan", state: "completed", revision: 2, base_config_version: 1, counts: { failed: 1 },
    items: [{ item_id: "item", ordinal: 0, provider_id: "provider", model_id: "test", capability: "chat", state: "failed", revision: 2, safe_code: "empty_completion", safe_http_status: 200, diagnostics: details }],
  };
  return render(<NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
    <VerificationPanel plan={plan} batch={batch} busy={false} onConfirm={vi.fn()} onCancel={vi.fn()} onDismiss={vi.fn()} />
  </NextIntlClientProvider>);
}

async function expand() {
  const summary = screen.getByText("查看返回详情");
  expect(summary.closest("details")?.open).toBe(false);
  fireEvent.click(summary);
  await vi.waitFor(() => expect(screen.getByText("实际返回的模型")).toBeTruthy());
}

describe("empty response diagnostics", () => {
  it("explains the result before expanding and copies the same screened excerpt", async () => {
    vi.mocked(copyTextWithFallback).mockResolvedValue(true);
    show();
    expect(screen.getByText("模型服务已响应，但没有可显示的回答。请查看详情，进一步排查原因。")).toBeTruthy();
    expect(screen.queryByText("test-returned")).toBeNull();
    await expand();
    expect(screen.getByText("test-returned")).toBeTruthy();
    expect(screen.getByText("思考内容")).toBeTruthy();
    expect(screen.getByText(/这次测试达到了回复长度上限/)).toBeTruthy();
    expect(screen.getByText(/返回内容超过 64 KiB/)).toBeTruthy();
    expect(screen.getByText(/关闭结果或刷新页面后/)).toBeTruthy();
    expect(screen.getByText(/不含密钥或完整请求/)).toBeTruthy();
    expect(screen.getByText(diagnostics.response_body)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "复制排查详情" }));
    await vi.waitFor(() => expect(screen.getByText("已复制").getAttribute("role")).toBe("status"));
    expect(copyTextWithFallback).toHaveBeenCalledWith(JSON.stringify(diagnostics, null, 2), expect.any(String), JSON.stringify(diagnostics));
  });

  it("shows missing provider fields honestly without inventing a cause", async () => {
    show({ ...diagnostics, response_summary: { model: null, stop_reason: null, usage: null, content_types: [] }, response_truncated: false });
    await expand();
    expect(screen.getAllByText("服务未提供")).toHaveLength(3);
    expect(screen.getByText("未识别到文本、思考内容或工具调用")).toBeTruthy();
    expect(screen.queryByText(/这次测试达到了回复长度上限/)).toBeNull();
    expect(screen.queryByText(/返回内容超过 64 KiB/)).toBeNull();
  });

  it("offers a new verification when older or unavailable diagnostics have no details", () => {
    show(null);
    expect(screen.getByText("这次验证未记录返回详情，请重新验证以获取新的详情。")).toBeTruthy();
    expect(screen.queryByText("查看返回详情")).toBeNull();
  });
  it("does not claim a successful copy when manual copying is required", async () => {
    vi.mocked(copyTextWithFallback).mockClear().mockResolvedValue(false);
    show();
    await expand();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "复制排查详情" })));
    await vi.waitFor(() => expect(copyTextWithFallback).toHaveBeenCalled());
    expect(screen.queryByText("已复制")).toBeNull();
  });

  it.each(["max_output_tokens", "length"])("explains the %s output limit", async (stopReason) => {
    show({ ...diagnostics, response_summary: { ...diagnostics.response_summary, stop_reason: stopReason } });
    await expand();
    expect(screen.getByText(`达到回复长度上限 (${stopReason})`)).toBeTruthy();
    expect(screen.getByText(/这次测试达到了回复长度上限/)).toBeTruthy();
  });

  it("explains a normal stop without guessing that the output limit was reached", async () => {
    show({ ...diagnostics, response_summary: { ...diagnostics.response_summary, stop_reason: "end_turn" } });
    await expand();
    expect(screen.getByText("服务已结束本次回复 (end_turn)")).toBeTruthy();
    expect(screen.queryByText(/这次测试达到了回复长度上限/)).toBeNull();
  });

});
