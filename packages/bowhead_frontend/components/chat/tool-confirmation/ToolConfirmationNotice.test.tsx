import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getTitleAttentionSnapshot, setTitleAttentionCount } from "@/lib/page-title-store";
import type { ToolConfirmationSummary } from "@/lib/tool-confirmation-api";
import {
  _resetConfirmationPresenceForTesting,
  consumeConfirmationFocus,
  setConfirmationCardVisible,
} from "@/lib/tool-confirmation-presence";
import { _resetAllTurnsForTesting } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  push: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      `${key}${values ? ` ${JSON.stringify(values)}` : ""}`,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useAuthSession: () => ({ me: { id: "u_owner" } }),
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push }),
}));
vi.mock("@/lib/tool-confirmation-api", () => ({
  listToolConfirmations: mocks.list,
}));

import { ToolConfirmationNotice } from "./ToolConfirmationNotice";

function summary(id: string, sessionId: string, sessionTitle: string | null): ToolConfirmationSummary {
  return {
    id,
    kind: "team_mcp",
    session_id: sessionId,
    session_title: sessionTitle,
    turn_id: `t_${id}`,
    step_id: `step_${id}`,
    title: "delete_contact",
    source_label: "CRM",
    agent_name: null,
    expires_at: "2026-11-08T10:03:00Z",
    remaining_seconds: 120,
  };
}

function streamRequest(turnId: string, confirmationId: string, requester = "u_owner") {
  act(() => {
    dispatchToTurnStore("confirmation_requested", {
      card_id: turnId,
      step_id: `step_${confirmationId}`,
      call_id: confirmationId,
      confirmation_id: confirmationId,
      kind: "team_mcp",
      requester_user_id: requester,
      title: "delete_contact",
      source_label: "CRM",
      category: "unknown",
      choices: ["reject", "approve"],
      expires_at: "2026-11-08T10:03:00Z",
      timeout_seconds: 180,
    }, "live", turnId);
  });
}

beforeEach(() => {
  mocks.list.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  _resetConfirmationPresenceForTesting();
  setTitleAttentionCount(0);
  window.history.replaceState({}, "", "/");
});

describe("ToolConfirmationNotice", () => {
  it("groups waiting items by session and skips the ones whose card is on screen", async () => {
    mocks.list.mockResolvedValue([
      summary("ttc_a", "s_one", "Quarterly review"),
      summary("ttc_b", "s_one", "Quarterly review"),
      summary("ttc_c", "s_two", null),
      summary("ttc_visible", "s_three", "Open here"),
    ]);
    act(() => setConfirmationCardVisible("ttc_visible", true));
    render(<ToolConfirmationNotice />);

    expect(await screen.findByText('noticeSession {"title":"Quarterly review","count":2}')).toBeInTheDocument();
    expect(screen.getByText('noticeSession {"title":"untitled","count":1}')).toBeInTheDocument();
    expect(screen.queryByText(/Open here/)).toBeNull();
    expect(screen.getByRole("status")).toBeInTheDocument();
    // The visible card still counts as waiting in the tab title.
    expect(getTitleAttentionSnapshot()).toBe(4);

    // Scrolled out of view: the notice lists it again.
    act(() => setConfirmationCardVisible("ttc_visible", false));
    expect(screen.getByText('noticeSession {"title":"Open here","count":1}')).toBeInTheDocument();
  });

  it("opens the session and asks its card to take focus", async () => {
    mocks.list.mockResolvedValue([summary("ttc_a", "s one", "Quarterly review")]);
    render(<ToolConfirmationNotice />);
    fireEvent.click(await screen.findByRole("button", { name: "view" }));
    expect(mocks.push).toHaveBeenCalledWith("/?session=s%20one");
    expect(consumeConfirmationFocus("ttc_a")).toBe(true);
  });

  it("scrolls to a card already in the page instead of navigating", async () => {
    mocks.list.mockResolvedValue([summary("ttc_a", "s_one", "Quarterly review")]);
    window.history.replaceState({}, "", "/?session=s_one");
    const card = document.createElement("div");
    card.dataset.toolConfirmationId = "ttc_a";
    card.tabIndex = -1;
    document.body.appendChild(card);
    render(<ToolConfirmationNotice />);
    fireEvent.click(await screen.findByRole("button", { name: "view" }));
    expect(document.activeElement).toBe(card);
    expect(mocks.push).not.toHaveBeenCalled();
    card.remove();
  });

  it("counts a request streamed to this tab before any server read sees it, and restores the title", async () => {
    let resolveRead: (items: ToolConfirmationSummary[]) => void = () => undefined;
    mocks.list.mockImplementation(() => new Promise((resolve) => { resolveRead = resolve; }));
    const view = render(<ToolConfirmationNotice />);
    // The mount read started before the event arrived.
    await waitFor(() => expect(mocks.list).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 2));
    streamRequest("t_live", "ttc_live");
    streamRequest("t_other", "ttc_teammate", "u_teammate");
    expect(screen.getByText('noticeCurrent {"count":1}')).toBeInTheDocument();
    expect(getTitleAttentionSnapshot()).toBe(1);

    // A read that started before the event and does not list it does not hide it.
    await act(async () => resolveRead([]));
    expect(getTitleAttentionSnapshot()).toBe(1);

    act(() => {
      dispatchToTurnStore("confirmation_resolved", {
        card_id: "t_live",
        step_id: "step_ttc_live",
        confirmation_id: "ttc_live",
        outcome: "approved",
        decided_at: "2026-11-08T10:01:00Z",
      }, "live", "t_live");
    });
    expect(getTitleAttentionSnapshot()).toBe(0);
    expect(screen.queryByText(/noticeCurrent/)).toBeNull();

    streamRequest("t_live2", "ttc_live2");
    expect(getTitleAttentionSnapshot()).toBe(1);
    view.unmount();
    expect(getTitleAttentionSnapshot()).toBe(0);
  });

  it("drops a stale copy from a stream that has closed once a later server read omits it", async () => {
    streamRequest("t_closed", "ttc_stale");
    await new Promise((resolve) => setTimeout(resolve, 2));
    render(<ToolConfirmationNotice />);
    await waitFor(() => expect(mocks.list).toHaveBeenCalled());
    await waitFor(() => expect(getTitleAttentionSnapshot()).toBe(0));
    expect(screen.queryByText(/noticeCurrent/)).toBeNull();
  });

  it("ignores a server item the turn store has already seen decided", async () => {
    act(() => {
      dispatchToTurnStore("confirmation_requested", {
        card_id: "t_done",
        step_id: "step_ttc_done",
        confirmation_id: "ttc_done",
        kind: "email_send",
        requester_user_id: "u_owner",
        title: "send_email",
        choices: ["reject", "approve"],
        expires_at: "2026-11-08T10:03:00Z",
        timeout_seconds: 180,
      }, "live", "t_done");
      dispatchToTurnStore("confirmation_resolved", {
        card_id: "t_done",
        step_id: "step_ttc_done",
        confirmation_id: "ttc_done",
        outcome: "rejected",
        decided_at: "2026-11-08T10:01:00Z",
      }, "live", "t_done");
    });
    mocks.list.mockResolvedValue([summary("ttc_done", "s_one", "Mail")]);
    render(<ToolConfirmationNotice />);
    await waitFor(() => expect(mocks.list).toHaveBeenCalled());
    await act(async () => undefined);
    expect(screen.queryByText(/noticeSession/)).toBeNull();
    expect(getTitleAttentionSnapshot()).toBe(0);
  });
});
