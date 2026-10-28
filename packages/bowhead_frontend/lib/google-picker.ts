import { ApiError, apiJson } from "./api-client";
import { TEAM_CHANGED_EVENT } from "./team-storage";

/**
 * Google Drive Picker for Picker-scoped (`drive.file`) access.
 *
 * Google's scripts are preloaded while the Google Workspace capability is on (see
 * `prepareGooglePicker`), so the token popup can open inside the click. The browser asks
 * Google Identity Services for a short-lived `drive.file` token that stays in memory;
 * the backend refresh token never reaches the browser. Picking a file grants this
 * OAuth client access to it, which the backend then uses with the member's own grant.
 */

export type GooglePickerConfig = {
  client_id: string;
  api_key: string;
  project_number: string;
  /** The member's connected Google account and the team's Workspace domain (GIS account hints). */
  login_hint?: string;
  hosted_domain?: string;
};

export type PickedDriveFile = {
  id: string;
  name: string;
  mimeType: string;
  url: string;
};

/** Typed failures the UI maps to copy. */
export type GooglePickerErrorCode =
  | "google_not_connected"
  | "google_picker_unavailable"
  | "google_origin_not_registered"
  | "google_popup_blocked"
  | "google_load_failed";

export class GooglePickerError extends Error {
  readonly code: GooglePickerErrorCode;

  constructor(code: GooglePickerErrorCode) {
    super(code);
    this.name = "GooglePickerError";
    this.code = code;
  }
}

const GIS_SRC = "https://accounts.google.com/gsi/client";
const GAPI_SRC = "https://apis.google.com/js/api.js";
const DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file";

type GoogleTokenResponse = { access_token?: string; error?: string };
type GoogleGlobal = {
  accounts?: {
    oauth2?: {
      initTokenClient(config: {
        client_id: string;
        scope: string;
        hint?: string;
        hosted_domain?: string;
        callback: (response: GoogleTokenResponse) => void;
        error_callback?: (error: { type?: string }) => void;
      }): { requestAccessToken(options?: { prompt?: string }) : void };
    };
  };
  picker?: {
    PickerBuilder: new () => PickerBuilder;
    DocsView: new () => { setIncludeFolders(value: boolean): unknown };
    Feature: { MULTISELECT_ENABLED: string };
    Action: { PICKED: string; CANCEL: string };
  };
};
type PickerBuilder = {
  addView(view: unknown): PickerBuilder;
  enableFeature(feature: string): PickerBuilder;
  setAppId(id: string): PickerBuilder;
  setDeveloperKey(key: string): PickerBuilder;
  setOAuthToken(token: string): PickerBuilder;
  setOrigin(origin: string): PickerBuilder;
  setCallback(callback: (data: PickerResponse) => void): PickerBuilder;
  build(): { setVisible(visible: boolean): void };
};
type PickerResponse = {
  action: string;
  docs?: Array<{ id: string; name?: string; mimeType?: string; url?: string }>;
};

declare global {
  interface Window {
    google?: GoogleGlobal;
    gapi?: { load(name: string, options: { callback: () => void; onerror?: () => void }): void };
  }
}

const loaded = new Map<string, Promise<void>>();

function loadScript(src: string): Promise<void> {
  const existing = loaded.get(src);
  if (existing) return existing;
  const promise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => {
      loaded.delete(src);
      script.remove();
      reject(new GooglePickerError("google_load_failed"));
    };
    document.head.appendChild(script);
  });
  loaded.set(src, promise);
  return promise;
}

export async function fetchGooglePickerConfig(): Promise<GooglePickerConfig> {
  try {
    return await apiJson<GooglePickerConfig>("/api/v1/google/picker-config");
  } catch (error) {
    if (error instanceof ApiError && error.code === "google_not_connected") throw new GooglePickerError("google_not_connected");
    if (error instanceof ApiError && (error.code === "google_picker_unavailable" || error.status === 404)) {
      throw new GooglePickerError("google_picker_unavailable");
    }
    throw error;
  }
}

/**
 * Ask GIS for a `drive.file` token; `null` means the member cancelled (closed the popup or
 * declined). A popup the browser blocked is reported separately so the UI can ask for one
 * more click instead of blaming the access address.
 */
async function requestDriveFileToken(config: GooglePickerConfig): Promise<string | null> {
  const oauth2 = window.google?.accounts?.oauth2;
  if (!oauth2) throw new GooglePickerError("google_load_failed");
  return new Promise<string | null>((resolve, reject) => {
    let client: { requestAccessToken(options?: { prompt?: string }): void };
    try {
      client = oauth2.initTokenClient({
        client_id: config.client_id,
        scope: DRIVE_FILE_SCOPE,
        ...(config.login_hint ? { hint: config.login_hint } : {}),
        ...(config.hosted_domain ? { hosted_domain: config.hosted_domain } : {}),
        callback: (response) => {
          if (response.access_token) resolve(response.access_token);
          else if (response.error === "access_denied") resolve(null);
          // Any other OAuth error here comes from Google rejecting this page's origin/client setup.
          else reject(new GooglePickerError("google_origin_not_registered"));
        },
        error_callback: (error) => {
          if (error.type === "popup_closed") resolve(null);
          else if (error.type === "popup_failed_to_open") reject(new GooglePickerError("google_popup_blocked"));
          else reject(new GooglePickerError("google_origin_not_registered"));
        },
      });
    } catch {
      // GIS refuses to initialise for an origin that is not registered on the OAuth client.
      reject(new GooglePickerError("google_origin_not_registered"));
      return;
    }
    client.requestAccessToken({ prompt: "" });
  });
}

