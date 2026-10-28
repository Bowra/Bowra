/**
 * Idle state must not issue readiness verdicts on the server's behalf.
 *
 * Before a primary model is configured, readiness has not been evaluated. A failure banner would show
 * “服务端判定未就绪” + “当前配置还不能安全应用”, while all three counts were “—” (no data).
 * Server actually required configuration completion before readiness evaluation; incomplete configuration had never been judged,
 * `readiness` was null—the page issued a verdict the server never made.
 *
 * Since 2026-08-24, a **stronger rule** supersedes this: idle state **renders no readiness verdict at all**.
 * Two existing authorities independently support it: `docs/design/团队模型配置.md` requires readiness **before** V2 Apply from a frozen
 * Draft semantic snapshot; the original models-page design forbids a permanent overview of current effective configuration.
 * The old panel violated both: without a draft, it asked whether already-running production configuration could apply,
 * leaving a persistent red warning, with no clickable element anywhere in the panel.
 *
 * Enforce: **show a quiet single-line hint only when something needs attention, and never show a red verdict**.
 */

import type { ComponentProps } from "react";

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelSettingsOverview } from "@/components/settings/models/model-settings-overview";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const READY = {
  as_of: "2026-08-17T00:00:00Z",
  draft_semantic_hash: "a".repeat(64),
  may_apply: true,
  rows: [],
  blocking: [],
  warnings: [],
  required_acceptances: [],
  migration_acceptance: null,
  external_rows: [],
  baseline_will_invalidate: false,
};

/** An aged-evidence row—the only idle-state case that should speak up. */
const AGED_ROW = {
  role_key: "primary",
  provider_id: "p",
  provider_display_name: "P",
  model_id: "m",
  capability: "chat",
  routing_critical: true,
  declared: true,
  fact_state: "aged",
  observed_at: "2026-08-09T00:00:00Z",
  status: "warning",
  changed: false,
};

type OverviewProps = ComponentProps<typeof ModelSettingsOverview>;

// Use the component's own props type, not Record<string, unknown> + `as never`:
// the latter causes tsc TS2698 (spread never), and fails to catch misspelled fixture prop names.
function renderOverview(overrides: Partial<OverviewProps>) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ModelSettingsOverview
        version={1}
        providers={[]}
        readiness={null}
        readinessLoading={false}
        readinessError={null}
        reindexState="idle"
        onEdit={vi.fn()}
        onAddProvider={vi.fn()}
        onEditProvider={vi.fn()}
        onRetryReadiness={vi.fn()}
        onVerify={vi.fn()}
        verificationBusy={false}
        verificationAvailable={false}
        configurationActionsDisabled={false}
        {...overrides}
      />
    </NextIntlClientProvider>,
  );
}

/** Any red verdict is a regression—the shared criterion in this file. */
function expectNoVerdict() {
  expect(screen.queryByText("服务端判定未就绪")).toBeNull();
  expect(screen.queryByText("服务端判定就绪")).toBeNull();
  expect(screen.queryByText("当前配置还不能安全应用")).toBeNull();
  expect(document.querySelector(".overview-authority")).toBeNull();
  expect(document.querySelector(".overview-summary")).toBeNull();
}

describe("静置态不出现就绪判决", () => {
  it("★ 一切正常时页面上没有任何常驻状态条", () => {
    renderOverview({ readiness: READY });

    expectNoVerdict();
    expect(document.querySelector(".overview-quiet")).toBeNull();
  });

  it("★ 真有事时只留一行安静提示（不是红判决）", () => {
    renderOverview({ readiness: { ...READY, may_apply: false, rows: [AGED_ROW] } });

    const line = document.querySelector(".overview-quiet");
    expect(line?.textContent).toContain("1 条依据时间较早");
    expect(line?.className).not.toContain("is-error");
    expectNoVerdict();
  });

  it("提示里给得着的入口，指向下面那张逐条依据表", () => {
    renderOverview({ readiness: { ...READY, may_apply: false, rows: [AGED_ROW] } });

    const jump = screen.getByRole("link", { name: "去验证" });
    expect(jump.getAttribute("href")).toBe("#mset-overview-evidence");
  });
});

describe("服务端还没评估：第三种状态", () => {
  it("★ readiness 为 null 时说清「还没评估」，绝不说「未就绪」", () => {
    renderOverview({ readiness: null });

    expect(
      screen.getByText(/请先按下方提示补全配置/),
    ).toBeTruthy();
    expectNoVerdict();
  });

  it("负对照：加载中顶部不出声——那句话由下面的依据表自己说，别重复", () => {
    renderOverview({ readiness: null, readinessLoading: true });

    expect(document.querySelector(".overview-quiet")).toBeNull();
    // Positive control: this state is still rendered, but in the table.
    expect(screen.getByText(/正在检查模型是否可以使用/)).toBeTruthy();
    expect(screen.queryByText(/请先按下方提示补全配置/)).toBeNull();
    expectNoVerdict();
  });

  it("负对照：取不到结论时给重试，也不说未就绪", () => {
    renderOverview({ readiness: null, readinessError: "boom" });

    expect(screen.getByRole("button", { name: /重新检查/ })).toBeTruthy();
    expectNoVerdict();
  });
});
