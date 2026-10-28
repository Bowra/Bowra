import { appFetch as fetch } from "@/lib/base-path";
import {
  genericUserFacingErrorMessage,
  safeUserFacingErrorMessage,
} from "@/lib/user-facing-error";

export type InviteType = "workspace" | "team";
export type InvitePreviewStatus = "valid" | "expired" | "revoked" | "consumed" | "invalid";
export type InviteNextAction =
  | "activate_password"
  | "activate_federated"
  | "login_password"
  | "login_federated"
  | "accept"
  | "switch_account"
  | "identity_blocked"
  | "policy_blocked";

export type InviteViewerState =
  | "anonymous"
  | "logged_in_matching_email"
  | "logged_in_mismatched_email"
  | "logged_in_wrong_team";

export interface InvitePreviewValid {
  status: "valid";
  invite_type: InviteType;
  invited_email_mask: string;
  expires_at?: string | null;
  /** Inviter's display name (display name only, never the email); omitted when the inviter has none. */
  inviter_display_name?: string | null;
  team: {
    name: string;
  };
  workspace?: {
    name: string;
    role: string;
  };
  viewer_context?: {
    state: InviteViewerState;
    email_mask?: string;
  };
}

export interface InviteIntentContext extends Omit<InvitePreviewValid, "status"> {
  status: "ready";
  next_action?: InviteNextAction;
}

export interface AuthCapabilities {
  deployment_mode: "local" | "saas";
  account_creation_policy: "open" | "invite_only" | "admin_only";
  email_verification_policy: "not_required" | "smtp_required" | "trusted_idp_required";
  auth_methods: string[];
  public_registration: boolean;
  bootstrap_required: boolean;
  invite_activation: boolean;
  /** Whether this deployment serves team-level API credentials. Absent on older gateways. */
  team_openapi?: boolean;
}

export type InvitePreviewResponse =
  | InvitePreviewValid
  | {
      status: Exclude<InvitePreviewStatus, "valid">;
      invite_type?: InviteType;
    };

export interface InviteRegisterResult {
  invite_type: InviteType;
  next_route: string;
  ready_route?: string;
  status?: "accepted" | "already_member" | "verification_required";
  next_step?: "profile_setup" | "workspace_ready" | "team_ready_no_workspace" | "verify_email";
  requires_verification?: boolean;
  team?: {
    id?: string | null;
    name?: string | null;
  };
  workspace?: {
    id?: string | null;
    name?: string | null;
    role?: string | null;
  };
}

export interface InviteAcceptResponse {
  ok: boolean;
  invite_result: InviteRegisterResult;
}

export const INVITE_PREVIEW_STORAGE_KEY = "bowhead.invite.preview";
export const INVITE_RESULT_STORAGE_KEY = "bowhead.invite.result";
export const INVITE_VERIFY_EMAIL_STORAGE_KEY = "bowhead.invite.verifyEmail";

export class InviteApiError extends Error {
  status: number;
  code?: string;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.name = "InviteApiError";
    this.status = status;
    this.code = code;
  }
}

export async function previewInvite(token: string): Promise<InvitePreviewResponse> {
  const response = await fetch("/api/v1/auth/invites/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
    credentials: "include",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw inviteApiError(response.status, data);
  return data as InvitePreviewResponse;
}

export async function getAuthCapabilities(): Promise<AuthCapabilities> {
  return requestJson<AuthCapabilities>("/api/v1/auth/capabilities", {
    credentials: "include",
  });
}

export async function exchangeInviteIntent(token: string): Promise<InviteIntentContext> {
  return requestJson<InviteIntentContext>("/api/v1/auth/invites/intent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
    credentials: "include",
  });
}

export async function continueInviteIntent(): Promise<InviteIntentContext> {
  return requestJson<InviteIntentContext>("/api/v1/auth/invites/intent/continue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    credentials: "include",
  });
}

export async function activateInvite(body: {
  display_name?: string;
  password: string;
}): Promise<InviteAcceptResponse & { requires_verification?: boolean }> {
  return requestJson("/api/v1/auth/invites/activate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
}

export async function acceptInvite(token?: string): Promise<InviteAcceptResponse> {
  const response = await fetch("/api/v1/auth/invites/accept", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(token ? { token } : {}),
    credentials: "include",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw inviteApiError(response.status, data);
  return data as InviteAcceptResponse;
}

export function readInvitePreview(): InvitePreviewValid | InviteIntentContext | null {
  return readInviteJson<InvitePreviewValid | InviteIntentContext>(INVITE_PREVIEW_STORAGE_KEY);
}

export function storeInvitePreview(preview: InvitePreviewValid | InviteIntentContext): void {
  writeInviteJson(INVITE_PREVIEW_STORAGE_KEY, preview);
}

export function readInviteResult(): InviteRegisterResult | null {
  return readInviteJson<InviteRegisterResult>(INVITE_RESULT_STORAGE_KEY);
}

export function storeInviteResult(result: InviteRegisterResult): void {
  writeInviteJson(INVITE_RESULT_STORAGE_KEY, result);
}

export function readInviteVerifyEmail(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(INVITE_VERIFY_EMAIL_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function readInviteJson<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function writeInviteJson(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}

async function requestJson<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw inviteApiError(response.status, data);
  return data as T;
}

function inviteApiError(status: number, data: unknown): InviteApiError {
  const detail = (
    data && typeof data === "object"
      ? (data as { detail?: unknown }).detail
      : undefined
  );
  if (typeof detail === "string" && detail.trim()) {
    return new InviteApiError(status, detail.trim());
  }
  if (!detail || typeof detail !== "object") {
    return new InviteApiError(status, genericUserFacingErrorMessage());
  }

  const direct = detail as {
    code?: unknown;
    message?: unknown;
    error?: unknown;
  };
  const nested = (
    direct.error && typeof direct.error === "object"
      ? direct.error as { code?: unknown; message?: unknown }
      : undefined
  );
  const codeValue = nested?.code ?? direct.code;
  const messageValue = nested?.message ?? direct.message;
  const code = typeof codeValue === "string" && codeValue.trim()
    ? codeValue.trim()
    : undefined;
  return new InviteApiError(
    status,
    safeUserFacingErrorMessage(messageValue, code),
    code,
  );
}