let preparedConfig: GooglePickerConfig | null = null;
let preparing: Promise<GooglePickerConfig> | null = null;
// Both configuration and deterministic refusals belong to the current team/authorization.
let unavailable: GooglePickerError | null = null;
let contextGeneration = 0;
let observingContext = false;
const cancelPickers = new Set<() => void>();

class GooglePickerContextChangedError extends Error {}

/** Drop connection-specific state and fence responses started under the previous context. */
export function invalidateGooglePicker(): void {
  contextGeneration += 1;
  preparedConfig = null;
  preparing = null;
  unavailable = null;
  for (const cancel of cancelPickers) cancel();
}

function observeContext(): void {
  if (!observingContext) {
    window.addEventListener(TEAM_CHANGED_EVENT, invalidateGooglePicker);
    observingContext = true;
  }
}

/**
 * Fetch the Picker config and load Google's scripts ahead of the click.
 *
 * Google's token popup must open inside the user's click. When everything is already
 * loaded, `pickDriveFiles` reaches `requestAccessToken` without awaiting anything, so
 * browsers that only allow popups during the click (Safari, Firefox) do not block it.
 * "Not connected" and "Picker not configured" are cached for the page session; other
 * failures are not, so the next call retries.
 */
export function prepareGooglePicker(): Promise<GooglePickerConfig> {
  observeContext();
  if (preparedConfig) return Promise.resolve(preparedConfig);
  if (unavailable) return Promise.reject(unavailable);
  if (!preparing) {
    const generation = contextGeneration;
    const preparation = (async () => {
      let config: GooglePickerConfig;
      try {
        config = await fetchGooglePickerConfig();
      } catch (error) {
        if (generation !== contextGeneration) throw new GooglePickerContextChangedError();
        if (error instanceof GooglePickerError && (error.code === "google_not_connected" || error.code === "google_picker_unavailable")) {
          unavailable = error;
        }
        throw error;
      }
      if (generation !== contextGeneration) throw new GooglePickerContextChangedError();
      await Promise.all([loadScript(GIS_SRC), loadScript(GAPI_SRC)]);
      await new Promise<void>((resolve, reject) => {
        if (!window.gapi) {
          reject(new GooglePickerError("google_load_failed"));
          return;
        }
        window.gapi.load("picker", { callback: () => resolve(), onerror: () => reject(new GooglePickerError("google_load_failed")) });
      });
      if (generation !== contextGeneration) throw new GooglePickerContextChangedError();
      preparedConfig = config;
      return config;
    })().finally(() => {
      if (generation === contextGeneration) preparing = null;
    });
    preparing = preparation;
  }
  return preparing;
}

/** Test seam: forget the prepared config and loaded scripts. */
export function resetGooglePickerForTests(): void {
  invalidateGooglePicker();
  window.removeEventListener(TEAM_CHANGED_EVENT, invalidateGooglePicker);
  observingContext = false;
  loaded.clear();
}

/** Open the Drive Picker; resolves with the picked files (empty when cancelled). */
export async function pickDriveFiles(): Promise<PickedDriveFile[]> {
  observeContext();
  const generation = contextGeneration;
  // No await on the prepared path: the token popup opens inside the click.
  let config: GooglePickerConfig;
  let token: string | null;
  try {
    config = preparedConfig ?? (await prepareGooglePicker());
    if (generation !== contextGeneration) return [];
    token = await requestDriveFileToken(config);
  } catch (error) {
    if (generation !== contextGeneration || error instanceof GooglePickerContextChangedError) return [];
    throw error;
  }
  if (token === null || generation !== contextGeneration) return [];
  const picker = window.google?.picker;
  if (!picker) throw new GooglePickerError("google_load_failed");
  return new Promise<PickedDriveFile[]>((resolve) => {
    let selection: { setVisible(visible: boolean): void };
    const cancel = () => {
      selection.setVisible(false);
      cancelPickers.delete(cancel);
      resolve([]);
    };
    const finish = (files: PickedDriveFile[]) => {
      cancelPickers.delete(cancel);
      resolve(generation === contextGeneration ? files : []);
    };
    const view = new picker.DocsView();
    view.setIncludeFolders(false);
    selection = new picker.PickerBuilder()
      .addView(view)
      .enableFeature(picker.Feature.MULTISELECT_ENABLED)
      .setAppId(config.project_number)
      .setDeveloperKey(config.api_key)
      .setOAuthToken(token)
      .setOrigin(window.location.origin)
      .setCallback((data) => {
        if (data.action === picker.Action.PICKED) finish(normalizePicked(data.docs ?? []));
        else if (data.action === picker.Action.CANCEL) finish([]);
      })
      .build();
    cancelPickers.add(cancel);
    selection.setVisible(true);
  });
}

export function normalizePicked(docs: NonNullable<PickerResponse["docs"]>): PickedDriveFile[] {
  return docs
    .filter((doc) => typeof doc.id === "string" && doc.id.length > 0)
    .map((doc) => ({
      id: doc.id,
      name: doc.name?.trim() || doc.id,
      mimeType: doc.mimeType ?? "",
      url: doc.url || `https://drive.google.com/file/d/${doc.id}/view`,
    }));
}

/** Plain-text lines inserted into the composer: "name link", one per file (product decision). */
export function pickedFilesAsText(files: PickedDriveFile[]): string {
  return files.map((file) => `${file.name} ${file.url}`).join("\n");
}

/** Message key under `google_picker` for a Picker failure. */
export function googlePickerErrorKey(error: unknown): "not_connected" | "unavailable" | "origin" | "popup_blocked" | "load_failed" {
  if (error instanceof GooglePickerError) {
    if (error.code === "google_not_connected") return "not_connected";
    if (error.code === "google_picker_unavailable") return "unavailable";
    if (error.code === "google_origin_not_registered") return "origin";
    if (error.code === "google_popup_blocked") return "popup_blocked";
  }
  return "load_failed";
}
