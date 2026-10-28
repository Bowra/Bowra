import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { GoogleAuthorization } from "@/lib/google-workspace-api";

const api = vi.hoisted(() => ({
  fetchMyGoogleAuthorization: vi.fn(),
  beginGoogleAuthorization: vi.fn(),
  disconnectGoogle: vi.fn(),
}));
vi.mock("@/lib/google-workspace-api", async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
const session = vi.hoisted(() => ({ googleWorkspace: true as boolean | undefined }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { google_workspace: session.googleWorkspace } } }),
}));

import { GoogleConnectionCard } from "./google-connection-card";

function authorization(overrides: Partial<GoogleAuthorization> = {}): GoogleAuthorization {
  return {
    connection_id: "ibt_1",
    name: "Google Workspace",
    connection_status: "enabled",
    status: "active",
    email: "member@acme.example",
    bundles: [
      { id: "base", enabled: true, granted: true },
      { id: "read_by_link", enabled: true, granted: true },
      { id: "edit_docs", enabled: true, granted: true },
      { id: "edit_sheets", enabled: true, granted: true },
      { id: "calendar", enabled: false, granted: false },
    ],
    needs_update: false,
    connected_at: "2026-09-30T10:00:00Z",
    last_used_at: "2026-10-01T09:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  session.googleWorkspace = true;
  window.history.replaceState({}, "", "/connections/");
});
afterEach(cleanup);

describe("Google card in My connections", () => {
  it.each([false, undefined])(
    "does not request the member authorization or render when the google_workspace capability is %s",
    async (capability) => {
      session.googleWorkspace = capability;
      api.fetchMyGoogleAuthorization.mockResolvedValue({ available: true, authorization: authorization() });
      const { container } = renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
      // Let any mount effects flush before asserting that nothing was requested.
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(api.fetchMyGoogleAuthorization).not.toHaveBeenCalled();
      expect(container.textContent).toBe("");
      expect(screen.queryByTestId("google-connection-card")).toBeNull();
    },
  );

  it("requests the member authorization and renders the card when the google_workspace capability is on", async () => {
    api.fetchMyGoogleAuthorization.mockResolvedValue({ available: true, authorization: authorization() });
    renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    expect(await screen.findByTestId("google-connection-card")).toBeTruthy();
    expect(api.fetchMyGoogleAuthorization).toHaveBeenCalledTimes(1);
  });

  it("shows a load failure instead of looking unavailable", async () => {
    api.fetchMyGoogleAuthorization.mockRejectedValue(new Error("boom"));
    renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    expect(await screen.findByText(/could not be loaded/)).toBeTruthy();
  });

  it("is hidden unless Google Workspace is available to the team", async () => {
    api.fetchMyGoogleAuthorization.mockResolvedValue({ available: false, authorization: null });
    const { container } = renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    await waitFor(() => expect(api.fetchMyGoogleAuthorization).toHaveBeenCalled());
    expect(container.textContent).toBe("");

    cleanup();
    api.fetchMyGoogleAuthorization.mockResolvedValue(null); // deployment switch off
    const second = renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    await waitFor(() => expect(api.fetchMyGoogleAuthorization).toHaveBeenCalledTimes(2));
    expect(second.container.textContent).toBe("");
  });

  it("offers to connect when not connected and redirects to Google", async () => {
    api.fetchMyGoogleAuthorization.mockResolvedValue({ available: true, authorization: authorization({ status: "not_connected", email: null }) });
    api.beginGoogleAuthorization.mockResolvedValue("https://accounts.google.com/o/oauth2/v2/auth?x=1");
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, search: "" });
    renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    fireEvent.click(await screen.findByRole("button", { name: "Connect Google" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?x=1"));
    expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();
    vi.unstubAllGlobals();
  });

  it("shows the account and granted capabilities, and disconnects after confirming", async () => {
    api.fetchMyGoogleAuthorization.mockResolvedValueOnce({ available: true, authorization: authorization() });
    api.fetchMyGoogleAuthorization.mockResolvedValueOnce({ available: true, authorization: authorization({ status: "not_connected", email: null }) });
    api.disconnectGoogle.mockResolvedValue(undefined);
    renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    expect(await screen.findByText("Account: member@acme.example")).toBeTruthy();
    expect(screen.getByText("Granted: Basic, Read by link, Edit existing Docs and Slides, Write to existing Sheets")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(await screen.findByText("Disconnect your Google account?")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Disconnect" }).at(-1)!);
    await waitFor(() => expect(api.disconnectGoogle).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("button", { name: "Connect Google" })).toBeTruthy();
  });

  it.each([
    [{ status: "needs_reauth" as const }, "Reconnect", /expired or was revoked/],
    [{ needs_update: true }, "Update authorization", /turned on new capabilities/],
  ])("guides the member when %o", async (overrides, action, message) => {
    api.fetchMyGoogleAuthorization.mockResolvedValue({ available: true, authorization: authorization(overrides) });
    renderIntl(<GoogleConnectionCard />, { locale: "en-US" });
    expect(await screen.findByRole("button", { name: action })).toBeTruthy();
    expect(screen.getByText(message)).toBeTruthy();
  });

  it("shows the OAuth return result in the member's language", async () => {
    window.history.replaceState({}, "", "/connections/?google=connected");
    api.fetchMyGoogleAuthorization.mockResolvedValue({ available: true, authorization: authorization() });
    renderIntl(<GoogleConnectionCard />, { locale: "zh-CN" });
    expect(await screen.findByText("已连接 Google 账号。")).toBeTruthy();
    expect(window.location.search).toBe("");
  });
});
