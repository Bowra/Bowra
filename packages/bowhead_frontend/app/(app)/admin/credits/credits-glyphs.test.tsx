import { cleanup, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";

vi.mock("@/components/admin-shell", () => ({ AdminShell: ({ children }: { children: ReactNode }) => <>{children}</> }));
vi.mock("@/components/admin/credits/csv-download-button", () => ({ CsvDownloadButton: () => null }));
vi.mock("@/components/admin/credits/edit-user-modal", () => ({ EditUserModal: () => null }));
vi.mock("@/components/admin/credits/grant-credit-modal", () => ({ GrantCreditModal: () => null }));
vi.mock("@/components/admin/credits/period-reset-modal", () => ({ PeriodResetModal: () => null }));
vi.mock("@/components/admin/credits/summary-card", () => ({ SummaryCard: () => null }));
vi.mock("@/components/admin/credits/tier-defaults", () => ({ TierDefaults: () => null }));
vi.mock("@/components/admin/credits/team-quota-modal", () => ({ TeamQuotaModal: () => null }));
vi.mock("@/components/admin/credits/users-table", () => ({ UsersTable: () => null }));
vi.mock("@/components/admin/credits/ledger-table", () => ({ LedgerTable: () => null }));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams("user_id=u_1") }));
vi.mock("@/lib/auth/use-auth-redirect", () => ({
  useAuthRedirect: () => ({
    status: "authed",
    me: { id: "u_admin", credit_capabilities: { credits_read: true, credit_period_reset: true } },
  }),
}));
vi.mock("@/lib/admin-credits-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/admin-credits-api")>()),
  fetchCreditSummary: vi.fn().mockResolvedValue(null),
  fetchCreditUserInfo: vi.fn().mockResolvedValue({
    user_id: "u_1",
    email: "user@example.com",
    display_name: "User",
    tier: "pro",
    status: "active",
    credit_balance: 120,
    credit_quota: 500,
    spend_30d: 1234,
  }),
}));

import CreditsAdminPage from "./page";
import CreditUserDrilldownPage from "./users/page";

afterEach(cleanup);

describe("credits admin glyphs", () => {
  it("period reset button: ▾ after the label", async () => {
    renderIntl(<CreditsAdminPage />);
    const message = catalogText("zh-CN", "admin.credits.period_reset_button");
    const button = await screen.findByRole("button", { name: (name) => name.startsWith(message) });
    expectRefreshedGlyph(button, "▾", "end", message);
  });

  it("user drill-down: ⚡ after the 30-day spend", async () => {
    const { container } = renderIntl(<CreditUserDrilldownPage />);
    await screen.findByText("user@example.com");
    const balance = container.querySelector(".legacy-credits-user-balance") as HTMLElement;
    const spend = catalogText("zh-CN", "admin.credits.spend_30d_label", { spend: (1234).toLocaleString() });
    const glyphs = balance.querySelectorAll("svg.bh-glyph");
    expect(glyphs).toHaveLength(2);
    expect(glyphs[1].getAttribute("aria-label")).toBe("⚡");
    expect(balance.lastChild).toBe(glyphs[1]);
    expect(balance.textContent?.endsWith(` · ${spend} `)).toBe(true);
  });

  it("downloads the selected user's credit ledger directly", async () => {
    renderIntl(<CreditUserDrilldownPage />);
    const link = await screen.findByRole("link", { name: "⬇ 下载额度流水 CSV" });
    expect(link.getAttribute("href")).toBe("/api/v1/admin/credits/export.csv?scope=user_ledger&user_id=u_1");
    expect(link.hasAttribute("download")).toBe(true);
  });
});

