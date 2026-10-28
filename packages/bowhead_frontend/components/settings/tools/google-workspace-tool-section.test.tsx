import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { GoogleConnection, GoogleConnectionState } from "@/lib/google-workspace-api";
import type { Locale } from "@/lib/locale";

const api = vi.hoisted(() => ({
  fetchGoogleConnection: vi.fn(),
  saveGoogleConnection: vi.fn(),
  setGoogleConnectionEnabled: vi.fn(),
  deleteGoogleConnection: vi.fn(),
  beginGoogleAuthorization: vi.fn(),
}));
vi.mock("@/lib/google-workspace-api", async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));
const session = vi.hoisted(() => ({ googleWorkspace: true as boolean | undefined }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ me: { capabilities: { google_workspace: session.googleWorkspace } } }),
}));

import { GoogleWorkspaceToolSection } from "./google-workspace-tool-section";
import { useState } from "react";

/** Mirrors the Tools page: the picker bumps a request counter to open the form. */
function Harness({ onAvailableChange }: { onAvailableChange?: (available: boolean, configured: boolean) => void }) {
  const [request, setRequest] = useState(0);
  return <>
    <button type="button" onClick={() => setRequest((value) => value + 1)}>open-config</button>
    <GoogleWorkspaceToolSection createRequest={request} onAvailableChange={onAvailableChange} />
  </>;
}
import deMessages from "@/messages/de-DE.json";
import enMessages from "@/messages/en-US.json";
import frMessages from "@/messages/fr-FR.json";
import zhMessages from "@/messages/zh-CN.json";

const BUNDLES = [
  { id: "base", enabled: true, required: true, scopes: ["openid", "https://www.googleapis.com/auth/drive.file"] },
  { id: "read_by_link", enabled: true, required: false, scopes: ["https://www.googleapis.com/auth/documents.readonly"] },
  { id: "edit_docs", enabled: true, required: false, scopes: ["https://www.googleapis.com/auth/documents"] },
  { id: "edit_sheets", enabled: true, required: false, scopes: ["https://www.googleapis.com/auth/spreadsheets"] },
  { id: "calendar", enabled: false, required: false, scopes: ["https://www.googleapis.com/auth/calendar.events.readonly"] },
] as const;

function connection(overrides: Partial<GoogleConnection> = {}): GoogleConnection {
  return {
    id: "ibt_1",
    provider: "google_workspace",
    name: "Google Workspace",
    status: "disabled",
    client_id: "client-1",
    client_secret_configured: true,
    picker_api_key_configured: true,
    picker_project_number: "123",
    domain: "acme.example",
    bundles: BUNDLES.map((bundle) => ({ ...bundle, scopes: [...bundle.scopes] })),
    scopes: ["openid", "https://www.googleapis.com/auth/drive.file"],
    redirect_uri: "https://bowhead.example/api/v1/google/oauth/callback",
    javascript_origin: "https://bowhead.example",
    authorized_members: 3,
    created_at: "t",
    updated_at: "t",
    ...overrides,
  };
}

function state(conn: GoogleConnection | null): GoogleConnectionState {
  return conn ? { connection: conn } : { connection: null, redirect_uri: "https://bowhead.example/api/v1/google/oauth/callback", javascript_origin: "https://bowhead.example", bundles: BUNDLES.map((bundle) => ({ ...bundle, scopes: [...bundle.scopes] })) };
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  session.googleWorkspace = true;
  window.history.replaceState({}, "", "/team/settings/tools/");
});
afterEach(cleanup);

