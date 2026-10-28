/**
 * Per-item verification: action on each evidence-table row, with inline consent.
 *
 * Previously this table allowed **full-batch** verification only—users could not reverify one item, while a batch
 * sends many requests to customer model services and consumes their quota.
 *
 * Enforce two contracts:
 * 1. **Consent still required**: clicking Verify sends no immediate request; first expand inline “1 request will be sent to X”.
 *    Verification costs real money; placing it in a row does not waive consent.
 * 2. **Ask only once**: after confirmation, go directly plan → batch, without the full-batch confirmation modal.
 */

import type { ComponentProps } from "react";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsOverview } from "@/components/settings/models/model-settings-overview";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const ROW = {
  role_key: "primary",
  provider_id: "llmp_1",
  provider_display_name: "corp_gateway",
  model_id: "gpt-51",
  capability: "chat",
  routing_critical: true,
  declared: true,
  fact_state: "aged",
  observed_at: "2026-08-09T00:00:00Z",
  status: "warning",
  changed: false,
};

const READINESS = {
  as_of: "2026-08-24T00:00:00Z",
  draft_semantic_hash: "a".repeat(64),
  may_apply: false,
  rows: [ROW],
  blocking: [],
  warnings: [],
  required_acceptances: [],
  migration_acceptance: null,
  external_rows: [],
  baseline_will_invalidate: false,
};

type OverviewProps = ComponentProps<typeof ModelSettingsOverview>;

function renderOverview(overrides: Partial<OverviewProps> = {}) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ModelSettingsOverview
        version={17}
        providers={[]}
        readiness={READINESS as never}
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
        {...overrides}
      />
    </NextIntlClientProvider>,
  );
}

describe("逐项验证", () => {
  it("★ 点「验证」不发请求，先就地问同意", () => {
    const onVerifyItem = vi.fn();
    renderOverview({ onVerifyItem });

    fireEvent.click(screen.getByRole("button", { name: "验证 gpt-51" }));

    expect(onVerifyItem).not.toHaveBeenCalled();
    expect(screen.getByText("将向 corp_gateway 发 1 次请求。")).toBeTruthy();
  });

  it("确认之后才真的开始，且带的是这一行", () => {
    const onVerifyItem = vi.fn();
    renderOverview({ onVerifyItem });

    fireEvent.click(screen.getByRole("button", { name: "验证 gpt-51" }));
    fireEvent.click(screen.getByRole("button", { name: "开始验证" }));

    expect(onVerifyItem).toHaveBeenCalledOnce();
    expect(onVerifyItem.mock.calls[0][0]).toMatchObject({
      provider_id: "llmp_1",
      model_id: "gpt-51",
      capability: "chat",
    });
  });

  it("取消就什么都不发生", () => {
    const onVerifyItem = vi.fn();
    renderOverview({ onVerifyItem });

    fireEvent.click(screen.getByRole("button", { name: "验证 gpt-51" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(onVerifyItem).not.toHaveBeenCalled();
    expect(screen.queryByText(/发 1 次请求/)).toBeNull();
  });

  it("★ 正在验证的那一行不给按钮——否则会重复下单", () => {
    renderOverview({
      onVerifyItem: vi.fn(),
      readiness: { ...READINESS, rows: [{ ...ROW, attempt_state: "verifying" }] } as never,
    });

    expect(screen.queryByRole("button", { name: "验证 gpt-51" })).toBeNull();
  });

  it("★★ 刚点完、readiness 还没刷新的那段窗口里也不许再点", () => {
    // Code-review defect: `attempt_state` updates only after the batch **finishes** and evidence is refetched.
    // During that gap the row looks unchanged and the button re-enables; users may assume nothing happened and click again,
    // duplicating requests to customer model services (each consumes real requests/quota).
    renderOverview({
      onVerifyItem: vi.fn(),
      // Readiness is still **old** (no attempt_state); only the local marker guards this.
      verifyingRowKey: "llmp_1 gpt-51 chat",
    });

    expect(screen.queryByRole("button", { name: "验证 gpt-51" })).toBeNull();
  });

  it("负对照：本地标记指向别的行时，这一行照常可点", () => {
    renderOverview({
      onVerifyItem: vi.fn(),
      verifyingRowKey: "llmp_1 别的模型 chat",
    });

    expect(screen.getByRole("button", { name: "验证 gpt-51" })).toBeTruthy();
  });

  it("上次失败的那一行说「重试」而不是「验证」", () => {
    renderOverview({
      onVerifyItem: vi.fn(),
      readiness: { ...READINESS, rows: [{ ...ROW, fact_state: "failed" }] } as never,
    });

    const button = screen.getByRole("button", { name: "验证 gpt-51" });
    expect(button.textContent).toBe("重试");
  });

  it("负对照：不传 onVerifyItem 时整列不出现（只读面不给假 affordance）", () => {
    renderOverview();

    expect(screen.queryByRole("button", { name: "验证 gpt-51" })).toBeNull();
    // Positive control: the row is rendered, just without an action column.
    expect(screen.getByText("gpt-51")).toBeTruthy();
  });

  it("负对照：配置面被禁用时按钮也禁用", () => {
    renderOverview({ onVerifyItem: vi.fn(), configurationActionsDisabled: true });

    expect(screen.getByRole("button", { name: "验证 gpt-51" })).toHaveProperty("disabled", true);
  });

  it("先显示需要关注的记录，并用分工名称代替内部角色键", () => {
    const rows = [0, 1, 2, 3, 4, 5].map((index) => ({
      ...ROW,
      role_key: "kb.embed",
      model_id: `model-${index}`,
      fact_state: "verified",
      status: "ok",
    }));
    rows.push({
      ...ROW,
      role_key: "tier.primary",
      model_id: "model-critical",
      fact_state: "failed",
      status: "blocked",
    });
    renderOverview({ readiness: { ...READINESS, rows } as never });

    expect(screen.getByText("model-critical")).toBeTruthy();
    expect(screen.getByText("主模型")).toBeTruthy();
    expect(screen.queryByText("tier.primary")).toBeNull();
    expect(screen.queryByText("model-5")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "显示其余 2 项验证记录" }));
    expect(screen.getByText("model-5")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "收起验证记录" }));
    expect(screen.queryByText("model-5")).toBeNull();
  });
});
