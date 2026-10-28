import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DiscoveryPicker } from "@/components/settings/models/discovery-picker";
import {
  ModelSettingsOverview,
  safeOverviewEndpoint,
} from "@/components/settings/models/model-settings-overview";
import { ReadinessReview } from "@/components/settings/models/readiness-review";
import { VerificationPanel } from "@/components/settings/models/verification-panel";
import { VerificationSelector } from "@/components/settings/models/verification-selector";
import messagesZh from "@/messages/zh-CN.json";

function renderZh(child: React.ReactNode) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      {child}
    </NextIntlClientProvider>,
  );
}

afterEach(cleanup);

describe("model settings V2 components", () => {
  it("keeps verification modal focus contained and restores the trigger", async () => {
    const trigger = document.createElement("button");
    document.body.append(trigger);
    trigger.focus();
    const onClose = vi.fn();
    const view = renderZh(
      <VerificationSelector
        candidates={[{
          providerId: "llmp_1",
          providerName: "corp",
          modelId: "chat-current",
          capability: "chat",
          factState: "unknown",
          referenced: true,
          recommended: true,
        }]}
        busy={false}
        onPlan={vi.fn()}
        onClose={onClose}
      />,
    );

    const search = screen.getByRole("searchbox");
    const close = screen.getByRole("button", { name: "关闭" });
    const plan = screen.getByRole("button", { name: "生成验证计划" });
    expect(document.activeElement).toBe(search);
    plan.focus();
    fireEvent.keyDown(plan, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    close.focus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(plan);
    fireEvent.keyDown(plan, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    view.unmount();
    // DialogShell FloatingFocusManager asynchronously returns focus after unmount.
    await vi.waitFor(() => expect(document.activeElement).toBe(trigger));
    trigger.remove();
  });

  it("closes the verification selector on Escape, but not while planning is busy", () => {
    const candidate = {
      providerId: "llmp_1",
      providerName: "corp",
      modelId: "chat-current",
      capability: "chat" as const,
      factState: "unknown",
      referenced: true,
      recommended: true,
    };
    const onClose = vi.fn();
    const view = renderZh(
      <VerificationSelector candidates={[candidate]} busy onPlan={vi.fn()} onClose={onClose} />,
    );
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    view.rerender(
      <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
        <VerificationSelector candidates={[candidate]} busy={false} onPlan={vi.fn()} onClose={onClose} />
      </NextIntlClientProvider>,
    );
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("never reflects endpoint credentials or query parameters", () => {
    expect(safeOverviewEndpoint(
      "https://user:password@models.example.test/v1?api_key=secret#fragment",
    )).toBe("https://models.example.test/v1");
    expect(safeOverviewEndpoint("not a valid endpoint?api_key=secret")).toBe("—");
  });

  it("selects only production capabilities missing recent evidence by default", () => {
    const onPlan = vi.fn();
    renderZh(
      <VerificationSelector
        candidates={[
          {
            providerId: "llmp_1",
            providerName: "corp",
            modelId: "chat-current",
            capability: "chat",
            factState: "unknown",
            referenced: true,
            recommended: true,
          },
          {
            providerId: "llmp_1",
            providerName: "corp",
            modelId: "chat-verified",
            capability: "chat",
            factState: "verified",
            referenced: true,
            recommended: false,
          },
          {
            providerId: "llmp_1",
            providerName: "corp",
            modelId: "embed-unused",
            capability: "embed",
            factState: "unknown",
            referenced: false,
            recommended: false,
          },
        ]}
        busy={false}
        onPlan={onPlan}
        onClose={vi.fn()}
      />,
    );

    const checks = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(checks.map((check) => check.checked)).toEqual([true, false, false]);
    expect(screen.getByText("已选择 1 项能力")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生成验证计划" }));
    expect(onPlan).toHaveBeenCalledWith([
      expect.objectContaining({ modelId: "chat-current", capability: "chat" }),
    ]);
  });

  it("keeps authoritative evidence and the latest attempt as separate facts", () => {
    renderZh(
      <ModelSettingsOverview
        version={12}
        providers={[{
          runtimeKey: "corp_azure",
          displayName: "corp_azure",
          type: "azure_openai",
          endpoint: "https://models.example.test",
          modelCount: 2,
          fingerprint: "sha256:abc123",
        }]}
        readiness={{
          as_of: "2026-07-30T00:00:00Z",
          draft_semantic_hash: "a".repeat(64),
          may_apply: true,
          rows: [{
            role_key: "tiers.primary",
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            provider_display_name: "corp_azure",
            model_id: "gpt-51",
            capability: "chat",
            routing_critical: true,
            declared: true,
            fact_state: "verified",
            attempt_state: "errored",
            observed_at: "2026-07-30T00:00:00Z",
            status: "ready",
            changed: false,
          }],
          blocking: [],
          warnings: [],
          required_acceptances: [],
          external_rows: [],
          baseline_will_invalidate: false,
        }}
        readinessLoading={false}
        readinessError={null}
        reindexState="idle"
        onEdit={vi.fn()}
        onAddProvider={vi.fn()}
        onEditProvider={vi.fn()}
        onRetryReadiness={vi.fn()}
        onVerify={vi.fn()}
        verificationBusy={false}
        verificationAvailable
        configurationActionsDisabled={false}
      />,
    );

    // 2026-08-24: idle state no longer renders “服务端判定就绪/未就绪” verdicts (see readiness-unknown.test.tsx).
    // This case concerns authoritative evidence and latest attempt as two independent facts, unrelated to the verdict.
    expect(screen.queryByText("服务端判定就绪")).toBeNull();
    expect(screen.getByText("已验证")).toBeTruthy();
    expect(screen.getByText("本次请求出错")).toBeTruthy();
    expect(screen.getByText("sha256:abc123")).toBeTruthy();
    expect(screen.queryByText(/Bearer|API key/i)).toBeNull();
  });

  it("never presents missing server readiness as ready", () => {
    const onRetry = vi.fn();
    renderZh(
      <ModelSettingsOverview
        version={12}
        providers={[]}
        readiness={null}
        readinessLoading={false}
        readinessError="localized safe error"
        reindexState="idle"
        onEdit={vi.fn()}
        onAddProvider={vi.fn()}
        onEditProvider={vi.fn()}
        onRetryReadiness={onRetry}
        onVerify={vi.fn()}
        verificationBusy={false}
        verificationAvailable={false}
        configurationActionsDisabled={false}
      />,
    );

    expect(screen.queryByText("服务端判定就绪")).toBeNull();
    expect(screen.getByText("暂时无法检查模型是否可以使用，请稍后重试。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("keeps discovered candidates unselected and never invents capabilities", () => {
    const onAdd = vi.fn();
    renderZh(
      <DiscoveryPicker
        candidates={[
          {
            model_id: "chat-looking-name",
            already_added: false,
            capability_state: "unknown",
          },
          {
            model_id: "already-present",
            already_added: true,
            capability_state: "unknown",
          },
        ]}
        truncated={false}
        onAdd={onAdd}
        onClose={vi.fn()}
      />,
    );

    const add = screen.getByRole("button", { name: "添加所选模型" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByText("能力未知")).toHaveLength(1);
    expect(
      (screen.getByRole("option", {
        name: /already-present/,
      }) as HTMLButtonElement).disabled,
    ).toBe(true);

    fireEvent.click(screen.getByRole("option", { name: /chat-looking-name/ }));
    expect((add as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(add);
    expect(onAdd).toHaveBeenCalledWith(["chat-looking-name"]);
  });

  it("moves through discovered models with the keyboard and keeps a single tab stop", () => {
    renderZh(
      <DiscoveryPicker
        candidates={[
          { model_id: "model-a", already_added: false, capability_state: "unknown" },
          { model_id: "present-one", already_added: true, capability_state: "unknown" },
          { model_id: "model-b", already_added: false, capability_state: "unknown" },
          { model_id: "model-c", already_added: false, capability_state: "unknown" },
        ]}
        truncated={false}
        onAdd={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    const option = (name: RegExp) => screen.getByRole("option", { name });
    const list = screen.getByRole("listbox", { name: "选择要添加的模型" });

    expect(
      screen.getAllByRole("option").filter((item) => item.tabIndex === 0),
    ).toEqual([option(/model-a/)]);

    fireEvent.keyDown(screen.getByRole("textbox"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(option(/model-a/));
    fireEvent.keyDown(list, { key: "ArrowDown" });
    // Already-added models cannot be selected; arrow keys skip them.
    expect(document.activeElement).toBe(option(/model-b/));
    fireEvent.keyDown(list, { key: "End" });
    expect(document.activeElement).toBe(option(/model-c/));
    fireEvent.keyDown(list, { key: "ArrowDown" });
    expect(document.activeElement).toBe(option(/model-c/));
    fireEvent.keyDown(list, { key: "Home" });
    expect(document.activeElement).toBe(option(/model-a/));
    fireEvent.keyDown(list, { key: "ArrowUp" });
    expect(document.activeElement).toBe(option(/model-a/));

    fireEvent.keyDown(list, { key: "End" });
    expect(
      screen.getAllByRole("option").filter((item) => item.tabIndex === 0),
    ).toEqual([option(/model-c/)]);
  });

  it("requires explicit readiness acceptance without changing server status", () => {
    const onToggleAcceptance = vi.fn();
    const onToggleMigration = vi.fn();
    renderZh(
      <ReadinessReview
        readiness={{
          as_of: "2026-07-30T00:00:00Z",
          draft_semantic_hash: "a".repeat(64),
          may_apply: false,
          rows: [{
            role_key: "kb.rerank",
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            provider_display_name: "corp-openai",
            model_id: "reranker-1",
            capability: "rerank",
            routing_critical: false,
            declared: true,
            fact_state: "not_testable",
            status: "risk_acceptance_required",
            changed: true,
          }],
          blocking: [],
          warnings: [],
          required_acceptances: [{
            acceptance_token: "signed-risk-token",
            risk_code: "capability_not_testable",
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            model_id: "reranker-1",
            capability: "rerank",
            role_key: "kb.rerank",
          }],
          migration_acceptance: {
            acceptance_token: "signed-migration-token",
            risk_code: "pre_v2_configuration_unverified",
          },
          external_rows: [],
          baseline_will_invalidate: false,
        }}
        acceptedTokens={new Set()}
        migrationAccepted={false}
        onToggleAcceptance={onToggleAcceptance}
        onToggleMigration={onToggleMigration}
      />,
    );

    const checks = screen.getAllByRole("checkbox");
    fireEvent.click(checks[0]);
    fireEvent.click(checks[1]);
    expect(onToggleAcceptance).toHaveBeenCalledWith("signed-risk-token");
    expect(onToggleMigration).toHaveBeenCalledOnce();
    expect(screen.getByText("需确认风险")).toBeTruthy();
  });

  it("shows an actionable Azure verification failure without raw provider text", () => {
    const onRetry = vi.fn();
    renderZh(
      <VerificationPanel
        plan={{
          plan_id: "lvplan_0123456789abcdef0123456789abcdef",
          confirmation_token: "signed-confirmation-token",
          expires_at: "2026-07-30T00:15:00Z",
          item_count: 1,
          provider_request_count: 1,
          max_concurrency: 1,
          notices: [],
          items: [{
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            provider_display_name: "UPTIMIZE",
            model_id: "gpt-51",
            capability: "chat",
            testability: "testable",
            provider_request_count: 1,
            testability_contract_version: "azure_openai-chat-testability-v1",
            probe_contract_version: "chat-v1",
            probe_execution_policy_version: "probe-policy-v1",
          }],
        }}
        batch={{
          batch_id: "lvb_0123456789abcdef0123456789abcdef",
          plan_id: "lvplan_0123456789abcdef0123456789abcdef",
          state: "completed",
          revision: 2,
          base_config_version: 7,
          counts: { failed: 1 },
          items: [{
            item_id: "lvi_chat",
            ordinal: 0,
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            model_id: "gpt-51",
            capability: "chat",
            state: "failed",
            revision: 2,
            safe_code: "api_version_invalid",
            safe_http_status: 400,
          }],
        }}
        busy={false}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onDismiss={vi.fn()}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText(/API Version 不受支持/)).toBeTruthy();
    expect(screen.getByText("HTTP 400")).toBeTruthy();
    expect(screen.getByText("api_version_invalid")).toBeTruthy();
    expect(document.querySelector(".verification-items")?.getAttribute("aria-live"))
      .toBe("polite");
    expect(screen.getByText("已验证 0 · 未通过 1 · 无法自动验证 0 · 未完成 0 · 进行中 0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重试未通过的 1 项" }));
    expect(onRetry).toHaveBeenCalledOnce();
    expect(screen.queryByText(/raw provider/i)).toBeNull();
  });

  it("does not present an untestable capability as a failed verification", () => {
    renderZh(
      <VerificationPanel
        plan={{
          plan_id: "lvplan_0123456789abcdef0123456789abcdef",
          confirmation_token: "signed-confirmation-token",
          expires_at: "2026-07-30T00:15:00Z",
          item_count: 1,
          provider_request_count: 0,
          max_concurrency: 1,
          notices: [],
          items: [{
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            provider_display_name: "DashScope",
            model_id: "gte-rerank-v2",
            capability: "rerank",
            testability: "not_testable",
            provider_request_count: 0,
            testability_contract_version: "openai-rerank-testability-v1",
            probe_contract_version: "rerank-v1",
            probe_execution_policy_version: "probe-policy-v1",
          }],
        }}
        batch={{
          batch_id: "lvb_0123456789abcdef0123456789abcdef",
          plan_id: "lvplan_0123456789abcdef0123456789abcdef",
          state: "completed",
          revision: 2,
          base_config_version: 7,
          counts: { not_testable: 1 },
          items: [{
            item_id: "lvi_rerank",
            ordinal: 0,
            provider_id: "llmp_0123456789abcdef0123456789abcdef",
            model_id: "gte-rerank-v2",
            capability: "rerank",
            state: "not_testable",
            revision: 2,
            safe_code: "probe_not_supported",
          }],
        }}
        busy={false}
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("暂不支持自动验证")).toBeTruthy();
    expect(screen.queryByText("验证未通过")).toBeNull();
  });
});
