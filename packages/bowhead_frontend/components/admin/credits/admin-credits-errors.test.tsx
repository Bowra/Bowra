import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";
import type { CreditUserRow } from "@/lib/admin-credits-api";

import { EditUserModal } from "./edit-user-modal";
import { GrantCreditModal } from "./grant-credit-modal";
import { PeriodResetModal } from "./period-reset-modal";

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    grant: "发放",
    save: "保存",
    poolInsufficient:
      "团队额度池余额不足，无法完成发放；请减少金额或先提升团队额度。",
    inactive: "该成员已停用，不能调整额度；请刷新用户列表。",
    teamMissing:
      "当前团队不可用，请刷新页面后重试；如问题持续，请联系系统管理员。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    grant: "Grant",
    save: "Save",
    poolInsufficient:
      "The team credit pool does not have enough balance for this grant. Reduce the amount or increase the team quota first.",
    inactive:
      "This member is inactive, so their credits cannot be changed. Refresh the user list.",
    teamMissing:
      "The current team is unavailable. Refresh the page and try again. If the problem continues, contact your system administrator.",
  },
] as const;

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

function codedError(status: number, code: string): Response {
  return new Response(
    JSON.stringify({
      detail: {
        code,
        message: `RAW_BACKEND_${code}`,
      },
    }),
    {
      status,
      headers: { "content-type": "application/json" },
    },
  );
}

function renderLocalized(
  entry: (typeof locales)[number],
  child: React.ReactNode,
) {
  return render(
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      {child}
    </NextIntlClientProvider>,
  );
}

function expectNoTechnicalErrorText() {
  expect(document.body.textContent).not.toContain("RAW_BACKEND");
  expect(document.body.textContent).not.toContain(
    "credit_team_pool_insufficient",
  );
  expect(document.body.textContent).not.toContain("credit_user_inactive");
  expect(document.body.textContent).not.toContain("team_not_found");
  expect(document.body.textContent).not.toContain("HTTP 409");
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe.each(locales)("admin credit errors — $locale", (entry) => {
  it("localizes grant failures without HTTP status or backend prose", async () => {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      codedError(409, "credit_team_pool_insufficient"),
    );
    renderLocalized(
      entry,
      <GrantCreditModal
        user={user}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: entry.grant }));

    expect(await screen.findByText(entry.poolInsufficient)).toBeTruthy();
    expectNoTechnicalErrorText();
  });

  it("localizes edit conflicts without backend prose", async () => {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      codedError(409, "credit_user_inactive"),
    );
    renderLocalized(
      entry,
      <EditUserModal
        user={user}
        capabilities={{
          credits_read: true,
          credit_grant: true,
          credit_user_quota_write: true,
          credit_user_tier_write: true,
          credit_period_reset: true,
          credit_team_quota_write: true,
          credit_attention_dismiss: true,
        }}
        onClose={vi.fn()}
        onSaved={vi.fn()}
      />,
    );

    const fields = screen.getAllByRole("spinbutton");
    fireEvent.change(fields[1], { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: entry.save }));

    expect(await screen.findByText(entry.inactive)).toBeTruthy();
    expectNoTechnicalErrorText();
  });

  it("localizes period-reset load failures", async () => {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      codedError(404, "team_not_found"),
    );
    renderLocalized(
      entry,
      <PeriodResetModal
        mode="balance_only"
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        onSwitchMode={vi.fn()}
      />,
    );

    expect(await screen.findByText(entry.teamMissing)).toBeTruthy();
    expectNoTechnicalErrorText();
  });
});
