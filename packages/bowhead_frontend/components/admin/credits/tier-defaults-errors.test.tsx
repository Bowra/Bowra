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

import { TierDefaults } from "./tier-defaults";

const locales = [
  {
    locale: "zh-CN",
    messages: messagesZh,
    edit: "改",
    save: "保存",
    teamMissing:
      "当前团队不可用，请刷新页面后重试；如问题持续，请联系系统管理员。",
    managed:
      "当前私有化部署的等级默认额度由签名许可证管控；如需修改，请上传重新签发的许可证。",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    edit: "Edit",
    save: "Save",
    teamMissing:
      "The current team is unavailable. Refresh the page and try again. If the problem continues, contact your system administrator.",
    managed:
      "Tier defaults are controlled by the signed license in this private deployment. Upload a newly issued license to change them.",
  },
] as const;

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

function creditsResponse(): Response {
  return new Response(
    JSON.stringify({
      team_id: "team_test",
      tier_credits: { pro: 5_000, ultra: 30_000 },
      contract_expires_at: null,
      team_credit_balance: 10_000,
      team_credit_quota: 30_000,
    }),
    {
      status: 200,
      headers: { "content-type": "application/json" },
    },
  );
}

function renderLocalized(entry: (typeof locales)[number]) {
  return render(
    <NextIntlClientProvider
      locale={entry.locale}
      messages={entry.messages}
    >
      <TierDefaults
        teamId="team_test"
        onChange={vi.fn()}
        refreshTick={0}
      />
    </NextIntlClientProvider>,
  );
}

function expectNoTechnicalErrorText() {
  expect(document.body.textContent).not.toContain("team_not_found");
  expect(document.body.textContent).not.toContain("managed_by_license");
  expect(document.body.textContent).not.toContain("RAW_BACKEND");
  expect(document.body.textContent).not.toContain("HTTP 409");
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe.each(locales)("tier defaults errors — $locale", (entry) => {
  it("localizes team-load failures without backend prose", async () => {
    vi.spyOn(global, "fetch").mockResolvedValueOnce(
      codedError(404, "team_not_found"),
    );

    renderLocalized(entry);

    expect(await screen.findByText(entry.teamMissing)).toBeTruthy();
    expectNoTechnicalErrorText();
  });

  it("explains that private-deployment defaults require a new license", async () => {
    vi.spyOn(global, "fetch")
      .mockResolvedValueOnce(creditsResponse())
      .mockResolvedValueOnce(codedError(409, "managed_by_license"));

    renderLocalized(entry);

    const editButtons = await screen.findAllByRole("button", {
      name: entry.edit,
    });
    fireEvent.click(editButtons[0]);
    fireEvent.change(screen.getByRole("spinbutton"), {
      target: { value: "6000" },
    });
    fireEvent.click(screen.getByRole("button", { name: entry.save }));

    expect(await screen.findByText(entry.managed)).toBeTruthy();
    expectNoTechnicalErrorText();
  });
});
