/**
 * Claim-checkbox copy must be truthful **for each risk type**, because users are signing it.
 *
 * Before 2026-08-14 allowed failed / stale / unknown claims to apply, this had one hardcoded sentence:
 * “当前协议无法自动验证”. Reusing it when probes explicitly fail makes users sign a false statement (claiming failed verification
 * is unverifiable), precisely when they most need the truth.
 *
 * This exact case occurred on 116, 2026-08-15: Doubao-Seed-2.0-pro returned model_identity_mismatch,
 * with the server actually returning doubao-seed-2-1-turbo-260628 — **a different model**, not a version difference.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReadinessReview } from "@/components/settings/models/readiness-review";
import messagesZh from "@/messages/zh-CN.json";

afterEach(cleanup);

function readinessWith(riskCode: string) {
  return {
    as_of: "2026-08-15T00:00:00Z",
    draft_semantic_hash: "a".repeat(64),
    may_apply: false,
    rows: [],
    blocking: [],
    warnings: [],
    required_acceptances: [{
      acceptance_token: `token-${riskCode}`,
      risk_code: riskCode,
      provider_id: "llmp_0123456789abcdef0123456789abcdef",
      model_id: "Doubao-Seed-2.0-pro",
      capability: "chat",
      role_key: "tier.primary_fallback",
    }],
    migration_acceptance: null,
    external_rows: [],
    baseline_will_invalidate: false,
  };
}

function renderRisk(riskCode: string) {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={messagesZh}>
      <ReadinessReview
        readiness={readinessWith(riskCode) as never}
        acceptedTokens={new Set()}
        migrationAccepted={false}
        onToggleAcceptance={vi.fn()}
        onToggleMigration={vi.fn()}
        disabled={false}
      />
    </NextIntlClientProvider>,
  );
}

describe("认领文案按风险种类取", () => {
  it("验证失败：说「验证失败」，不得说成「无法自动验证」", () => {
    renderRisk("capability_failed");

    expect(screen.getByText(/接受验证失败的能力风险/)).toBeTruthy();
    // The negative assertion matters most: claiming verification is impossible makes users sign a false statement
    expect(screen.queryByText(/无法自动验证/)).toBeNull();
    expect(screen.getByText(/最近一次验证判定为失败/)).toBeTruthy();
  });

  it("证据过期：说「证据已过期」", () => {
    renderRisk("capability_stale");

    expect(screen.getByText(/接受证据已过期的能力风险/)).toBeTruthy();
    expect(screen.queryByText(/无法自动验证/)).toBeNull();
  });

  it("从未验证：说「尚未验证」", () => {
    renderRisk("capability_unknown");

    expect(screen.getByText(/接受尚未验证的能力风险/)).toBeTruthy();
    expect(screen.queryByText(/无法自动验证/)).toBeNull();
  });

  it("协议测不了：保留原文案（负对照，本次不该动它）", () => {
    renderRisk("capability_not_testable");

    expect(screen.getByText(/接受未能自动验证的能力风险/)).toBeTruthy();
    expect(screen.getByText(/目前无法自动验证/)).toBeTruthy();
  });

  it("未知 risk_code 回落到最保守那句，不抛", () => {
    expect(() => renderRisk("some_future_risk_code")).not.toThrow();
    expect(screen.getByText(/接受未能自动验证的能力风险/)).toBeTruthy();
  });
});