describe("Google Workspace on the Tools page", () => {
  it.each([false, undefined])("does not request the connection or render when the google_workspace capability is %s", async (capability) => {
    session.googleWorkspace = capability;
    api.fetchGoogleConnection.mockResolvedValue(state(connection({ status: "enabled" })));
    const onAvailable = vi.fn();
    const { container } = renderIntl(<GoogleWorkspaceToolSection createRequest={0} onAvailableChange={onAvailable} />, { locale: "en-US" });
    // Let any mount effects flush before asserting that nothing was requested.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(api.fetchGoogleConnection).not.toHaveBeenCalled();
    expect(onAvailable).not.toHaveBeenCalled();
    expect(container.textContent).toBe("");
  });

  it("renders nothing when the connection route reports the feature off", async () => {
    api.fetchGoogleConnection.mockResolvedValue(null);
    const onAvailable = vi.fn();
    const { container } = renderIntl(<GoogleWorkspaceToolSection createRequest={0} onAvailableChange={onAvailable} />, { locale: "en-US" });
    await waitFor(() => expect(onAvailable).toHaveBeenCalledWith(false, false));
    expect(container.textContent).toBe("");
  });

  it("shows status and the number of authorized members", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(connection({ status: "enabled" })));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale: "en-US" });
    expect(await screen.findByText(/Enabled · acme\.example · 3 members authorized/)).toBeTruthy();
  });

  it.each(["zh-CN", "en-US"] as const)("bounds load errors in %s without displaying exception prose", async (locale) => {
    api.fetchGoogleConnection.mockRejectedValue(new Error("RAW_GOOGLE_DIAGNOSTIC"));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale });
    const messages = locale === "zh-CN" ? zhMessages : enMessages;
    expect(await screen.findByText(messages.google_workspace.errors.load)).toBeTruthy();
    expect(screen.queryByText("RAW_GOOGLE_DIAGNOSTIC")).toBeNull();
  });

  it("keeps an acknowledged connection change when refreshing its status fails", async () => {
    api.fetchGoogleConnection.mockResolvedValueOnce(state(connection()));
    api.setGoogleConnectionEnabled.mockResolvedValue(connection({ status: "enabled" }));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale: "en-US" });
    const toggle = await screen.findByRole("switch");
    api.fetchGoogleConnection.mockRejectedValueOnce(new Error("RAW_REFRESH_DIAGNOSTIC"));
    fireEvent.click(toggle);
    await screen.findByText(enMessages.google_workspace.errors.saved_refresh_failed);
    expect(screen.getByText(/Enabled · acme\.example/)).toBeTruthy();
    expect(api.setGoogleConnectionEnabled).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("RAW_REFRESH_DIAGNOSTIC")).toBeNull();
  });

  it("creates a connection with default bundles and the registration addresses", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(null));
    api.saveGoogleConnection.mockResolvedValue(connection());
    const onAvailable = vi.fn();
    renderIntl(<Harness onAvailableChange={onAvailable} />, { locale: "en-US" });
    await waitFor(() => expect(onAvailable).toHaveBeenCalledWith(true, false));
    fireEvent.click(screen.getByRole("button", { name: "open-config" }));

    expect(await screen.findByText("https://bowhead.example/api/v1/google/oauth/callback")).toBeTruthy();
    expect(screen.getByText("https://bowhead.example")).toBeTruthy();
    const calendar = screen.getByRole("checkbox", { name: /Calendar/ }) as HTMLInputElement;
    expect(calendar.checked).toBe(false);
    expect((screen.getByRole("checkbox", { name: /Basic/ }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-1" } });
    fireEvent.change(screen.getByLabelText("Client secret"), { target: { value: "secret-1" } });
    fireEvent.change(screen.getByLabelText("Workspace domain"), { target: { value: "acme.example" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.saveGoogleConnection).toHaveBeenCalledTimes(1));
    expect(api.saveGoogleConnection.mock.calls[0][0]).toEqual({
      name: "Google Workspace",
      client_id: "client-1",
      client_secret: "secret-1",
      domain: "acme.example",
      bundles: { base: true, read_by_link: true, edit_docs: true, edit_sheets: true, calendar: false },
      picker_project_number: "",
    });
  });

  it("keeps write-only secrets when left blank and confirms a client change first", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(connection()));
    api.saveGoogleConnection.mockResolvedValue(connection());
    renderIntl(<Harness />, { locale: "en-US" });
    await screen.findByTestId("google-workspace-tool");
    fireEvent.click(screen.getByRole("button", { name: "open-config" }));
    const secret = (await screen.findByLabelText("Client secret")) as HTMLInputElement;
    expect(secret.value).toBe("");
    expect(secret.placeholder).toMatch(/leave blank to keep/);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.saveGoogleConnection).toHaveBeenCalledTimes(1));
    const kept = api.saveGoogleConnection.mock.calls[0][0];
    expect("client_secret" in kept).toBe(false);
    expect("picker_api_key" in kept).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "open-config" }));
    fireEvent.change(await screen.findByLabelText("Client ID"), { target: { value: "client-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Change the OAuth client?")).toBeTruthy();
    expect(api.saveGoogleConnection).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Change and save" }));
    await waitFor(() => expect(api.saveGoogleConnection).toHaveBeenCalledTimes(2));
    expect(api.saveGoogleConnection.mock.calls[1][0].client_id).toBe("client-2");
  });

  it("explains that the admin must authorize before enabling", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(connection()));
    api.setGoogleConnectionEnabled.mockRejectedValue(Object.assign(new Error("x"), { code: "google_admin_authorization_required" }));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale: "en-US" });
    fireEvent.click(await screen.findByRole("switch"));
    expect(await screen.findByText(/authorize once with your own Google account/)).toBeTruthy();
    expect(api.setGoogleConnectionEnabled).toHaveBeenCalledWith(true);
  });

  it("shows the OAuth return result and removes it from the address", async () => {
    window.history.replaceState({}, "", "/team/settings/tools/?google=failed&google_reason=email_mismatch");
    api.fetchGoogleConnection.mockResolvedValue(state(connection()));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale: "en-US" });
    expect(await screen.findByText(/matches your Bowhead sign-in email/)).toBeTruthy();
    expect(window.location.search).toBe("");
  });

  it("shows a load failure instead of disappearing", async () => {
    api.fetchGoogleConnection.mockRejectedValue(new Error("Service unavailable"));
    renderIntl(<GoogleWorkspaceToolSection createRequest={0} />, { locale: "en-US" });
    expect(await screen.findByText(enMessages.google_workspace.errors.load)).toBeTruthy();
  });

  it("lists consent-screen scopes for the bundles ticked in the form", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(connection()));
    renderIntl(<Harness />, { locale: "en-US" });
    await screen.findByTestId("google-workspace-tool");
    fireEvent.click(screen.getByRole("button", { name: "open-config" }));
    const calendarScope = "https://www.googleapis.com/auth/calendar.events.readonly";
    await screen.findByLabelText("Client ID");
    expect(screen.queryByText(calendarScope)).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /Calendar/ }));
    expect(screen.getByText(calendarScope)).toBeTruthy();
  });

  it("clears the stored Picker API key only when asked", async () => {
    api.fetchGoogleConnection.mockResolvedValue(state(connection()));
    api.saveGoogleConnection.mockResolvedValue(connection());
    renderIntl(<Harness />, { locale: "en-US" });
    await screen.findByTestId("google-workspace-tool");
    fireEvent.click(screen.getByRole("button", { name: "open-config" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Clear the saved API key" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.saveGoogleConnection).toHaveBeenCalledTimes(1));
    expect(api.saveGoogleConnection.mock.calls[0][0].picker_api_key).toBe("");
  });
});

describe("Google Workspace copy", () => {
  const catalogs: Record<Locale, typeof enMessages> = { "zh-CN": zhMessages as never, "en-US": enMessages, "de-DE": deMessages as never, "fr-FR": frMessages as never };

  function keys(value: unknown, prefix = ""): string[] {
    if (typeof value !== "object" || value === null) return [prefix];
    return Object.entries(value).flatMap(([key, child]) => keys(child, prefix ? `${prefix}.${key}` : key));
  }

  it.each(["zh-CN", "de-DE", "fr-FR"] as Locale[])("%s has every Google Workspace string", (locale) => {
    const catalog = catalogs[locale] as unknown as { google_workspace: unknown; settings: { tools: { google_desc?: string } } };
    expect(keys(catalog.google_workspace).sort()).toEqual(keys(enMessages.google_workspace).sort());
    expect(catalog.settings.tools.google_desc).toBeTruthy();
  });

  it("never promises a per-write confirmation", () => {
    for (const catalog of Object.values(catalogs)) {
      const text = JSON.stringify((catalog as unknown as { google_workspace: unknown }).google_workspace);
      expect(text).not.toMatch(/确认后写入|after you confirm|Nach Ihrer Bestätigung|après votre confirmation/);
    }
  });
});
