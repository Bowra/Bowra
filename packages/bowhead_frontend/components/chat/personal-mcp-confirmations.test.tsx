import { afterEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("next-intl", () => ({
  useLocale: () => "zh-CN",
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
}));
vi.mock("@/lib/personal-mcp-api", () => ({ personalMcpApi: mocks.api }));
import {
  CONFIRMATION_POLL_MS,
  PersonalMcpConfirmations,
} from "./personal-mcp-confirmations";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("personal MCP consent", () => {
  it("renders on the top layer outside the app shell", async () => {
    // Desktop shell .v2-app-root creates its own stacking context; rendering in place would put this beneath an open dialog
    // (See Email Send Confirmation in e2e/layer-stacking.spec.ts for real-browser evidence; both use the same shell structure).
    mocks.api.mockImplementation(async (path: string) =>
      path === "/capabilities"
        ? { enabled: true }
        : {
            confirmations: [{
              id: "pmi_layer", turn_id: "t", session_id: "s", agent_name: "a", connection_name: "c",
              tool_name: "t", description: "d", arguments: {}, expires_at: "2030-01-01T00:00:00Z",
            }],
          },
    );
    const { container } = render(<PersonalMcpConfirmations />);
    const dialog = await screen.findByRole("dialog");
    expect(container.contains(dialog)).toBe(false);
    expect(dialog.closest<HTMLElement>("[data-settings-dialog-portal]")?.style.zIndex).toBe("var(--z-dialog-top)");
  });

  it("shows complete parameters without submitting consent until the user decides", async () => {
    const confirmation = {
      id: "pmi_one",
      turn_id: "t_one",
      session_id: "s_one",
      agent_name: "My agent",
      connection_name: "My CRM",
      tool_name: "deleteContact",
      description: "Delete contact",
      arguments: { contactId: "contact-123", irreversible: true },
      expires_at: "2030-01-01T00:00:00Z",
    };
    mocks.api.mockImplementation(async (path: string) =>
      path === "/capabilities"
        ? { enabled: true }
        : path === "/calls/confirmations"
          ? { confirmations: [confirmation] }
          : { status: "approved" },
    );
    render(<PersonalMcpConfirmations />);
    await screen.findByRole("dialog");
    expect(screen.getByText(/contact-123/)).toBeTruthy();
    expect(
      mocks.api.mock.calls.some((call) => call[0].endsWith("/decision")),
    ).toBe(false);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "reject" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "approveOnce" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith(
        "/calls/pmi_one/decision",
        "POST",
        { decision: "approve" },
      ),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
  it("allows the owner to permit this exact tool for the rest of the session", async () => {
    mocks.api.mockImplementation(async (path: string) =>
      path === "/capabilities"
        ? { enabled: true }
        : path === "/calls/confirmations"
          ? {
              confirmations: [
                {
                  id: "pmi_one",
                  agent_name: "Agent",
                  connection_name: "CRM",
                  tool_name: "search",
                  description: "Search",
                  arguments: { query: "example" },
                  expires_at: "2030-01-01T00:00:00Z",
                },
              ],
            }
          : { status: "approved" },
    );
    render(<PersonalMcpConfirmations />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "approveSession" }));
    await waitFor(() =>
      expect(mocks.api).toHaveBeenCalledWith(
        "/calls/pmi_one/decision",
        "POST",
        { decision: "approve_session" },
      ),
    );
  });
  it("polls at a bounded interval, never overlaps requests, and pauses while the page is hidden", async () => {
    vi.useFakeTimers();
    let visibility: DocumentVisibilityState = "visible";
    const visibilitySpy = vi
      .spyOn(document, "visibilityState", "get")
      .mockImplementation(() => visibility);
    try {
      const resolvers: Array<(value: unknown) => void> = [];
      mocks.api.mockImplementation((path: string) =>
        path === "/capabilities"
          ? Promise.resolve({ enabled: true })
          : new Promise((resolve) => resolvers.push(resolve)),
      );
      const reads = () =>
        mocks.api.mock.calls.filter((call) => call[0] === "/calls/confirmations")
          .length;
      render(<PersonalMcpConfirmations />);
      await vi.advanceTimersByTimeAsync(0);
      expect(reads()).toBe(1);

      // A slow response must not stack further reads, even across a focus return.
      await vi.advanceTimersByTimeAsync(CONFIRMATION_POLL_MS * 6);
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(reads()).toBe(1);

      resolvers.shift()!({ confirmations: [] });
      await vi.advanceTimersByTimeAsync(CONFIRMATION_POLL_MS - 1);
      expect(reads()).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(reads()).toBe(2);
      expect(CONFIRMATION_POLL_MS).toBeGreaterThanOrEqual(5000);

      // Hidden: no reads at all; visible again: read immediately.
      visibility = "hidden";
      document.dispatchEvent(new Event("visibilitychange"));
      resolvers.shift()!({ confirmations: [] });
      await vi.advanceTimersByTimeAsync(CONFIRMATION_POLL_MS * 10);
      expect(reads()).toBe(2);
      visibility = "visible";
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
      expect(reads()).toBe(3);
    } finally {
      visibilitySpy.mockRestore();
      vi.useRealTimers();
    }
  });
  it("formats the decision deadline in the interface locale, not the browser default", async () => {
    const expiresAt = "2030-01-01T07:50:52Z";
    mocks.api.mockImplementation(async (path: string) =>
      path === "/capabilities"
        ? { enabled: true }
        : {
            confirmations: [
              {
                id: "pmi_one",
                agent_name: "Agent",
                connection_name: "CRM",
                tool_name: "search",
                description: "Search",
                arguments: {},
                expires_at: expiresAt,
              },
            ],
          },
    );
    render(<PersonalMcpConfirmations />);
    await screen.findByRole("dialog");
    const deadline = screen.getByText(/^expires /);
    expect(deadline.textContent).toBe(
      `expires ${JSON.stringify({ time: new Date(expiresAt).toLocaleTimeString("zh-CN") })}`,
    );
    expect(deadline.textContent).not.toMatch(/AM|PM/);
  });
  it("keeps the confirmation visible when the decision response is uncertain", async () => {
    mocks.api.mockImplementation(async (path: string) => {
      if (path === "/capabilities") return { enabled: true };
      if (path === "/calls/confirmations")
        return {
          confirmations: [
            {
              id: "pmi_one",
              agent_name: "Agent",
              connection_name: "CRM",
              tool_name: "search",
              description: "Search",
              arguments: { query: "example" },
              expires_at: "2030-01-01T00:00:00Z",
            },
          ],
        };
      throw new Error("network");
    });
    render(<PersonalMcpConfirmations />);
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "reject" }));
    await screen.findByRole("alert");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(
      mocks.api.mock.calls.filter((call) => call[0].endsWith("/decision")),
    ).toHaveLength(1);
  });
});
