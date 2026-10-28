import { afterEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({ apiJson: vi.fn() }));
vi.mock("./api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api-client")>()),
  apiJson: apiMocks.apiJson,
}));

import { ApiError } from "./api-client";
import {
  GooglePickerError,
  fetchGooglePickerConfig,
  googlePickerErrorKey,
  normalizePicked,
  pickDriveFiles,
  pickedFilesAsText,
  prepareGooglePicker,
  resetGooglePickerForTests,
} from "./google-picker";

describe("google picker helpers", () => {
  afterEach(() => apiMocks.apiJson.mockReset());

  it("inserts one plain 'name link' line per picked file", () => {
    const files = normalizePicked([
      { id: "a1", name: " 季度报告 ", mimeType: "application/vnd.google-apps.document", url: "https://docs.google.com/document/d/a1/edit" },
      { id: "b2" },
      { id: "" },
    ]);
    expect(files.map((f) => f.id)).toEqual(["a1", "b2"]);
    expect(pickedFilesAsText(files)).toBe("季度报告 https://docs.google.com/document/d/a1/edit\nb2 https://drive.google.com/file/d/b2/view");
  });

  it("maps picker-config refusals to typed errors and copy keys", async () => {
    apiMocks.apiJson.mockRejectedValueOnce(new ApiError(409, "x", { code: "google_not_connected" }));
    await expect(fetchGooglePickerConfig()).rejects.toMatchObject({ code: "google_not_connected" });
    apiMocks.apiJson.mockRejectedValueOnce(new ApiError(409, "x", { code: "google_picker_unavailable" }));
    await expect(fetchGooglePickerConfig()).rejects.toMatchObject({ code: "google_picker_unavailable" });

    expect(googlePickerErrorKey(new GooglePickerError("google_not_connected"))).toBe("not_connected");
    expect(googlePickerErrorKey(new GooglePickerError("google_origin_not_registered"))).toBe("origin");
    expect(googlePickerErrorKey(new Error("boom"))).toBe("load_failed");
  });
});

describe("google picker preloading", () => {
  afterEach(() => {
    apiMocks.apiJson.mockReset();
    resetGooglePickerForTests();
    delete (window as { google?: unknown }).google;
    delete (window as { gapi?: unknown }).gapi;
  });

  it("opens the token popup synchronously inside the click once prepared", async () => {
    apiMocks.apiJson.mockResolvedValue({ client_id: "client-1", api_key: "key", project_number: "123" });
    const requestAccessToken = vi.fn();
    (window as { google?: unknown }).google = {
      accounts: { oauth2: { initTokenClient: () => ({ requestAccessToken }) } },
    };
    (window as { gapi?: unknown }).gapi = { load: (_name: string, options: { callback: () => void }) => options.callback() };
    // Scripts resolve as soon as they are appended.
    const append = vi.spyOn(document.head, "appendChild").mockImplementation((node) => {
      queueMicrotask(() => (node as HTMLScriptElement).onload?.(new Event("load")));
      return node;
    });

    await prepareGooglePicker();
    void pickDriveFiles();
    // No await between the click and the popup request.
    expect(requestAccessToken).toHaveBeenCalledTimes(1);
    append.mockRestore();
  });
});
