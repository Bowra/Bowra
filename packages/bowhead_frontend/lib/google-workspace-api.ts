import { apiFetch, readApiError } from "./api-client";
import { invalidateGooglePicker } from "./google-picker";

/** Capability bundle ids; `base` is always on. */
export type GoogleBundleId = "base" | "read_by_link" | "edit_docs" | "edit_sheets" | "calendar";

export const GOOGLE_BUNDLE_IDS: readonly GoogleBundleId[] = ["base", "read_by_link", "edit_docs", "edit_sheets", "calendar"];

export type GoogleBundle = { id: GoogleBundleId; enabled: boolean; required: boolean; scopes: string[] };

export type GoogleConnection = {
  id: string;
  provider: "google_workspace";
  name: string;
  status: "enabled" | "disabled";
  client_id: string;
  client_secret_configured: boolean;
  picker_api_key_configured: boolean;
  picker_project_number: string;
  domain: string;
  bundles: GoogleBundle[];
  scopes: string[];
  redirect_uri: string | null;
  javascript_origin: string | null;
  authorized_members: number;
  created_at: string;
  updated_at: string;
};

export type GoogleConnectionState = {
  connection: GoogleConnection | null;
  /** Present only before a connection exists. */
  redirect_uri?: string | null;
  javascript_origin?: string | null;
  bundles?: GoogleBundle[];
};

export type GoogleConnectionInput = {
  name: string;
  client_id: string;
  /** Omit to keep the stored secret. */
  client_secret?: string;
  domain: string;
  bundles: Partial<Record<GoogleBundleId, boolean>>;
  /** Omit to keep the stored key; empty string clears it. */
  picker_api_key?: string;
  picker_project_number: string;
};

export type GoogleAuthorization = {
  connection_id: string;
  name: string;
  connection_status: "enabled" | "disabled";
  status: "not_connected" | "active" | "needs_reauth";
  email: string | null;
  bundles: Array<{ id: GoogleBundleId; enabled: boolean; granted: boolean }>;
  needs_update: boolean;
  connected_at: string | null;
  last_used_at: string | null;
};

export type GoogleApiError = Error & { status?: number; code?: string };

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response;
  const error = await readApiError(response);
  throw Object.assign(new Error(error.message), { status: error.status, code: error.code }) as GoogleApiError;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

/** `null` when Google Workspace is unavailable on this deployment (feature switch off). */
export async function fetchGoogleConnection(): Promise<GoogleConnectionState | null> {
  const response = await apiFetch("/api/v1/google/connection");
  if (response.status === 404) return null;
  return (await (await checked(response)).json()) as GoogleConnectionState;
}

export async function saveGoogleConnection(input: GoogleConnectionInput): Promise<GoogleConnection> {
  const response = await apiFetch("/api/v1/google/connection", { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(input) });
  const connection = ((await (await checked(response)).json()) as { connection: GoogleConnection }).connection;
  invalidateGooglePicker();
  return connection;
}

export async function setGoogleConnectionEnabled(enabled: boolean): Promise<GoogleConnection> {
  const response = await apiFetch("/api/v1/google/connection/status", { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify({ enabled }) });
  const connection = ((await (await checked(response)).json()) as { connection: GoogleConnection }).connection;
  invalidateGooglePicker();
  return connection;
}

export async function deleteGoogleConnection(): Promise<void> {
  await checked(await apiFetch("/api/v1/google/connection", { method: "DELETE" }));
  invalidateGooglePicker();
}

/** `null` when the feature switch is off. */
export async function fetchMyGoogleAuthorization(): Promise<{ available: boolean; authorization: GoogleAuthorization | null } | null> {
  const response = await apiFetch("/api/v1/google/me");
  if (response.status === 404) return null;
  return (await (await checked(response)).json()) as { available: boolean; authorization: GoogleAuthorization | null };
}

export async function beginGoogleAuthorization(): Promise<string> {
  const response = await apiFetch("/api/v1/google/connect", { method: "POST" });
  return ((await (await checked(response)).json()) as { authorize_url: string }).authorize_url;
}

export async function disconnectGoogle(): Promise<void> {
  await checked(await apiFetch("/api/v1/google/me", { method: "DELETE" }));
  invalidateGooglePicker();
}

/** Callback result codes the gateway appends as `?google=…&google_reason=…`. */
export const GOOGLE_CALLBACK_REASONS = [
  "connection_unavailable",
  "session_expired",
  "authorization_denied",
  "authorization_failed",
  "domain_mismatch",
  "email_mismatch",
  "google_unreachable",
  "token_exchange_failed",
  "base_scope_denied",
  "refresh_token_missing",
] as const;

export type GoogleCallbackNotice = { result: "connected" } | { result: "failed"; reason: (typeof GOOGLE_CALLBACK_REASONS)[number] | "unknown" };

export function googleCallbackNotice(search: string): GoogleCallbackNotice | null {
  const params = new URLSearchParams(search);
  const result = params.get("google");
  if (result === "connected") return { result: "connected" };
  if (result !== "failed") return null;
  const reason = params.get("google_reason") ?? "";
  return { result: "failed", reason: (GOOGLE_CALLBACK_REASONS as readonly string[]).includes(reason) ? (reason as (typeof GOOGLE_CALLBACK_REASONS)[number]) : "unknown" };
}

/** Remove the callback query from the current URL without navigating. */
export function clearGoogleCallbackQuery(): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.delete("google");
  url.searchParams.delete("google_reason");
  window.history.replaceState({}, "", url.toString());
}
