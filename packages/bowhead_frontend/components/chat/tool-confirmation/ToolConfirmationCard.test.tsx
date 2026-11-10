import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { ToolConfirmationDetail } from "@/lib/tool-confirmation-api";
import {
  _resetConfirmationPresenceForTesting,
  requestConfirmationFocus,
} from "@/lib/tool-confirmation-presence";
import { _resetAllTurnsForTesting, type StepConfirmation, useTurn } from "@/lib/turn-store";
import { _resetNarrativeCursorsForTesting, dispatchToTurnStore } from "@/lib/turn-store-dispatch";

const mocks = vi.hoisted(() => ({
  getDetail: vi.fn(),
  decide: vi.fn(),
  viewerId: "u_owner" as string | null,
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations:
    (namespace?: string) => (key: string, values?: Record<string, unknown>) =>
      `${namespace ? `${namespace}.` : ""}${key}${values ? ` ${JSON.stringify(values)}` : ""}`,
}));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => (mocks.viewerId ? { me: { id: mocks.viewerId } } : { me: null }),
}));
vi.mock("@/lib/tool-confirmation-api", () => ({
  getToolConfirmation: mocks.getDetail,
  decideToolConfirmation: mocks.decide,
}));

import { ToolConfirmationCard, ToolConfirmationRecord } from "./ToolConfirmationCard";

const T = "t_card";

function confirmation(over: Partial<StepConfirmation> = {}): StepConfirmation {
  return {
    id: "ttc_one",
    kind: "team_mcp",
    requesterUserId: "u_owner",
    title: "delete_contact",
    sourceLabel: "CRM",
    category: "destructive",
    choices: ["reject", "approve", "approve_session"],
    expiresAt: "2026-11-08T10:03:00Z",
    timeoutSeconds: 180,
    receivedAt: Date.now(),
    ...over,
  };
}

function teamDetail(over: Partial<ToolConfirmationDetail> = {}): ToolConfirmationDetail {
  return {
    id: "ttc_one",
    kind: "team_mcp",
    session_id: "s_one",
    session_title: "Contacts",
    turn_id: T,
    step_id: "step_call_1",
    title: "delete_contact",
    source_label: "CRM",
    agent_name: "Compute Agent",
    // Deliberately far from the local clock: the countdown must not use it.
    expires_at: "2001-01-01T00:00:00Z",
    remaining_seconds: 42,
    category: "destructive",
    choices: ["reject", "approve", "approve_session"],
    item: {
      id: "ttc_one",
      turn_id: T,
      session_id: "s_one",
      team_id: "team_a",
      source_name: "CRM",
      tool_ref: "teammcp.crm.delete_contact",
      tool_name: "delete_contact",
      description: "Delete a contact",
      category: "destructive",
      arguments: { contactId: "contact-123", note: "x".repeat(400) },
      expires_at: "2001-01-01T00:00:00Z",
    },
    ...over,
  } as ToolConfirmationDetail;
}

/** Renders the card from the turn store, the way the step list does. */
function Connected() {
  const step = useTurn(T)?.steps[0];
  return step?.confirmation ? <ToolConfirmationCard confirmation={step.confirmation} agentName="Trace Agent" /> : null;
}

function seedRequest(over: Record<string, unknown> = {}) {
  act(() => {
    dispatchToTurnStore("step_started", {
      seq: 1,
      card_id: T,
      step_id: "step_call_1",
      step_type: "tool_run",
      tool_name: "teammcp.crm.delete_contact",
      header_zh: "Team tool",
      started_at: "2026-11-08T10:00:00Z",
    }, "live", T);
    dispatchToTurnStore("confirmation_requested", {
      seq: 2,
      card_id: T,
      step_id: "step_call_1",
      call_id: "call_1",
      confirmation_id: "ttc_one",
      kind: "team_mcp",
      requester_user_id: "u_owner",
      title: "delete_contact",
      source_label: "CRM",
      category: "destructive",
      choices: ["reject", "approve", "approve_session"],
      expires_at: "2026-11-08T10:03:00Z",
      timeout_seconds: 180,
      ...over,
    }, "live", T);
  });
}

beforeEach(() => {
  mocks.viewerId = "u_owner";
  mocks.getDetail.mockResolvedValue(teamDetail());
  mocks.decide.mockResolvedValue({ ok: true });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
  _resetAllTurnsForTesting();
  _resetNarrativeCursorsForTesting();
  _resetConfirmationPresenceForTesting();
});

