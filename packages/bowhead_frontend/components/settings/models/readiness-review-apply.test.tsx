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
    expect(note).toContain("确认后仍可保存");  // Saving does not need verification
    expect(note).toContain("先返回");          // Where to verify: back in the editor or assignments page
    expect(note).toContain("验证这些能力后再保存"); // Verifying first means saving again
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

describe("the Team review reads in plain names and puts decisions first", () => {
  const row = (roleKey: string, model: string, capability: string, status: string) => ({
    role_key: roleKey, provider_id: "p", provider_display_name: "aliyun_qwen", model_id: model, capability,
    routing_critical: true, declared: true, fact_state: status === "ready" ? "verified" : "failed", status,
  });

  it("names roles and capabilities, and folds rows that need nothing", () => {
    renderReview({
      ...BASE,
      rows: [
        row("tier.primary", "qwen-max", "chat", "ready"),
        row("tier.primary_fallback", "qwen-plus", "tool", "warning"),
        row("kb.row_extractor", "qwen-flash", "chat", "risk_accepted"),
      ],
      required_acceptances: [{ ...ACCEPTANCE, role_key: "tier.primary_fallback", capability: "tool", risk_code: "capability_failed" }],
    });

    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/tier\.|kb\.|· tool|· chat/);
    expect(text).toContain("gpt-51 的工具调用能力用于主模型备用");
    const visibleRows = [...document.querySelectorAll(".readiness-review > .readiness-rows .readiness-row")];
    expect(visibleRows.map((node) => node.querySelector("b")?.textContent)).toEqual(["qwen-plus"]);
    expect(document.querySelector(".readiness-settled summary")?.textContent).toBe("其余 2 项无需处理");
    // The acknowledgment comes before the table it would otherwise sit under.
    const acceptance = document.querySelector(".readiness-acceptance") as Element;
    const table = document.querySelector(".readiness-review > .readiness-rows") as Element;
    expect(acceptance.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
