/**
 * Apply surface: unverified has never been a hard block, but the heading must agree with the button.
 *
 * Applying without verification already works mechanically—the button checks only `blocking` and completed risk acknowledgments
 * (`may_apply = not blocking and not required_acceptances`). Two things were missing:
 *
 * 1. **Heading previously had only two states**: `blocking=0` said Can Apply, but outstanding risk acknowledgments
 *    actually disabled Apply, making page copy disagree with system behavior.
 * 2. **Nobody explained that acknowledgment was the normal path**, forcing users to infer it from checkboxes.
 *
 * ⚠ Verification is **deliberately absent here**: review uses a frozen draft semantic snapshot, while `draft_semantic_hash`
 * includes every row's `fact_state`/`evidence_id`; verifying inside review immediately invalidates it
 * (`ReadinessReviewStale`). Keep verification in the overview evidence table; here explain its location and cost.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReadinessReview } from "@/components/settings/models/readiness-review";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

const ACCEPTANCE = {
  acceptance_token: "tok_1",
  risk_code: "capability_unknown",
  role_key: "primary",
  provider_id: "p",
  model_id: "gpt-51",
  capability: "chat",
};

const BASE = {
  as_of: "2026-08-24T00:00:00Z",
  draft_semantic_hash: "a".repeat(64),
  may_apply: false,
  rows: [],
  blocking: [],
  warnings: [],
  required_acceptances: [],
  migration_acceptance: null,
  external_rows: [],
  baseline_will_invalidate: false,
};

function renderReview(readiness: Record<string, unknown>, accepted: string[] = []) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ReadinessReview
        readiness={readiness as never}
        acceptedTokens={new Set(accepted)}
        migrationAccepted={false}
        onToggleAcceptance={vi.fn()}
        onToggleMigration={vi.fn()}
      />
    </NextIntlClientProvider>,
  );
}

describe("表头三态", () => {
  it("★ 有未勾的风险确认时不说「可以应用」——那时按钮还是灰的", () => {
    renderReview({ ...BASE, required_acceptances: [ACCEPTANCE] });

    expect(screen.getByText("确认 1 项后即可应用")).toBeTruthy();
    expect(screen.queryByText("可以应用")).toBeNull();
  });

  it("勾完之后才说「可以应用」", () => {
    renderReview({ ...BASE, required_acceptances: [ACCEPTANCE] }, ["tok_1"]);

    expect(screen.getByText("可以应用")).toBeTruthy();
  });

  it("负对照：真有阻断时仍然说阻断（阻断压过一切）", () => {
    renderReview({
      ...BASE,
      blocking: [{ code: "x", field_path: "f", role_key: "r", provider_id: "p", model_id: "m", capability: "chat", action_code: "fix" }],
      required_acceptances: [ACCEPTANCE],
    });

    expect(screen.getByText("1 项阻止应用")).toBeTruthy();
    expect(screen.queryByText(/即可应用/)).toBeNull();
  });

  it("负对照：本来就没风险时直接说「可以应用」", () => {
    renderReview(BASE);

    expect(screen.getByText("可以应用")).toBeTruthy();
  });
});

describe("把「不验证也能应用」说出来", () => {
  it("★ 有待确认项时说清：不影响应用、以及验证在哪、代价是什么", () => {
    renderReview({ ...BASE, required_acceptances: [ACCEPTANCE] });

    const note = document.querySelector(".readiness-optional")?.textContent ?? "";
    expect(note).toContain("确认风险后保存");
    expect(note).toContain("模型分工");      // Identify where to verify
    expect(note).toContain("重新检查并确认保存");      // Explain the cost: invalidates the review
  });

  it("负对照：没有待确认项时不出这句话（别对着空气解释）", () => {
    renderReview(BASE);

    expect(document.querySelector(".readiness-optional")).toBeNull();
  });

  it("★ 审阅面里不提供验证按钮——在这里验证会当场把本次审阅作废", () => {
    renderReview({ ...BASE, required_acceptances: [ACCEPTANCE] });

    expect(screen.queryByRole("button", { name: /验证|重试/ })).toBeNull();
    // Positive control: prove this surface actually rendered, only without a verification button.
    expect(screen.getByText("模型可用性")).toBeTruthy();
  });
});
