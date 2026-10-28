import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import type { ConfigDraftResult } from "@/lib/config-drafts-api";

const draftMock = vi.fn();
vi.mock("@/lib/config-drafts-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/config-drafts-api")>();
  return { ...original, createConfigDraft: (...args: unknown[]) => draftMock(...args) };
});

import { AiFillPanel } from "./ai-fill-panel";

const OK_RESULT: ConfigDraftResult = {
  status: "ok",
  fields: { name: "月度经营月报", cron: "0 10 1 * *", query: "汇总上月经营情况", tz: "Asia/Shanghai", notify_channels: { email: ["a@b.com"] }, workspace_id: "ws_1" },
  field_meta: {
    name: { origin: "generated", needs_confirm: false, note: null },
    cron: { origin: "generated", needs_confirm: true, note: "平台暂不支持原周期（最短每小时），已调整为每小时" },
    query: { origin: "generated", needs_confirm: false, note: null },
    tz: { origin: "generated", needs_confirm: false, note: null },
    notify_channels: { origin: "generated", needs_confirm: false, note: null },
    workspace_id: { origin: "server", needs_confirm: false, note: null },
  },
  issues: [],
  clarification: null,
  secret_redacted: false,
  form_id: "scheduled_job",
};

function setup(overrides: Partial<Parameters<typeof AiFillPanel>[0]> = {}) {
  const onApply = vi.fn();
  const onClear = vi.fn();
  const onAcceptAll = vi.fn();
  renderIntl(
    <AiFillPanel
      formId="scheduled_job"
      getCurrentValues={() => ({ workspace_id: "ws_1" })}
      onApply={onApply}
      onClear={onClear}
      onAcceptAll={onAcceptAll}
      {...overrides}
    />,
  );
  return { onApply, onClear, onAcceptAll };
}

const typeAndGenerate = async (text: string) => {
  fireEvent.change(screen.getByPlaceholderText("用一句话描述这个任务，或粘贴相关资料…"), { target: { value: text } });
  fireEvent.click(screen.getByTestId("afp-generate"));
};

afterEach(() => {
  cleanup();
  draftMock.mockReset();
});

describe("AiFillPanel", () => {
  it("初始态：标题+能力说明，空描述时生成禁用", () => {
    setup();
    expect(screen.getByText("智能填写")).toBeTruthy();
    expect(screen.getByText("AI 会填好下面的表单，保存前都可以改")).toBeTruthy();
    expect((screen.getByTestId("afp-generate") as HTMLButtonElement).disabled).toBe(true);
  });

  it("生成成功：调 API（form_id+current_values）→ onApply + 摘要行（含需确认计数）", async () => {
    draftMock.mockResolvedValueOnce(OK_RESULT);
    const { onApply } = setup();
    await typeAndGenerate("每月1号早上10点出月报");
    await waitFor(() => expect(onApply).toHaveBeenCalledWith(OK_RESULT));
    expect(draftMock).toHaveBeenCalledWith({
      form_id: "scheduled_job",
      text: "每月1号早上10点出月报",
      current_values: { workspace_id: "ws_1" },
    });
    const sum = screen.getByTestId("afp-sum");
    expect(sum.textContent).toContain("已填写 5 个字段，");
    expect(sum.textContent).toContain("1 个需确认");
    expect(screen.getByText("全部接受")).toBeTruthy();
  });

  it("fallback：失败一行字可重新生成，onApply 不被调（表单不受影响）", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "fallback", fields: {}, field_meta: {} });
    const { onApply } = setup();
    await typeAndGenerate("x");
    await waitFor(() => expect(screen.getByText("生成没成功，你可以直接手动填写")).toBeTruthy());
    expect(onApply).not.toHaveBeenCalled();
    expect((screen.getByTestId("afp-generate") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("重新生成")).toBeTruthy();
  });

  it("429 频控：limit 文案 + 按钮禁用", async () => {
    draftMock.mockRejectedValueOnce(new ApiError(429, "rate limited"));
    setup();
    await typeAndGenerate("x");
    await waitFor(() => expect(screen.getByText("本小时智能填写次数已用完，稍后再试")).toBeTruthy());
    expect((screen.getByTestId("afp-generate") as HTMLButtonElement).disabled).toBe(true);
  });

  it("422 注入拒绝：明确文案，可改后重试", async () => {
    draftMock.mockRejectedValueOnce(new ApiError(422, "rejected"));
    setup();
    await typeAndGenerate("x");
    await waitFor(() => expect(screen.getByText("内容包含不支持的指令片段，请调整后重试")).toBeTruthy());
    expect((screen.getByTestId("afp-generate") as HTMLButtonElement).disabled).toBe(false);
  });

  it("澄清单轮：问题内联展示，回答并入原文重新生成", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "clarification", fields: {}, field_meta: {}, clarification: "改为每周一，还是按每月 1、15 号近似？" });
    draftMock.mockResolvedValueOnce(OK_RESULT);
    const { onApply } = setup();
    await typeAndGenerate("隔周周一发关键指标");
    await waitFor(() => expect(screen.getByTestId("afp-ask")).toBeTruthy());
    expect(screen.getByText("还需要确认一件事")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("回答一句就行…"), { target: { value: "每周一吧" } });
    fireEvent.click(screen.getByText("继续生成"));
    await waitFor(() => expect(onApply).toHaveBeenCalled());
    expect(draftMock.mock.calls[1][0].text).toBe("隔周周一发关键指标\n每周一吧");
  });
});

