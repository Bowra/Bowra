import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchUserLedger, type LedgerEntry } from "@/lib/admin-credits-api";
import messagesEn from "@/messages/en-US.json";
import messagesZh from "@/messages/zh-CN.json";

import { LedgerTable } from "./ledger-table";

vi.mock("@/lib/admin-credits-api", () => ({ fetchUserLedger: vi.fn() }));

const ledgerTypes = [
  "subagent_draft_reservation",
  "subagent_draft_reservation_refund",
  "subagent_draft_reservation_extra",
  "subagent_draft_reservation_release",
  "unknown_future_type",
];

const entries: LedgerEntry[] = ledgerTypes.map((type, index) => ({
  id: `ledger_${index}`,
  type,
  workspace_id: null,
  turn_id_short: null,
  tokens_input: 0,
  tokens_output: 0,
  delta: index === 0 ? -100 : 25,
  created_at: "2026-09-25T12:00:00Z",
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe.each([
  {
    locale: "zh-CN",
    messages: messagesZh,
    labels: ["草稿生成预留", "草稿生成退还", "草稿生成补扣", "草稿生成释放预留", "其他额度变动"],
    filter: "类型",
    title: "额度流水",
  },
  {
    locale: "en-US",
    messages: messagesEn,
    labels: ["Draft generation reserved", "Draft generation refund", "Draft generation extra charge", "Draft generation reservation released", "Other credit change"],
    filter: "Type",
    title: "Credit ledger",
  },
])("credit ledger labels — $locale", ({ locale, messages, labels, filter, title }) => {
  it("shows readable entry types and allows filtering a draft adjustment", async () => {
    vi.mocked(fetchUserLedger).mockResolvedValue({ items: entries, total: entries.length, page: 1, size: 50 });
    render(
      <NextIntlClientProvider locale={locale} messages={messages}>
        <LedgerTable userId="usr_member" />
      </NextIntlClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: title })).toBeTruthy();
    for (const label of labels) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(document.body.textContent).not.toContain("ledger_type_subagent");
    expect(document.body.textContent).not.toContain("unknown_future_type");

    fireEvent.change(screen.getByRole("combobox", { name: filter }), {
      target: { value: "subagent_draft_reservation_refund" },
    });
    await waitFor(() => {
      expect(fetchUserLedger).toHaveBeenLastCalledWith("usr_member", {
        page: 1,
        size: 50,
        type: "subagent_draft_reservation_refund",
      });
    });
  });
});

it("shows each model and the frozen token multipliers while old rows remain readable", async () => {
  vi.mocked(fetchUserLedger).mockResolvedValue({
    items: [
      {
        ...entries[0],
        id: "new_turn",
        type: "turn",
        model_usage: [
          { provider: "openai", model: "fast", tokens_input: 200, tokens_cached: 600, tokens_output: 100, rate_input: "2", rate_cached: "0.25", rate_output: "4", weighted_tokens: "950" },
          { provider: "anthropic", model: "deep", tokens_input: 100, tokens_cached: 0, tokens_output: 100, rate_input: "1", rate_cached: "1", rate_output: "2", weighted_tokens: "300" },
        ],
      },
      { ...entries[1], id: "old_turn", type: "turn" },
    ],
    total: 2, page: 1, size: 50,
  });
  render(<NextIntlClientProvider locale="en-US" messages={messagesEn}><LedgerTable userId="usr_member" /></NextIntlClientProvider>);
  expect(await screen.findByText("openai / fast, anthropic / deep")).toBeTruthy();
  expect(screen.getByText("300/600/200")).toBeTruthy();
  expect(screen.getByText(/fast: 200 × 2 \+ 600 × 0.25 \+ 100 × 4/)).toBeTruthy();
  expect(screen.getAllByText("—").length).toBeGreaterThan(0);
});
