/**
 * <FinalFailBanner> — the turn's one failure card (Chat Cards Spec §7, D3/D5).
 */
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { FinalFailBanner } from "@/components/chat/rho/final-fail-banner";
import zhMessages from "@/messages/zh-CN.json";

const clipboard = vi.hoisted(() => ({ copy: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyTextWithFallback: (...args: unknown[]) => clipboard.copy(...args) }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/lib/use-toast", () => ({ toast }));

afterEach(() => {
  cleanup();
  clipboard.copy.mockReset();
  toast.mockReset();
});

const card = () => screen.getByTestId("final-fail");
const primaryButtons = () => card().querySelectorAll(".settings-button--primary");

describe("FinalFailBanner", () => {
  it("shows the cause and next step without opening technical details", () => {
    renderIntl(<FinalFailBanner kind="llm_request_rejected" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      providerMessage: "Model deepseek-v4.1-flash does not exist",
    }} />);

    expect(card()).toHaveTextContent("问题判断：模型服务拒绝了本次请求");
    expect(card()).toHaveTextContent("建议操作：请联系团队管理员检查模型连接和请求参数");
    expect(card()).toHaveTextContent("Model deepseek-v4.1-flash does not exist");
    fireEvent.click(screen.getByRole("button", { name: "技术详情" }));
    expect(card()).toHaveTextContent("模型服务原始报错（已过滤敏感内容）");
    expect(card()).toHaveTextContent("Model deepseek-v4.1-flash does not exist");
  });

  it("shows a rejected API key as an authentication problem with expandable provider detail", () => {
    renderIntl(<FinalFailBanner kind="llm_provider_auth_failed" turnId="t_invalid_key" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      code: "llm_provider_auth_failed", turnId: "t_invalid_key",
      safeDetail: "phase=stream; failure_class=access; http_status=401; upstream_code=invalid_api_key",
      providerMessage: "Invalid API key",
    }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("问题判断：模型连接的 API 密钥被拒绝");
    expect(card()).toHaveTextContent("建议操作：请联系团队管理员更新模型连接的 API 密钥");
    expect(card()).toHaveTextContent("Invalid API key");
    fireEvent.click(screen.getByRole("button", { name: "技术详情" }));
    expect(card()).toHaveTextContent("Invalid API key");
  });

  it.each(["assertive", "off"] as const)("shows Responses HTTP 400 facts and conditional advice in live and history (%s)", (live) => {
    renderIntl(<FinalFailBanner kind="llm_request_rejected" turnId="t_model" live={live} recoveryAction="copy_diagnostics" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed", code: "llm_request_rejected", turnId: "t_model",
      safeDetail: "phase=invoke; failure_class=request; http_status=400; api_surface=responses; outcome=unknown",
      providerMessage: "Invalid request body", retryable: false,
    }} />);
    expect(card()).toHaveTextContent("模型服务返回 HTTP 400");
    expect(card()).toHaveTextContent("Invalid request body");
    expect(card()).toHaveTextContent("如果该网关或模型不支持 Responses");
    expect(card()).toHaveTextContent("改为 Chat Completions");
    expect(card()).toHaveTextContent("系统已停止自动重试");
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  });

  it("keeps authentication wording when HTTP 401 does not identify the API key", () => {
    renderIntl(<FinalFailBanner kind="llm_provider_auth_failed" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      code: "llm_provider_auth_failed", safeDetail: "phase=stream; failure_class=access; http_status=401",
    }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("问题判断：模型服务认证失败");
    expect(card()).not.toHaveTextContent("API 密钥被拒绝");
  });

  it("explains a provider-rejected API key in English", () => {
    renderIntl(<FinalFailBanner kind="llm_provider_auth_failed" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      code: "llm_provider_auth_failed", safeDetail: "phase=stream; failure_class=access; http_status=401; upstream_code=invalid_api_key",
      providerMessage: "Invalid API key",
    }} />, { locale: "en-US" });
    expect(screen.getByRole("alert")).toHaveTextContent("The model service rejected the connection's API key");
    expect(card()).toHaveTextContent("update the model connection's API key");
    expect(card()).toHaveTextContent("Invalid API key");
  });

  it.each([
    ["llm_provider_unavailable", "failure_class=network; http_status=none", "暂时无法连接模型服务", "模型服务地址和网络连接"],
    ["llm_provider_auth_failed", "failure_class=access; http_status=403", "模型服务拒绝了当前账号的访问", "是否有权使用所选模型"],
    ["llm_request_rejected", "failure_class=request; http_status=404", "模型服务找不到请求的模型或接口", "核对模型 ID 和接口地址"],
    ["llm_request_rejected", "failure_class=request; http_status=400; upstream_param=temperature", "模型服务不接受本次请求的某个参数", "检查模型连接的请求参数"],
  ] as const)("explains a published %s failure with its specific next step", (kind, safeDetail, title, step) => {
    renderIntl(<FinalFailBanner kind={kind} turnId="t_model" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      code: kind, turnId: "t_model", safeDetail,
    }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(`问题判断：${title}`);
    expect(card()).toHaveTextContent(`建议操作：`);
    expect(card()).toHaveTextContent(step);
    if (safeDetail.includes("upstream_param=temperature")) expect(card()).toHaveTextContent("被拒绝的参数：temperature");
  });

  it.each([
    ["t_other", "chat_llm_failed", "模型服务拒绝了本次请求"],
    ["t_model", "chat_turn_failed", "模型服务拒绝了本次请求"],
  ] as const)("does not infer a model cause from unrelated diagnostics", (publishedTurn, diagnosticCode, title) => {
    renderIntl(<FinalFailBanner kind="llm_request_rejected" turnId="t_model" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", code: "llm_request_rejected",
      turnId: publishedTurn, diagnosticCode, safeDetail: "phase=invoke; failure_class=request; http_status=404",
    }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(`问题判断：${title}`);
    expect(card()).not.toHaveTextContent("模型服务找不到请求的模型或接口");
  });

  it("keeps the broad reason when the diagnostic omits its code", () => {
    renderIntl(<FinalFailBanner kind="llm_request_rejected" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      safeDetail: "phase=invoke; failure_class=request; http_status=404",
    }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("问题判断：模型服务拒绝了本次请求");
  });

  it.each([
    ["en-US", "clarification_capture", "The follow-up question could not be prepared", "The question was not created"],
    ["zh-CN", "clarification_capture", "未能准备好需要你回答的问题", "这次问题没有创建"],
    ["en-US", "clarification_resume_preflight", "The original task could not resume", "checks before continuing"],
    ["zh-CN", "clarification_resume_preflight", "原任务未能恢复执行", "继续前的检查"],
    ["de-DE", "clarification_capture", "Die Rückfrage konnte nicht vorbereitet werden", "Die Frage wurde nicht erstellt"],
    ["fr-FR", "clarification_capture", "La question de suivi n’a pas pu être préparée", "La question n’a pas été créée"],
    ["de-DE", "clarification_resume_preflight", "Die ursprüngliche Aufgabe konnte nicht fortgesetzt werden", "Prüfungen vor der Fortsetzung"],
    ["fr-FR", "clarification_resume_preflight", "La tâche initiale n’a pas pu reprendre", "vérifications avant la reprise"],
  ] as const)("explains the recorded %s %s stage without inferring a saved answer", async (locale, phase, title, body) => {
    clipboard.copy.mockResolvedValue(true);
    const diagnostics = { schemaVersion: 1 as const, disclosure: "authenticated" as const,
      code: "clarification_resume_unavailable", turnId: "t_native", occurrenceId: "a".repeat(32),
      diagnosticCode: phase === "clarification_capture" ? "chat_turn_failed" : "rule_clarification_failure",
      safeDetail: `phase=${phase}; failure_class=${phase === "clarification_capture" ? "configuration" : "state"}`, retryable: false };
    renderIntl(<FinalFailBanner kind="clarification_resume_unavailable" turnId="t_native" diagnostics={diagnostics} recoveryAction="copy_diagnostics" />, { locale });
    expect(card()).toHaveTextContent(title);
    expect(card()).toHaveTextContent(body);
    const savedClaim = { "zh-CN": "回答已保存", "en-US": "answer was saved", "de-DE": "Ihre Antwort wurde gespeichert", "fr-FR": "Votre réponse a été enregistrée" };
    expect(card()).not.toHaveTextContent(savedClaim[locale]);
    expect(screen.queryByRole("button", { name: /^(Retry|重试)$/ })).toBeNull();
    fireEvent.click(screen.getByTestId("final-fail-copy-diagnostics"));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
    expect(clipboard.copy.mock.calls[0][0]).toContain(`phase=${phase}`);
  });

  it.each(["en-US", "zh-CN"] as const)("keeps saved-answer authority distinct from recorded resume checks (%s)", (locale) => {
    renderIntl(<FinalFailBanner kind="clarification_resume_unavailable" turnId="t_resume" clarificationAnswerState="saved"
      diagnostics={{schemaVersion: 1, disclosure: "authenticated", code: "clarification_resume_unavailable", turnId: "t_resume",
        diagnosticCode: "rule_clarification_failure", safeDetail: "phase=clarification_resume_preflight; failure_class=state", retryable: false}}
      recoveryAction="copy_diagnostics" />, {locale});
    expect(card()).toHaveTextContent(locale === "zh-CN" ? "继续前的检查" : "checks before continuing");
    expect(card()).toHaveTextContent(locale === "zh-CN" ? "你的回答已保存" : "Your answer was saved");
    expect(primaryButtons()).toHaveLength(1);
  });

  it.each(["phase=chat; failure_class=configuration", "phase=clarification_capture; failure_class=state", "phase=clarification_capture; failure_class=configuration; secret=raw"])("keeps unknown or invalid continuation facts honest: %s", (safeDetail) => {
    renderIntl(<FinalFailBanner kind="clarification_resume_unavailable" diagnostics={{ schemaVersion: 1, disclosure: "authenticated",
      code: "clarification_resume_unavailable", diagnosticCode: "chat_turn_failed", safeDetail, retryable: false }} />);
    expect(card()).toHaveTextContent("具体失败环节未记录");
    expect(card()).not.toHaveTextContent("这次问题没有创建");
  });

  it.each([
    ["en-US", "Your answer was saved", "Whether your answer was saved is not yet confirmed"],
    ["zh-CN", "你的回答已保存", "暂时无法确认你的回答是否已保存"],
  ] as const)("explains confirmed and unknown clarification outcomes with copied HTTP diagnostics (%s)", async (locale, saved, unconfirmed) => {
    clipboard.copy.mockResolvedValue(true);
    const diagnostics = {
      schemaVersion: 1 as const, disclosure: "authenticated" as const,
      code: "clarification_resume_unavailable", diagnosticCode: "rule_clarification_failure",
      safeDetail: "phase=state_change; failure_class=state", retryable: false,
      requestId: "0123456789abcdef0123456789abcdef",
    };
    const props = { kind: "clarification_resume_unavailable" as const, turnId: "t_original",
      diagnostics, recoveryAction: "copy_diagnostics" as const, onRetry: vi.fn() };
    const rendered = renderIntl(<FinalFailBanner {...props} clarificationAnswerState="unconfirmed" />, { locale });
    expect(card()).toHaveTextContent(unconfirmed);
    expect(screen.queryByRole("button", { name: /^(重试|Retry)$/ })).toBeNull();
    rendered.unmount();
    renderIntl(<FinalFailBanner {...props} clarificationAnswerState="saved" />, { locale });
    expect(card()).toHaveTextContent(saved);
    expect(primaryButtons()).toHaveLength(1);
    fireEvent.click(screen.getByTestId("final-fail-copy-diagnostics"));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalledOnce());
    const copied = String(clipboard.copy.mock.calls[0][0]);
    expect(copied).toContain("diagnostic_code: rule_clarification_failure");
    expect(copied).toContain("request_id: 0123456789abcdef0123456789abcdef");
    expect(copied).toContain("safe_detail: phase=state_change; failure_class=state");
    expect(props.onRetry).not.toHaveBeenCalled();
  });

  it("is a failure card: eyebrow, title announced once, body, one primary action", () => {
    renderIntl(<FinalFailBanner kind="llm_provider_timeout" recoveryAction="retry" onRetry={vi.fn()} />);
    expect(card().className).toContain("ui-card--failure");
    expect(within(card()).getByText("本轮没有完成")).toBeTruthy();
    expect(screen.getByRole("alert")).toHaveTextContent("等待模型回复超时");
    expect(card()).toHaveTextContent("模型未在规定时间内完成回复，本轮未能完成。可以稍后重试。");
    expect(primaryButtons()).toHaveLength(1);
  });

  it("does not announce a replayed failure again", () => {
    renderIntl(<FinalFailBanner kind="llm_provider_timeout" live="off" />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(card()).toHaveTextContent("等待模型回复超时");
  });

  it.each([
    ["wallclock_exceeded", "本轮处理时间已达到上限", "可以缩小范围，或拆成几个请求分别处理。"],
    ["hop_cap_exceeded", "本轮已达到执行步骤上限", "请拆分剩余工作后重新发送。"],
    ["llm_context_overflow", "对话内容超过了模型能处理的上限", "请新建会话，缩短问题或减少材料"],
    ["llm_provider_auth_failed", "模型服务认证失败", "请联系团队管理员核对模型连接的 API 密钥和访问权限，并提供诊断信息；修正后再发送。"],
    ["reference_policy_changed", "团队权限变了，这次引用没有执行", "重新选择智能体和技能后再发送。"],
    ["service_interrupted", "服务执行中断，本轮未完成", "请先核对已显示的步骤和结果"],
    ["terminal_projection_not_committed", "本轮结果未能完整保存", "请先核对已显示的结果"],
    ["lead_internal_error", "系统处理出错，本轮未完成", "请重试；如果再次发生，请复制诊断信息联系管理员。"],
  ] as const)("maps %s to what happened and what to do", (kind, title, body) => {
    renderIntl(<FinalFailBanner kind={kind} />);
    expect(screen.getByRole("alert")).toHaveTextContent(title);
    expect(card()).toHaveTextContent(body);
    expect(card()).not.toHaveTextContent(kind);
    expect(card()).not.toHaveTextContent("可能是");
    expect(card()).not.toHaveTextContent("原因暂时无法确定");
  });

  it("keeps the specific sentence of a reason outside the table", () => {
    renderIntl(<FinalFailBanner kind="access_context_missing" />);
    expect(screen.getByRole("alert")).toHaveTextContent(zhMessages.error_messages.reasons.access_context_missing);
  });

  it.each([
    ["zh-CN", "document_information_incomplete", "文档没有完成", "回答后仍未生成可审阅草稿"],
    ["en-US", "document_planning_failed", "The document didn't finish", "The document task could not finish and no files were published"],
  ] as const)("shows the document outcome without a system-error claim (%s/%s)", (locale, kind, eyebrow, message) => {
    renderIntl(<FinalFailBanner kind={kind} />, { locale });
    expect(card()).toHaveTextContent(eyebrow);
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByRole("button", { name: /^(重试|Retry)$/ })).toBeNull();
  });

  it.each([
    ["user_credit_exhausted", "你的可用额度已用完"],
    ["team_pool_exhausted", "团队的可用额度已用完"],
  ] as const)("honors the published copy action for exhausted credits (%s)", async (kind, title) => {
    clipboard.copy.mockResolvedValue(true);
    const onRetry = vi.fn();
    renderIntl(<FinalFailBanner kind={kind} recoveryAction="copy_diagnostics" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent(title);
    fireEvent.click(screen.getByTestId("final-fail-copy-diagnostics"));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalledOnce());
    expect(String(clipboard.copy.mock.calls[0][0])).toContain(`error_code: ${kind}`);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it.each([
    ["llm_provider_unavailable", "The model service is temporarily unavailable"],
    ["llm_provider_timeout", "Timed out waiting for the model"],
    ["llm_provider_rate_limited", "The model service limited this request"],
    ["llm_stream_interrupted", "The model response was interrupted"],
    ["llm_response_invalid", "The model response could not be processed"],
    ["llm_provider_auth_failed", "Model service authentication failed"],
    ["llm_request_rejected", "The model service rejected this request"],
    ["llm_stream_deadline_exceeded", "This model step reached its time limit"],
    ["plan_runtime_unavailable", "The plan execution service is unavailable"],
    ["single_write_leaf_violation", "This change did not pass the execution checks"],
  ] as const)("preserves the specific cause for %s", (kind, title) => {
    renderIntl(<FinalFailBanner kind={kind} />, { locale: "en-US" });
    expect(screen.getByRole("alert")).toHaveTextContent(title);
  });

  it("explains continuation uncertainty and the new-request action", () => {
    const edit = vi.fn();
    renderIntl(<FinalFailBanner kind="clarification_resume_unavailable" recoveryAction="edit_and_resend" onEditAndResend={edit} />);
    expect(card()).toHaveTextContent("具体失败环节未记录");
    expect(card()).toHaveTextContent("这会发起新请求，不会接着原任务执行");
    fireEvent.click(screen.getByRole("button", { name: "编辑后发送" }));
    expect(edit).toHaveBeenCalledOnce();
  });

  it("collapses technical details: readable facts only, the code and turn id only in the copied text", async () => {
    clipboard.copy.mockResolvedValue(true);
    renderIntl(
      <FinalFailBanner
        kind="lead_internal_error"
        turnId="t_safe_123"
        diagnostics={{
          schemaVersion: 1,
          disclosure: "authenticated",
          code: "lead_internal_error",
          diagnosticCode: "chat_turn_failed",
          occurrenceId: "0123456789abcdef0123456789abcdef",
          observedAt: "2026-10-05T06:23:05.000Z",
          safeDetail: "phase=chat; failure_class=timeout",
          retryable: true,
        }}
      />,
    );
    expect(card()).not.toHaveTextContent("lead_internal_error");
    expect(card()).not.toHaveTextContent("t_safe_123");
    fireEvent.click(screen.getByRole("button", { name: "技术详情" }));
    expect(card()).toHaveTextContent("对话");
    expect(card()).not.toHaveTextContent("0123456789abcdef0123456789abcdef");
    expect(card()).not.toHaveTextContent("phase=chat");
    const copy = screen.getByRole("button", { name: "复制诊断信息" });
    expect(screen.getByRole("alert").contains(copy)).toBe(false);
    fireEvent.click(copy);
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
    const bundle = String(clipboard.copy.mock.calls[0][0]);
    expect(bundle).toContain("error_code: lead_internal_error");
    expect(bundle).toContain("t_safe_123");
  });

  it("shows a recorded model stage and fills the current code in copied diagnostics", async () => {
    clipboard.copy.mockResolvedValue(true);
    renderIntl(<FinalFailBanner kind="llm_stream_interrupted" turnId="t_current" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      safeDetail: "phase=stream; failure_class=transport", retryable: true,
    }} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Technical details" }));
    expect(card()).toHaveTextContent("Chat · Model response");
    expect(card()).toHaveTextContent("Receiving the model response");
    fireEvent.click(screen.getByRole("button", { name: "Copy diagnostic information" }));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
    expect(String(clipboard.copy.mock.calls[0][0])).toContain("error_code: llm_stream_interrupted");
  });

  it.each([
    { code: "llm_provider_timeout", turnId: "t_current" },
    { code: "llm_stream_interrupted", turnId: "t_other" },
  ])("rejects diagnostic identity mismatch: %j", async (identity) => {
    clipboard.copy.mockResolvedValue(true);
    renderIntl(<FinalFailBanner kind="llm_stream_interrupted" turnId="t_current" diagnostics={{
      schemaVersion: 1, disclosure: "authenticated", diagnosticCode: "chat_llm_failed",
      occurrenceId: "0123456789abcdef0123456789abcdef", safeDetail: "phase=stream; failure_class=transport",
      ...identity,
    }} />);
    fireEvent.click(screen.getByRole("button", { name: "技术详情" }));
    fireEvent.click(screen.getByRole("button", { name: "复制诊断信息" }));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
    const bundle = String(clipboard.copy.mock.calls[0][0]);
    expect(bundle).not.toContain("0123456789abcdef0123456789abcdef");
    expect(bundle).not.toContain("phase=stream");
    expect(bundle).toContain("turn_id: t_current");
  });

  it("rejects diagnostics from an unrelated publication policy (keeps only the code and turn id)", async () => {
    clipboard.copy.mockResolvedValue(true);
    renderIntl(
      <FinalFailBanner
        kind="lead_internal_error"
        turnId="t_safe_123"
        diagnostics={{
          schemaVersion: 1,
          disclosure: "authenticated",
          diagnosticCode: "powerbi_query_failure",
          occurrenceId: "0123456789abcdef0123456789abcdef",
          turnId: "t_safe_123",
          retryable: true,
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "技术详情" }));
    fireEvent.click(screen.getByRole("button", { name: "复制诊断信息" }));
    await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
    const bundle = String(clipboard.copy.mock.calls[0][0]);
    expect(bundle).not.toContain("0123456789abcdef0123456789abcdef");
    expect(bundle).toContain("t_safe_123");
  });

  describe("recovery action (chat turn feedback D5)", () => {
    const handlers = () => ({ onRetry: vi.fn(), onEditAndResend: vi.fn(), onNewSession: vi.fn() });

    it.each([
      ["retry", "重试", "onRetry"],
      ["edit_and_resend", "编辑后发送", "onEditAndResend"],
      ["new_session", "新建会话", "onNewSession"],
    ] as const)("shows exactly one primary action for %s", (action, label, handler) => {
      const h = handlers();
      renderIntl(<FinalFailBanner kind="lead_internal_error" recoveryAction={action} {...h} />);
      expect(primaryButtons()).toHaveLength(1);
      expect(primaryButtons()[0]).toHaveTextContent(label);
      fireEvent.click(screen.getByRole("button", { name: label }));
      expect(h[handler]).toHaveBeenCalledOnce();
    });

    it("offers Edit and send as the secondary way forward after Retry", () => {
      const h = handlers();
      renderIntl(<FinalFailBanner kind="lead_internal_error" recoveryAction="retry" {...h} />);
      const edit = screen.getByRole("button", { name: "编辑后发送" });
      expect(edit.className).not.toContain("settings-button--primary");
      fireEvent.click(edit);
      expect(h.onEditAndResend).toHaveBeenCalledOnce();
    });

    it("copy_diagnostics is the primary action and confirms with a toast", async () => {
      clipboard.copy.mockResolvedValue(true);
      renderIntl(
        <FinalFailBanner kind="access_context_missing" turnId="t_safe_1" recoveryAction="copy_diagnostics" {...handlers()} />,
      );
      expect(primaryButtons()).toHaveLength(1);
      fireEvent.click(screen.getByTestId("final-fail-copy-diagnostics"));
      await waitFor(() => expect(clipboard.copy).toHaveBeenCalled());
      expect(String(clipboard.copy.mock.calls[0][0])).toContain("t_safe_1");
      await waitFor(() => expect(toast).toHaveBeenCalledWith({ kind: "success", text: "已复制诊断信息" }));
    });

    it("degrades retry to edit and resend when the surface cannot replay (archived turn)", () => {
      const onEditAndResend = vi.fn();
      renderIntl(<FinalFailBanner kind="lead_internal_error" recoveryAction="retry" onEditAndResend={onEditAndResend} />);
      expect(primaryButtons()[0]).toHaveTextContent("编辑后发送");
      fireEvent.click(screen.getByRole("button", { name: "编辑后发送" }));
      expect(onEditAndResend).toHaveBeenCalledOnce();
      expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
    });

    it.each([
      ["en-US", "New session"],
      ["de-DE", "Neue Sitzung"],
      ["fr-FR", "Nouvelle session"],
    ] as const)("localizes the new-session action (%s)", (locale, label) => {
      renderIntl(<FinalFailBanner kind="llm_context_overflow" recoveryAction="new_session" {...handlers()} />, { locale });
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    });
  });

  describe("plan_blocked resumes the plan", () => {
    it("offers Resume Plan as the primary action and Edit and send as the secondary one", async () => {
      const onResume = vi.fn(async () => true);
      const onEdit = vi.fn();
      renderIntl(
        <FinalFailBanner
          kind="plan_blocked"
          recoveryAction="edit_and_resend"
          onEditAndResend={onEdit}
          resumePlan={{ available: true, onResume }}
        />,
      );
      expect(primaryButtons()).toHaveLength(1);
      expect(primaryButtons()[0]).toHaveTextContent("继续执行计划");
      expect(screen.getByRole("button", { name: "编辑后发送" })).toBeTruthy();
      fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
      await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
      expect(onEdit).not.toHaveBeenCalled();
      expect(card()).not.toHaveTextContent("计划已经变了");
    });

    it("falls back to Edit and send with the reason when the plan changed or the Space is gone", async () => {
      const onResume = vi.fn(async () => false);
      renderIntl(
        <FinalFailBanner
          kind="plan_blocked"
          recoveryAction="edit_and_resend"
          live="off"
          onEditAndResend={vi.fn()}
          resumePlan={{ available: true, onResume }}
        />,
      );
      fireEvent.click(screen.getByTestId("final-fail-resume-plan"));
      await waitFor(() => expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull());
      expect(primaryButtons()).toHaveLength(1);
      expect(primaryButtons()[0]).toHaveTextContent("编辑后发送");
      expect(screen.getByRole("alert")).toHaveTextContent("计划已经变了，或者当前空间不能再用");
    });

    it("keeps Edit and send when the plan cannot be resumed from this page", () => {
      renderIntl(
        <FinalFailBanner
          kind="plan_blocked"
          recoveryAction="edit_and_resend"
          onEditAndResend={vi.fn()}
          resumePlan={{ available: false, onResume: vi.fn(async () => true) }}
        />,
      );
      expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull();
      expect(primaryButtons()[0]).toHaveTextContent("编辑后发送");
    });

    it("never offers resuming for other reasons", () => {
      renderIntl(
        <FinalFailBanner
          kind="wallclock_exceeded"
          recoveryAction="edit_and_resend"
          onEditAndResend={vi.fn()}
          resumePlan={{ available: true, onResume: vi.fn(async () => true) }}
        />,
      );
      expect(screen.queryByTestId("final-fail-resume-plan")).toBeNull();
    });
  });
});