describe("AiFillPanel — 密钥提示（批 1-c）", () => {
  it("secret_redacted=true：提示密钥已隐去、请手填（否则相关字段空着无人解释）", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, secret_redacted: true });
    setup();
    await typeAndGenerate("按这份 README 配置：API_KEY=sk-FAKE1234567890abcdefFAKE9d0e1234");
    await waitFor(() => expect(screen.getByTestId("afp-secret-note")).toBeTruthy());
    expect(screen.getByText("资料里的密钥已隐去，未发送给模型；相关字段请自己填写。")).toBeTruthy();
  });

  it("secret_redacted=false：不出提示（无密钥时不制造噪音）", async () => {
    draftMock.mockResolvedValueOnce(OK_RESULT);
    setup();
    await typeAndGenerate("每月1号早上10点出月报");
    await waitFor(() => expect(screen.getByTestId("afp-sum")).toBeTruthy());
    expect(screen.queryByTestId("afp-secret-note")).toBeNull();
  });

  it("降级轮同样提示——脱敏在生成之前，与本轮 status 无关", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "fallback", secret_redacted: true });
    setup();
    await typeAndGenerate("API_KEY=sk-FAKE1234567890abcdefFAKE9d0e1234");
    await waitFor(() => expect(screen.getByTestId("afp-secret-note")).toBeTruthy());
  });

  it("清除建议后提示消失", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, secret_redacted: true });
    setup();
    await typeAndGenerate("API_KEY=sk-FAKE1234567890abcdefFAKE9d0e1234");
    await waitFor(() => expect(screen.getByTestId("afp-secret-note")).toBeTruthy());
    fireEvent.click(screen.getByText("清除建议"));
    expect(screen.queryByTestId("afp-secret-note")).toBeNull();
  });
});

describe("AiFillPanel — 密钥旁路与按面降级文案（批 1-d）", () => {
  const SECRETS = [{ value: "Bearer sk-FAKE1234567890abcdefFAKE9d0e1234", label: "Authorization", kind: "kv" }];

  it("includeSecrets 才向服务端索取原值（默认不索取）", async () => {
    draftMock.mockResolvedValueOnce(OK_RESULT);
    setup();
    await typeAndGenerate("配一下");
    await waitFor(() => expect(draftMock).toHaveBeenCalled());
    expect(draftMock.mock.calls[0][0]).not.toHaveProperty("include_secrets");

    cleanup();
    draftMock.mockReset();
    draftMock.mockResolvedValueOnce(OK_RESULT);
    setup({ includeSecrets: true });
    await typeAndGenerate("配一下");
    await waitFor(() => expect(draftMock).toHaveBeenCalled());
    expect(draftMock.mock.calls[0][0].include_secrets).toBe(true);
  });

  it("拿到 secrets 就回调宿主直填（原值不经模型，直接回本浏览器）", async () => {
    const onSecrets = vi.fn();
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, secret_redacted: true, secrets: SECRETS });
    setup({ includeSecrets: true, onSecrets });
    await typeAndGenerate("按这份 curl 配置");
    await waitFor(() => expect(onSecrets).toHaveBeenCalledWith(SECRETS));
  });

  it("降级轮同样回吐——那恰恰是最需要「直接填进去」的时候", async () => {
    const onSecrets = vi.fn();
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "fallback", secrets: SECRETS });
    setup({ includeSecrets: true, onSecrets });
    await typeAndGenerate("按这份 curl 配置");
    await waitFor(() => expect(onSecrets).toHaveBeenCalled());
  });

  it("failMessageKey 覆写降级文案——共享的「生成没成功」在 provider 页语义太弱", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "fallback" });
    setup({ failMessageKey: "fail_provider" });
    await typeAndGenerate("配一下");
    await waitFor(() => expect(screen.getByText("当前模型连不通，智能填写用不了；请手动填写后点「测试连接」")).toBeTruthy());
  });

  it("不传 failMessageKey 时用共享文案", async () => {
    draftMock.mockResolvedValueOnce({ ...OK_RESULT, status: "fallback" });
    setup();
    await typeAndGenerate("配一下");
    await waitFor(() => expect(screen.getByText("生成没成功，你可以直接手动填写")).toBeTruthy());
  });
});
