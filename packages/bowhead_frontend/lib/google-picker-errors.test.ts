import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("./api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api-client")>()),
  apiJson: apiMocks.apiJson,
}));

import { ApiError } from "./api-client";
import { GooglePickerError, googlePickerErrorKey, invalidateGooglePicker, pickDriveFiles, prepareGooglePicker, resetGooglePickerForTests } from "./google-picker";
import { TEAM_CHANGED_EVENT } from "./team-storage";

type TokenConfig = {
  hint?: string;
  hosted_domain?: string;
  callback: (response: { access_token?: string; error?: string }) => void;
  error_callback?: (error: { type?: string }) => void;
};

let tokenConfig: TokenConfig | null = null;
let onRequest: (config: TokenConfig) => void = () => undefined;

beforeEach(() => {
  (window as { google?: unknown }).google = {
    accounts: {
      oauth2: {
        initTokenClient: (config: TokenConfig) => {
          tokenConfig = config;
          return { requestAccessToken: () => onRequest(config) };
        },
      },
    },
  };
  (window as { gapi?: unknown }).gapi = { load: (_name: string, options: { callback: () => void }) => options.callback() };
  vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
    queueMicrotask(() => (node as HTMLScriptElement).onload?.(new Event("load")));
    return node;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  apiMocks.apiJson.mockReset();
  resetGooglePickerForTests();
  tokenConfig = null;
  onRequest = () => undefined;
  delete (window as { google?: unknown }).google;
  delete (window as { gapi?: unknown }).gapi;
});

const CONFIG = { client_id: "c", api_key: "k", project_number: "1", login_hint: "member@acme.example", hosted_domain: "acme.example" };

describe("Google Picker token errors", () => {
  it("passes the connected account and domain as GIS hints", async () => {
    apiMocks.apiJson.mockResolvedValue(CONFIG);
    onRequest = (config) => config.callback({ error: "access_denied" });
    await expect(pickDriveFiles()).resolves.toEqual([]);
    expect(tokenConfig).toMatchObject({ hint: "member@acme.example", hosted_domain: "acme.example" });
  });

  it("treats a closed popup or declined consent as a silent cancel", async () => {
    apiMocks.apiJson.mockResolvedValue(CONFIG);
    onRequest = (config) => config.error_callback?.({ type: "popup_closed" });
    await expect(pickDriveFiles()).resolves.toEqual([]);
    onRequest = (config) => config.callback({ error: "access_denied" });
    await expect(pickDriveFiles()).resolves.toEqual([]);
  });

  it("reports a blocked popup separately from an unregistered origin", async () => {
    apiMocks.apiJson.mockResolvedValue(CONFIG);
    onRequest = (config) => config.error_callback?.({ type: "popup_failed_to_open" });
    const blocked = await pickDriveFiles().catch((error: unknown) => error);
    expect(googlePickerErrorKey(blocked)).toBe("popup_blocked");

    onRequest = (config) => config.error_callback?.({ type: "unknown" });
    const origin = await pickDriveFiles().catch((error: unknown) => error);
    expect(googlePickerErrorKey(origin)).toBe("origin");
  });

  it("caches 'not connected' for the page session instead of refetching on every mount", async () => {
    apiMocks.apiJson.mockRejectedValue(new ApiError(409, "x", { code: "google_not_connected" }));
    await expect(prepareGooglePicker()).rejects.toBeInstanceOf(GooglePickerError);
    await expect(prepareGooglePicker()).rejects.toMatchObject({ code: "google_not_connected" });
    expect(apiMocks.apiJson).toHaveBeenCalledTimes(1);
  });

  it("retries transient failures", async () => {
    apiMocks.apiJson.mockRejectedValueOnce(new ApiError(500, "x", { code: "internal" })).mockResolvedValueOnce(CONFIG);
    await expect(prepareGooglePicker()).rejects.toBeTruthy();
    await expect(prepareGooglePicker()).resolves.toMatchObject({ client_id: "c" });
  });
});

