/**
 * Credit-management dialog dismissal contract (component unification P1, step 2: migrate custom dialogs to DialogShell).
 * Esc requests dismissal; ignore Esc during submission so the dialog does not disappear mid-request with nowhere to show the result.
 */
import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { CreditUserRow } from "@/lib/admin-credits-api";

import { GrantCreditModal } from "./grant-credit-modal";
import { TeamQuotaModal } from "./team-quota-modal";

const user: CreditUserRow = {
  user_id: "usr_test",
  email: "member@example.com",
  display_name: "Member",
  tier: "pro",
  status: "active",
  credit_balance: 100,
  credit_quota: 1_000,
  credit_period_start: null,
  spend_30d: 0,
  last_activity_at: null,
  is_team_admin: false,
  attention_score: 0,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("GrantCreditModal dismissal", () => {
  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderIntl(<GrantCreditModal user={user} onClose={onClose} onSuccess={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "发放额度" });
    fireEvent.keyDown(document.activeElement ?? dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("stays open on Escape while the grant request is in flight", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise<Response>(() => {}));
    const onClose = vi.fn();
    renderIntl(<GrantCreditModal user={user} onClose={onClose} onSuccess={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "发放" }));
    fireEvent.keyDown(screen.getByRole("dialog", { name: "发放额度" }), { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "发放额度" })).toBeTruthy();
  });
});

describe("TeamQuotaModal dismissal", () => {
  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderIntl(<TeamQuotaModal currentQuota={1000} onClose={onClose} onSuccess={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "调整团队额度配额" });
    fireEvent.keyDown(document.activeElement ?? dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("stays open on Escape while the quota update is submitting", () => {
    vi.spyOn(global, "fetch").mockReturnValue(new Promise<Response>(() => {}));
    const onClose = vi.fn();
    renderIntl(<TeamQuotaModal currentQuota={1000} onClose={onClose} onSuccess={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "调整团队额度配额" });
    fireEvent.change(screen.getByPlaceholderText(/第四季度预算提升/), { target: { value: "Q4 budget increase" } });
    fireEvent.submit(dialog.querySelector("form")!);
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "调整团队额度配额" })).toBeTruthy();
  });
});
