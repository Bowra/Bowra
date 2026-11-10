import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  active: vi.fn(() => false),
  me: { capabilities: { team_tool_confirmations: true } } as { capabilities: Record<string, boolean> },
}));
vi.mock("next-intl", () => ({
  useLocale: () => "en-US",
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
}));
vi.mock("@/lib/team-tool-confirmation-api", () => ({ teamToolConfirmationApi: mocks.api }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useAuthSession: () => ({ me: mocks.me }) }));
vi.mock("@/lib/turn-store", () => ({ useHasActiveToolStep: mocks.active }));
import { TeamToolConfirmations } from "./team-tool-confirmations";

const confirmation = {
  id: "ttc_one",
  turn_id: "t_one",
  session_id: "s_one",
  team_id: "team_a",
  source_name: "CRM",
  tool_ref: "teammcp.ots_crm.delete_contact",
  tool_name: "delete_contact",
  description: "Delete a contact",
  arguments: { contactId: "contact-123" },
  expires_at: "2030-01-01T00:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  mocks.active.mockReturnValue(false);
  mocks.me = { capabilities: { team_tool_confirmations: true } };
});

describe("Team tool call confirmation", () => {
  it("polls nothing when the deployment has not enabled it", () => {
    mocks.me = { capabilities: {} };
    render(<TeamToolConfirmations />);
    expect(mocks.api).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the exact call on the top layer and decides only on a button", async () => {
    mocks.api.mockImplementation(async (path: string) => (path === "" ? { confirmations: [confirmation] } : { ok: true }));
    const { container } = render(<TeamToolConfirmations />);
    const dialog = await screen.findByRole("dialog");
    expect(container.contains(dialog)).toBe(false);
    expect(screen.getByText("CRM · delete_contact")).toBeTruthy();
    expect(screen.getByText(/contact-123/)).toBeTruthy();
    // A server that omits the category reads as unknown.
    expect(screen.getByText(/riskUnknown/)).toBeTruthy();
    expect(mocks.api.mock.calls.some((call) => call[0].endsWith("/decision"))).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "reject" }));
    fireEvent.click(screen.getByRole("button", { name: "approveSession" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith("/ttc_one/decision", "POST", { decision: "approve_session" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("states the risk of the tool's category", async () => {
    mocks.api.mockResolvedValue({ confirmations: [{ ...confirmation, category: "destructive" }] });
    render(<TeamToolConfirmations />);
    await screen.findByRole("dialog");
    expect(screen.getByText(/riskDestructive/)).toBeTruthy();
    expect(screen.queryByText(/riskUnknown|riskExternalWrite/)).toBeNull();
  });

  it("checks once on mount and keeps polling only while a Team tool step runs", async () => {
    vi.useFakeTimers();
    try {
      const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));
      mocks.api.mockResolvedValue({ confirmations: [] });
      const { rerender } = render(<TeamToolConfirmations />);
      await tick(0);
      expect(mocks.api).toHaveBeenCalledTimes(1);
      expect(mocks.active).toHaveBeenCalledWith("teammcp.", "prefix");
      await tick(20_000);
      expect(mocks.api).toHaveBeenCalledTimes(1);
      mocks.active.mockReturnValue(true);
      rerender(<TeamToolConfirmations />);
      await tick(5_000);
      expect(mocks.api.mock.calls.length).toBeGreaterThan(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the call pending and says so when the decision is not recorded", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "") return { confirmations: [confirmation] };
      throw new Error("conflict");
    });
    render(<TeamToolConfirmations />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "approveOnce" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});