describe("Google Picker context isolation", () => {
  it("loads the new team's configuration after a team change", async () => {
    apiMocks.apiJson.mockResolvedValueOnce(CONFIG).mockResolvedValueOnce({ ...CONFIG, client_id: "new-team-client" });
    await expect(prepareGooglePicker()).resolves.toMatchObject({ client_id: "c" });
    window.dispatchEvent(new Event(TEAM_CHANGED_EVENT));
    await expect(prepareGooglePicker()).resolves.toMatchObject({ client_id: "new-team-client" });
    expect(apiMocks.apiJson).toHaveBeenCalledTimes(2);
  });

  it("does not carry a not-connected refusal into another team", async () => {
    apiMocks.apiJson.mockRejectedValueOnce(new ApiError(409, "x", { code: "google_not_connected" })).mockResolvedValueOnce(CONFIG);
    await expect(prepareGooglePicker()).rejects.toMatchObject({ code: "google_not_connected" });
    window.dispatchEvent(new Event(TEAM_CHANGED_EVENT));
    await expect(prepareGooglePicker()).resolves.toMatchObject(CONFIG);
  });

  it.each(["success", "refusal"])("fences a late %s from the previous team", async (outcome) => {
    let finishOld!: (config: typeof CONFIG) => void;
    let rejectOld!: (error: Error) => void;
    const oldRequest = new Promise<typeof CONFIG>((resolve, reject) => {
      finishOld = resolve;
      rejectOld = reject;
    });
    apiMocks.apiJson.mockReturnValueOnce(oldRequest).mockResolvedValueOnce({ ...CONFIG, client_id: "new-client" });
    const oldPreparation = prepareGooglePicker().catch((error: unknown) => error);
    window.dispatchEvent(new Event(TEAM_CHANGED_EVENT));
    await expect(prepareGooglePicker()).resolves.toMatchObject({ client_id: "new-client" });
    if (outcome === "success") finishOld(CONFIG);
    else rejectOld(new ApiError(409, "x", { code: "google_not_connected" }));
    expect(await oldPreparation).toBeInstanceOf(Error);
    await expect(prepareGooglePicker()).resolves.toMatchObject({ client_id: "new-client" });
    expect(apiMocks.apiJson).toHaveBeenCalledTimes(2);
  });

  it("discards a token returned after the connection was invalidated", async () => {
    apiMocks.apiJson.mockResolvedValue(CONFIG);
    await prepareGooglePicker();
    const selection = pickDriveFiles();
    invalidateGooglePicker();
    tokenConfig!.callback({ access_token: "old-context-token" });
    await expect(selection).resolves.toEqual([]);
  });

  it("closes an open Picker and discards its later selection when the team changes", async () => {
    apiMocks.apiJson.mockResolvedValue(CONFIG);
    const setVisible = vi.fn();
    let callback!: (data: { action: string; docs?: Array<{ id: string; name: string }> }) => void;
    class Builder {
      addView() { return this; }
      enableFeature() { return this; }
      setAppId() { return this; }
      setDeveloperKey() { return this; }
      setOAuthToken() { return this; }
      setOrigin() { return this; }
      setCallback(value: typeof callback) { callback = value; return this; }
      build() { return { setVisible }; }
    }
    Object.assign(window.google!, {
      picker: {
        PickerBuilder: Builder,
        DocsView: class { setIncludeFolders() {} },
        Feature: { MULTISELECT_ENABLED: "multi" },
        Action: { PICKED: "picked", CANCEL: "cancel" },
      },
    });
    onRequest = (config) => config.callback({ access_token: "current-token" });
    await prepareGooglePicker();
    const selection = pickDriveFiles();
    await vi.waitFor(() => expect(setVisible).toHaveBeenCalledWith(true));
    window.dispatchEvent(new Event(TEAM_CHANGED_EVENT));
    await expect(selection).resolves.toEqual([]);
    expect(setVisible).toHaveBeenLastCalledWith(false);
    callback({ action: "picked", docs: [{ id: "old-file", name: "Previous team's file" }] });
    await expect(selection).resolves.toEqual([]);
  });
});
