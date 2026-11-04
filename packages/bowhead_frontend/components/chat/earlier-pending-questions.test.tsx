import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, act } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import { NextIntlClientProvider } from "next-intl";
import messages from "@/messages/zh-CN.json";
import { EarlierPendingQuestions } from "./earlier-pending-questions";
import { _resetClarificationTombstonesForTest } from "@/lib/clarification-tombstone";

const { list, dismiss } = vi.hoisted(() => ({ list: vi.fn(), dismiss: vi.fn() }));
vi.mock("@/lib/api", () => ({ listPendingSessionQuestions: list, dismissClarification: dismiss }));
afterEach(cleanup);
beforeEach(() => { list.mockReset(); dismiss.mockReset(); _resetClarificationTombstonesForTest(); });
const old = { request_id: "old-question", turn_id: "old-turn", question: "Earlier unanswered question", can_dismiss: true };

it("keeps an older question visible during a new Turn and removes it only after a committed cancellation", async () => {
  list.mockResolvedValue([old, { ...old, request_id: "current-question", turn_id: "current-turn", question: "Current question" }]);
  let commit!: (value: unknown) => void;
  dismiss.mockReturnValue(new Promise((resolve) => { commit = resolve; }));
  renderIntl(<EarlierPendingQuestions sessionId="session" currentTurnId="current-turn" running={false} />);
  await screen.findByText(old.question);
  expect(screen.queryByText("Current question")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(dismiss).toHaveBeenCalledWith(old.request_id);
  expect(screen.getByText(old.question)).toBeTruthy();
  await act(async () => { commit({ dismissed: true }); });
  await waitFor(() => expect(screen.queryByText(old.question)).toBeNull());
});

it("retains the question when cancellation is rejected", async () => {
  list.mockResolvedValue([old]);
  dismiss.mockResolvedValue({ dismissed: false });
  renderIntl(<EarlierPendingQuestions sessionId="session" currentTurnId="current-turn" running={false} />);
  await screen.findByText(old.question);
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  await waitFor(() => expect(screen.getAllByRole("group")).toHaveLength(2));
  expect(screen.getByText(old.question)).toBeTruthy();
});

it("discards a stale session response", async () => {
  let finish!: (value: unknown) => void;
  list.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; })).mockResolvedValueOnce([]);
  const view = renderIntl(<EarlierPendingQuestions sessionId="first" currentTurnId="turn" running={false} />);
  view.rerender(<NextIntlClientProvider locale="zh-CN" messages={messages}><EarlierPendingQuestions sessionId="second" currentTurnId="turn" running={false} /></NextIntlClientProvider>);
  await act(async () => { finish([old]); });
  expect(screen.queryByText(old.question)).toBeNull();
});