describe("ToolConfirmationCard for the requester", () => {
  it("fetches the preview once and shows who, what, the risk and the arguments", async () => {
    render(<ToolConfirmationCard confirmation={confirmation()} agentName="Trace Agent" />);
    expect(await screen.findByText("contact-123")).toBeInTheDocument();
    expect(mocks.getDetail).toHaveBeenCalledTimes(1);
    expect(mocks.getDetail).toHaveBeenCalledWith("ttc_one");
    // The server's agent name wins over the trace label.
    expect(screen.getByText(/toolConfirmation\.requestCallFrom .*"agent":"Compute Agent".*"tool":"delete_contact".*"source":"CRM"/)).toBeInTheDocument();
    expect(screen.getByText("teamToolConfirmation.riskDestructive")).toBeInTheDocument();
    expect(screen.getByText("Delete a contact")).toBeInTheDocument();
    // Long values fold behind a preview; the raw JSON stays one expansion away.
    expect(screen.getByText("toolConfirmation.argumentShowAll")).toBeInTheDocument();
    expect(screen.getByText("toolConfirmation.argumentsRaw")).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "teamToolConfirmation.reject",
      "teamToolConfirmation.approveOnce",
      "teamToolConfirmation.approveSession",
    ]);
  });

  it("does not take focus when it appears", async () => {
    const composer = document.createElement("textarea");
    document.body.appendChild(composer);
    composer.focus();
    render(<ToolConfirmationCard confirmation={confirmation()} />);
    await screen.findByText("contact-123");
    expect(document.activeElement).toBe(composer);
    composer.remove();
  });

  it("takes focus when the user arrived through the notice", async () => {
    requestConfirmationFocus("ttc_one");
    const { container } = render(<ToolConfirmationCard confirmation={confirmation()} />);
    await waitFor(() => expect(document.activeElement).toBe(container.querySelector("[data-tool-confirmation-id='ttc_one']")));
  });

  it("posts the chosen decision and records the outcome only from confirmation_resolved", async () => {
    seedRequest();
    render(<Connected />);
    fireEvent.click(await screen.findByRole("button", { name: "teamToolConfirmation.approveSession" }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledWith("ttc_one", "approve_session"));
    expect(await screen.findByText("toolConfirmation.submitted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /teamToolConfirmation/ })).toBeNull();
    expect(document.querySelector("[data-outcome]")).toBeNull();

    act(() => {
      dispatchToTurnStore("confirmation_resolved", {
        seq: 3,
        card_id: T,
        step_id: "step_call_1",
        confirmation_id: "ttc_one",
        outcome: "approved_session",
        decided_at: "2026-11-08T10:01:00Z",
      }, "live", T);
    });
    const record = document.querySelector("[data-outcome='approved_session']");
    expect(record?.textContent).toMatch(/^toolConfirmation\.recordApprovedSession · \d/);
    expect(screen.queryByText("toolConfirmation.submitted")).toBeNull();
  });

  it("anchors the countdown to the server's remaining seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<ToolConfirmationCard confirmation={confirmation({ timeoutSeconds: 180 })} />);
    expect(await screen.findByText(/toolConfirmation\.countdownSkip \{"time":"0:42"\}/)).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    expect(screen.getByText(/toolConfirmation\.countdownSkip \{"time":"0:40"\}/)).toBeInTheDocument();
  });

  it("falls back to the event's remaining seconds while the preview is unavailable", async () => {
    mocks.getDetail.mockRejectedValue(new Error("offline"));
    render(<ToolConfirmationCard confirmation={confirmation({ timeoutSeconds: 95 })} />);
    expect(await screen.findByText(/toolConfirmation\.detailsFailed/)).toBeInTheDocument();
    expect(screen.getByText(/toolConfirmation\.countdownSkip \{"time":"1:3[45]"\}/)).toBeInTheDocument();
    // Nothing can be allowed without seeing what will be sent.
    expect(screen.queryByRole("button", { name: /teamToolConfirmation/ })).toBeNull();
    mocks.getDetail.mockResolvedValue(teamDetail());
    fireEvent.click(screen.getByRole("button", { name: "toolConfirmation.retry" }));
    expect(await screen.findByRole("button", { name: "teamToolConfirmation.reject" })).toBeInTheDocument();
  });

  it("says when the confirmation is gone", async () => {
    mocks.getDetail.mockRejectedValue(new ApiError(404, "gone", { code: "tool_confirmation_not_found" }));
    render(<ToolConfirmationCard confirmation={confirmation()} />);
    expect(await screen.findByText("toolConfirmation.detailsGone")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("explains an already decided confirmation and offers no further choice", async () => {
    mocks.decide.mockRejectedValue(new ApiError(409, "taken", { code: "tool_confirmation_unavailable" }));
    render(<ToolConfirmationCard confirmation={confirmation()} />);
    fireEvent.click(await screen.findByRole("button", { name: "teamToolConfirmation.approveOnce" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("toolConfirmation.decisionUnavailable");
    expect(screen.queryByRole("button", { name: /teamToolConfirmation/ })).toBeNull();
  });

  it("lets the user choose again after a failed submission", async () => {
    mocks.decide.mockRejectedValueOnce(new Error("network"));
    render(<ToolConfirmationCard confirmation={confirmation()} />);
    fireEvent.click(await screen.findByRole("button", { name: "teamToolConfirmation.reject" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("toolConfirmation.decisionFailed");
    const reject = screen.getByRole("button", { name: "teamToolConfirmation.reject" });
    expect(reject).not.toBeDisabled();
    fireEvent.click(reject);
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledTimes(2));
  });

  it("previews an email with its own labels", async () => {
    mocks.getDetail.mockResolvedValue({
      ...teamDetail(),
      id: "esc_one",
      kind: "email_send",
      category: null,
      choices: ["reject", "approve"],
      item: {
        id: "esc_one",
        turn_id: T,
        session_id: "s_one",
        to: "customer@example.com",
        subject: "Monthly summary",
        body: "Revenue rose 4%.",
        expires_at: "2001-01-01T00:00:00Z",
      },
    });
    render(
      <ToolConfirmationCard
        confirmation={confirmation({ id: "esc_one", kind: "email_send", category: null, sourceLabel: null, choices: ["reject", "approve"] })}
      />,
    );
    expect(await screen.findByText("Revenue rose 4%.")).toBeInTheDocument();
    expect(screen.getByText("emailSendConfirmation.warning")).toBeInTheDocument();
    expect(screen.getByText(/toolConfirmation\.countdownEmail/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "emailSendConfirmation.approve" }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledWith("esc_one", "approve"));
  });
});

describe("ToolConfirmationCard for anyone else", () => {
  it("shows a waiting line without buttons and never requests the preview", () => {
    mocks.viewerId = "u_teammate";
    render(<ToolConfirmationCard confirmation={confirmation()} />);
    expect(screen.getByText("toolConfirmation.waitingForRequester")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(mocks.getDetail).not.toHaveBeenCalled();
  });
});

describe("ToolConfirmationRecord", () => {
  it.each([
    ["approved", "team_mcp", "toolConfirmation.recordApproved · "],
    ["approved", "email_send", "toolConfirmation.recordApprovedEmail · "],
    ["approved", "shopify_content", "toolConfirmation.recordApprovedShopify · "],
    ["rejected", "team_mcp", "toolConfirmation.recordRejected · "],
  ] as const)("shows %s (%s) with the decision time", (outcome, kind, prefix) => {
    render(<ToolConfirmationRecord confirmation={confirmation({ outcome, kind, decidedAt: "2026-11-08T10:01:00Z" })} />);
    expect(document.querySelector("[data-outcome]")?.textContent?.startsWith(prefix)).toBe(true);
  });

  it.each([
    ["expired", "toolConfirmation.recordExpired"],
    ["stopped", "toolConfirmation.recordStopped"],
    ["ended", "toolConfirmation.recordEnded"],
    ["unavailable", "toolConfirmation.recordUnavailable"],
  ] as const)("shows %s without a time", (outcome, text) => {
    render(<ToolConfirmationRecord confirmation={confirmation({ outcome, decidedAt: "2026-11-08T10:01:00Z" })} />);
    expect(document.querySelector("[data-outcome]")?.textContent).toBe(text);
  });

  it("is what every viewer sees once decided", () => {
    mocks.viewerId = "u_teammate";
    render(<ToolConfirmationCard confirmation={confirmation({ outcome: "rejected", decidedAt: "2026-11-08T10:01:00Z" })} />);
    expect(document.querySelector("[data-outcome='rejected']")).not.toBeNull();
    expect(mocks.getDetail).not.toHaveBeenCalled();
  });
});
