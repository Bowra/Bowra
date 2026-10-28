import { isNetworkFailure } from "./transport-error";
import { appFetch, appPath } from "@/lib/base-path";

/**
 * P4.5.12 — Bowhead Auth API helpers (frontend).
 *
 * Cookie-based JWT (httpOnly), automatically sent by browser. Bearer token fallback supported.
 *
 * Endpoints (in /api/v1/auth/*):
 *   POST /register | /login | /logout | GET /me
 *   POST /forgot-password | /reset-password
 *   GET  /oauth/{provider}/login (return redirect URL)
 *   POST /datarc-sso (Bowhead → datarc passwordless login)
 */

import { staticTranslator } from "./locale-messages";
import {
  activateCachedBrandForTeam,
  getBrandSnapshot,
} from "./brand-runtime";
import { beginBrandTransition } from "./effective-theme-request-guard";
import { clearSpaceStorage } from "./space-storage";
import { TEAM_CHANGED_EVENT } from "./team-storage";
import type { InviteRegisterResult } from "./invite-api";
import {
  parsePublishedErrorDiagnostics,
  projectTransportFailure,
} from "./error-presentation";
import type {
  ErrorDiagnostics,
  TransportFailureContext,
} from "./error-presentation";
import {
  genericUserFacingErrorMessage,
  safeUserFacingErrorMessage,
} from "./user-facing-error";
import { TERMINAL_CHAT_CAPABILITY_HEADERS } from "./terminal-chat-capability";

export interface MeUser {
  id: string;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  team_id: string;
  /** Display name of the current team; empty without an active membership (older gateways omit it). */
  team_name?: string;
  /** Deterministic revision of the current team capability projection. */
  capability_revision?: string;
  /** Server observation time for capability TTL/staleness UI. */
  fetched_at?: string;
  /** Non-credential fence, stable for one login/refresh chain. */
  auth_session_generation?: string;
  /** Server-advisory next-write protocol; every POST is revalidated. */
  chat_execution?: ChatExecutionBootstrap;
  tier: "pro" | "ultra";
  role: string;
  team_role?:
    "owner" | "team_admin" | "audit_admin" | "member";
  membership_required?: boolean;
  status: string;
  email_verified: boolean;
  has_password?: boolean;
  oauth_providers?: string[];
  auth_mode?: "dev" | "prod";
  capabilities?: {
    account_deletion?: boolean;
    personal_mcp?: boolean;
    /** Google Workspace is on and the team connection is enabled; members authorize under My connections. */
    google_workspace?: boolean;
    /** Google Workspace is switched on for the deployment (gates its help article). */
    google_workspace_feature?: boolean;
    shopify_plugin?: boolean;
    /** FEATURE_AGENT_EVAL_ALIGNMENT: the Coach alignment loop, point and suite review, held-out controls. */
    agent_eval_alignment?: boolean;
    /** Both Shopify integration and requester-confirmed content writes are enabled. */
    shopify_content_confirmations?: boolean;
    credit?: CreditCapabilities;
    /** Future Composer controls and the legacy submission-reader capability. */
    composer?: {
      submission_protocol?: boolean;
      reference_input?: boolean;
      reference_plan?: boolean;
      agent_start_use?: boolean;
      model_panel_v2: boolean;
      thinking_effort: boolean;
    };
    /** Final server-resolved planning capability consumed directly by the frontend. */
    planning?: boolean;
    /** Team license grants custom branding and appearance management. */
    custom_branding?: boolean;
    /** Default-off rollout for team and personal chart-style settings. */
    /** workflow-solidify B/C gate — true iff BOWHEAD_CCA_WORKFLOW_CREATOR is on (server-side). */
    workflow_creator?: boolean;
    /** “固化此流程” entry gate: workflow_creator && BOWHEAD_WORKFLOW_SOLIDIFY_ENTRY (default off).
     *  Separate from workflow_creator, which also gates team skill governance and /settings/workflows; cannot disable together. */
    workflow_solidify?: boolean;
    /** Whether team subagent authoring is available to this caller. */
    subagent_creator?: boolean;
    /** Member-safe Subagent catalog rollout. The server keeps this false unless its API master switch is also on. */
    subagent_management_v2?: boolean;
    /** Existing settings content in the shared Canvas; domain permissions still apply. */
    configuration_agent?: boolean;
    settings_pages_canvas?: boolean;
    /** Retained multi-object creation editors; domain APIs remain authoritative. */
    configuration_canvas?: boolean;
    /** Team-scoped conversational Subagent authoring rollout resolved by the server. */
    subagent_authoring?: {
      authoring: boolean;
      draft_chat_edit: boolean;
      unified_draft: boolean;
    };
    /** content-permissions wave 2 — true iff BOWHEAD_ACCESS_REQUESTS is on. Gates /settings/access-requests. */
    access_requests?: boolean;
    feedback_loop?: boolean;
    /** License-owner onboarding readiness surface, gated server-side by feature flag and Team role. */
    team_onboarding?: boolean;
    /** Team-level Space governance and invitation assignment UI. */
    space_admin_v2?: boolean;
    kb_health_observation?: boolean;
    kb_use_guard?: boolean;
    /** Deployment admits German AI answers (FEATURE_GERMAN_AI_OUTPUT); gates the Deutsch answer-language option. */
    german_ai_output?: boolean;
    [key: string]: unknown;
  };
  credit_capabilities?: CreditCapabilities;
  credit_balance: number;
  credit_quota: number;
  /** Start of the caller's current credit period (ISO); null or absent when unknown. */
  credit_period_start?: string | null;
  /** When the monthly reset next refills the caller's balance (ISO, UTC month start); null without an active Team membership, absent on older gateways. */
  credit_period_resets_at?: string | null;
  locale: string;
  timezone: string;
  // P4.5.23 onboarding signals.
  is_team_admin: boolean;
  workspace_count: number;
  needs_onboarding: boolean;
}

export interface ChatExecutionBootstrap {
  write_protocol:
    | "terminal_v1"
    | "legacy_v0"
    | "refresh_required"
    | "unavailable";
  config_generation: string;
  readiness_generation: string;
  /** Present only after serving and rollback-candidate Gateways accept Terminal typed refs. */
  attachment_refs_wire_version?: 1 | null;
  /** Exact persisted v1 writer target for an existing Session preflight. */
  active_turn_id?: string | null;
  active_execution_generation?: number | null;
}

export interface CreditCapabilities {
  credits_read: boolean;
  credit_grant: boolean;
  credit_user_quota_write: boolean;
  credit_user_tier_write: boolean;
  credit_period_reset: boolean;
  credit_team_quota_write: boolean;
  credit_attention_dismiss: boolean;
}

export interface TeamDTO {
  id: string;
  name: string;
  role: "owner" | "team_admin" | "audit_admin" | "member";
}

export function fetchTeams(): Promise<{
  items: TeamDTO[];
  active_team_id: string;
}> {
  return _get("/api/v1/auth/me/teams");
}

export async function setLastTeam(
  teamId: string,
): Promise<{ ok: string; team_id: string; redirect_url?: string }> {
  invalidateMeCache();
  const previousTeamId =
    typeof window !== "undefined" ? (getBrandSnapshot().team?.id ?? "") : "";
  if (typeof window !== "undefined") {
    beginBrandTransition(teamId);
    activateCachedBrandForTeam(teamId);
  }
  let result: { ok: string; team_id: string; redirect_url?: string };
  try {
    result = await _put<{
      ok: string;
      team_id: string;
      redirect_url?: string;
    }>("/api/v1/auth/me/last-team", { team_id: teamId });
  } catch (error) {
    if (typeof window !== "undefined") {
      beginBrandTransition(previousTeamId);
      activateCachedBrandForTeam(previousTeamId);
    }
    throw error;
  }
  invalidateMeCache();
  if (typeof window !== "undefined") {
    if (result.team_id !== teamId) {
      beginBrandTransition(result.team_id);
      activateCachedBrandForTeam(result.team_id);
    }
    window.dispatchEvent(
      new CustomEvent(TEAM_CHANGED_EVENT, {
        detail: { teamId: result.team_id },
      }),
    );
  }
  if (result.redirect_url && typeof window !== "undefined") {
    window.location.assign(result.redirect_url);
  }
  return result;
}

export interface LoginResponse {
  user: { id: string; email: string; tier: string };
  token: string;
}

export interface BootstrapStatus {
  required: boolean;
  deployment_mode: "local" | "saas";
  edition?: "community" | "business";
  setup_token_required?: boolean;
  license_team_ids?: Array<{ id: string; name: string }>;
}

export interface LoginCapabilities {
  oauth_providers: Array<"feishu">;
  enterprise_oidc: boolean;
}

export function localActivationRequired(
  status: BootstrapStatus | null,
): boolean {
  return status?.required === true && (status.edition === "community" || status.deployment_mode === "local");
}

export function getBootstrapStatus(): Promise<BootstrapStatus> {
  return _get<BootstrapStatus>("/api/v1/bootstrap/status");
}

export function getLoginCapabilities(): Promise<LoginCapabilities> {
  return _get<LoginCapabilities>("/api/v1/auth/login-capabilities");
}


/**
 * §C (real-auth): register no longer always auto-logs-in. When email-verify is REQUIRED
 * (116 SaaS) an unverified user gets `requires_verification: true` and NO token — the FE
 * shows "check your email" instead of routing into the app. Flag OFF keeps the LoginResponse.
 */
export interface RegisterResponse {
  user: {
    id: string;
    email: string;
    tier: string;
    team_id?: string;
    email_verified?: boolean;
  };
  token?: string;
  requires_verification?: boolean;
  /** Whether registration queued the verification email; present only when verification is required. */
  verification_mail?: VerificationMailOutcome;
  /** Short-lived register receipt; lets the public resend report the real outcome to this registrant. */
  verification_receipt?: string;
  invite_result?: InviteRegisterResult;
}

/** `queued` means the mail is in the outbox; later SMTP delivery is not observed. */
export interface VerificationMailOutcome {
  status: "queued" | "not_sent";
  reason?: "public_entry_required" | "email_not_configured" | "delivery_unavailable";
  /** Request id an administrator can find in the gateway logs. */
  reference?: string;
}

type RawMeUser = Omit<MeUser, "tier" | "credit_balance" | "credit_quota"> & {
  tier?: unknown;
  credit_balance?: unknown;
  credit_quota?: unknown;
};

class AuthError extends Error {
  status: number;
  /** Structured backend code from `detail.code` (e.g. "email_not_verified"), when present. */
  reasonCode?: string;
  /** Server-published, non-secret diagnostic text for an expandable details panel. */
  technicalMessage?: string;
  /** Correlation identifier returned by the gateway or edge proxy, when present. */
  requestId?: string;
  /** Versioned, validated diagnostics published by the shared frontend boundary. */
  diagnostics?: ErrorDiagnostics;
  constructor(
    status: number,
    message: string,
    reasonCode?: string,
    technicalMessage?: string,
    requestId?: string,
    diagnostics?: ErrorDiagnostics,
  ) {
    super(message);
    this.status = status;
    this.reasonCode = reasonCode;
    this.technicalMessage = technicalMessage;
    this.requestId = requestId;
    this.diagnostics = diagnostics;
  }
}

class AuthTransportError extends Error {
  reasonCode: "request_timed_out" | "service_unreachable";
  diagnostics: ErrorDiagnostics;

  constructor(
    reasonCode: "request_timed_out" | "service_unreachable",
    message: string,
    diagnostics: ErrorDiagnostics,
  ) {
    super(message);
    this.name = "AuthTransportError";
    this.reasonCode = reasonCode;
    this.diagnostics = diagnostics;
  }
}

interface ParsedAuthBody {
  data: unknown;
  rawText: string;
}

/**
 * Build an AuthError from a non-OK response body. `detail` may be a plain string (legacy)
 * OR a structured object `{code, message, email, ...}` (e.g. 403 email_not_verified) — parse
 * the code so callers can branch on it (mirrors api.ts's nested-detail handling).
 */
function _authError(
  status: number,
  _path: string,
  data: unknown,
  rawText = "",
  requestId?: string,
): AuthError {
  const detail = (data as { detail?: unknown })?.detail;
  if (detail && typeof detail === "object") {
    const d = detail as {
      code?: string;
      message?: string;
      technical_detail?: string;
    };
    const diagnostics = parsePublishedErrorDiagnostics(data, {
      status,
      code: d.code,
      requestId,
      observedAt: new Date().toISOString(),
    });
    return new AuthError(
      status,
      safeUserFacingErrorMessage(d.message, d.code),
      d.code,
      _safeTechnicalMessage(d.technical_detail),
      requestId,
      diagnostics,
    );
  }
  const legacyDetail = typeof detail === "string" ? detail.trim() : "";
  const technicalMessage = _safeLegacyAuthTechnicalMessage(
    legacyDetail || rawText,
  );
  const reasonCode = _legacyAuthReasonCode(status, technicalMessage);
  return new AuthError(
    status,
    genericUserFacingErrorMessage(),
    reasonCode,
    technicalMessage,
    requestId,
  );
}

async function _readAuthBody(response: Response): Promise<ParsedAuthBody> {
  const rawText = await response.text().catch(() => "");
  if (!rawText) return { data: {}, rawText: "" };
  try {
    return { data: JSON.parse(rawText) as unknown, rawText };
  } catch {
    return { data: {}, rawText };
  }
}

function _responseRequestId(response: Response): string | undefined {
  const candidate =
    response.headers.get("x-request-id") ??
    response.headers.get("x-correlation-id");
  return candidate &&
    /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(candidate)
    ? candidate
    : undefined;
}

function _safeTechnicalMessage(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  if (!candidate || candidate.length > 500) return undefined;
  if (/^\s*(?:<!doctype\s+html|<html)\b/i.test(candidate)) return undefined;
  return candidate;
}

function _safeLegacyAuthTechnicalMessage(value: string): string | undefined {
  const candidate = _safeTechnicalMessage(value);
  if (!candidate) return undefined;
  if (
    candidate === "browser public URL is not registered"
    || candidate === "invalid credentials"
    || /^user (?:inactive|disabled|suspended|blocked)$/.test(candidate)
  ) {
    return candidate;
  }
  return undefined;
}

function _legacyAuthReasonCode(
  status: number,
  technicalMessage?: string,
): string | undefined {
  if (technicalMessage === "invalid credentials") {
    return "invalid_credentials";
  }
  if (technicalMessage?.startsWith("user ")) {
    return "account_unavailable";
  }
  if (
    status === 421
    && technicalMessage === "browser public URL is not registered"
  ) {
    return "public_entry_not_registered";
  }
  if (status === 429) return "rate_limited";
  return undefined;
}

// followup-context-deck #11: timeout auth requests (/me /login /refresh /settings should return in seconds),
// preventing /me stalled behind proxy from spinning settings (subagent/skills/workflows) forever.
// Auth primitives _get/_post only; business apiFetch deliberately excluded because slow distill/export need longer timeouts.
const _AUTH_FETCH_TIMEOUT_MS = 20_000;

async function _fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  if (typeof AbortController === "undefined") {
    try {
      return await appFetch(input, init);
    } catch (error) {
      if (!isNetworkFailure(error)) throw error;
      throw _authTransportError({
        phase: "receive",
        timeoutTriggered: false,
        userAborted: false,
        onlineHint:
          typeof navigator === "undefined" ? undefined : navigator.onLine,
      });
    }
  }
  const ctrl = new AbortController();
  let timeoutTriggered = false;
  let userAborted = init.signal?.aborted === true;
  const abortFromCaller = () => {
    userAborted = true;
    ctrl.abort();
  };
  init.signal?.addEventListener("abort", abortFromCaller, { once: true });
  if (userAborted) ctrl.abort();
  const timer = setTimeout(() => {
    timeoutTriggered = true;
    ctrl.abort();
  }, _AUTH_FETCH_TIMEOUT_MS);
  try {
    const response = await appFetch(input, { ...init, signal: ctrl.signal });
    // Keep the timeout alive until the body has been consumed. A proxy can
    // deliver headers and then leave response.text() pending forever.
    const body = response.status === 204 || response.status === 205
      ? null
      : await response.arrayBuffer();
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } catch (e) {
    if (userAborted && !timeoutTriggered) throw e;
    if (!timeoutTriggered && !isNetworkFailure(e)) throw e;
    throw _authTransportError({
      phase: "receive",
      timeoutTriggered,
      userAborted: false,
      onlineHint:
        typeof navigator === "undefined" ? undefined : navigator.onLine,
    });
  } finally {
    clearTimeout(timer);
    init.signal?.removeEventListener("abort", abortFromCaller);
  }
}

function _authTransportError(
  context: TransportFailureContext,
): AuthTransportError {
  // Same transport-failure wording as UI (messages errors.transport), selected by UI locale.
  const t = staticTranslator("errors.transport");
  const presentation = projectTransportFailure(context, {
    requestTimedOut: t("requestTimedOut"),
    serviceUnreachable: t("serviceUnreachable"),
    retry: t("retry"),
  });
  // Auth requests do not expose a user-cancel action today. This guard keeps
  // the helper total if a future caller routes an explicit abort here.
  if (!presentation?.diagnostics) {
    throw new DOMException("The operation was aborted.", "AbortError");
  }
  return new AuthTransportError(
    context.timeoutTriggered
      ? "request_timed_out"
      : "service_unreachable",
    presentation.message,
    presentation.diagnostics,
  );
}

async function _post<T>(path: string, body: unknown): Promise<T> {
  const r = await _fetchWithTimeout(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  const { data, rawText } = await _readAuthBody(r);
  if (!r.ok) {
    throw _authError(r.status, path, data, rawText, _responseRequestId(r));
  }
  return data as T;
}

async function _put<T>(path: string, body: unknown): Promise<T> {
  const r = await _fetchWithTimeout(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  const { data, rawText } = await _readAuthBody(r);
  if (!r.ok) {
    throw _authError(r.status, path, data, rawText, _responseRequestId(r));
  }
  return data as T;
}

async function _get<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await _fetchWithTimeout(path, {
    credentials: "include",
    ...(init ?? {}),
  });
  const { data, rawText } = await _readAuthBody(r);
  if (!r.ok) {
    throw _authError(r.status, path, data, rawText, _responseRequestId(r));
  }
  return data as T;
}

export async function registerUser(body: {
  email: string;
  password: string;
  display_name?: string;
  invite_token?: string;
}): Promise<RegisterResponse> {
  invalidateMeCache();
  const result = await _post<RegisterResponse>("/api/v1/auth/register", body);
  invalidateMeCache();
  return result;
}

/**
 * §C (real-auth): resend the verify email WITHOUT a session. Used by the login/register
 * "check your email" notice — the register/login flows refuse a session to an unverified
 * user, so the by-email public endpoint is the only reachable resend. No-leak: always 200,
 * unless `receipt` is the registrant's valid register receipt, in which case the response carries
 * the real `verification_mail` outcome (and a 429 during the cooldown).
 */
export async function resendVerificationEmail(
  email: string,
  receipt?: string,
): Promise<{ ok: boolean; verification_mail?: VerificationMailOutcome }> {
  return _post("/api/v1/auth/resend-verify-public", receipt ? { email, receipt } : { email });
}

/** Resend for the signed-in account, with actionable errors and rate limits. */
export async function resendCurrentUserVerificationEmail(): Promise<{ ok: boolean; status?: "already_verified" }> {
  return _post("/api/v1/auth/resend-verify", {});
}

export async function loginUser(
  email: string,
  password: string,
): Promise<LoginResponse> {
  invalidateMeCache();
  const result = await _post<LoginResponse>("/api/v1/auth/login", {
    email,
    password,
  });
  invalidateMeCache();
  return result;
}

export async function logoutUser(): Promise<void> {
  invalidateMeCache();
  await _post("/api/v1/auth/logout", {});
  invalidateMeCache();
}

export async function logoutEverywhere(): Promise<{
  ok: boolean;
  revoked_count: number;
}> {
  invalidateMeCache();
  const result = await _post<{ ok: boolean; revoked_count: number }>(
    "/api/v1/auth/logout-everywhere",
    {},
  );
  invalidateMeCache();
  return result;
}

/**
 * P4.5.27: obtain new access via refresh cookie, automatically sent by browser.
 * Failure throws AuthError(401); caller may redirect /login.
 *
 * followup-context-deck review-r2 (CRITICAL): single-flight. Backend /refresh is one-time
 * rotation + replay defense; second concurrent same-cookie request triggers revoke_all_refresh_for_user
 * (all-device logout, auth.py "refresh race / replay detected"). Concurrent mount-time fetchMe calls
 * (settings-shell/page/members/user-menu…) produce N 401s on expiration → N concurrent
 * /refresh calls, deterministically triggering defense. Share one module-wide in-flight promise: first sends request,
 * others await same result (successful callers each retry /me with new cookie).
 * Tabs share the cookie too, so refreshes are also serialized across tabs, and a
 * `refresh_superseded` 401 (predecessor rotated by another tab moments ago) is retried.
 */
let _refreshInFlight: Promise<{
  ok: boolean;
  token: string;
  user: unknown;
}> | null = null;
let _invalidSessionRedirect: Promise<void> | null = null;
const _ME_CACHE_TTL_MS = 5_000;
let _meCache: { value: MeUser; cachedAtMs: number } | null = null;
let _meInFlight: Promise<MeUser> | null = null;
let _meCacheGeneration = 0;

export function invalidateMeCache(): void {
  _meCacheGeneration += 1;
  _meCache = null;
  _meInFlight = null;
}

/**
 * Remove the browser-side traces of the current account, ask the backend to
 * expire its httpOnly cookies, then replace the current page with login.
 *
 * A 401 can arrive concurrently from the page gate, a polling request and a
 * user action. Keep this single-flight: otherwise each request races to
 * refresh/revoke cookies and repeatedly navigates the browser.
 */
export function clearInvalidSessionAndRedirect(
  returnPath?: string,
): Promise<void> {
  invalidateMeCache();
  if (typeof window === "undefined") return Promise.resolve();
  if (_invalidSessionRedirect) return _invalidSessionRedirect;

  const next = encodeURIComponent(
    returnPath ?? `${window.location.pathname}${window.location.search}`,
  );
  _invalidSessionRedirect = (async () => {
    const bootstrapStatus = getBootstrapStatus().catch(() => null);
    try {
      // /logout is deliberately authentication-free and always emits the
      // cookie deletion headers, including when the access token is invalid.
      await appFetch("/api/v1/auth/logout", {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // The 401 is already authoritative. Continue to login even if a proxy
      // prevents the best-effort cookie cleanup request.
    }
    try {
      window.localStorage.removeItem("bowhead.user_profile");
      clearSpaceStorage();
    } catch {
      // Storage can be disabled; navigation must not depend on it.
    }
    const destination = localActivationRequired(await bootstrapStatus)
      ? "/activate/"
      : `/login/?next=${next}`;
    window.location.replace(appPath(destination));
  })();
  return _invalidSessionRedirect;
}

type RefreshResult = { ok: boolean; token: string; user: unknown };

/** Gateway code for a refresh token that another tab rotated moments ago. */
const _REFRESH_SUPERSEDED_CODE = "refresh_superseded";
const _REFRESH_CROSS_TAB_LOCK = "bowhead-auth-refresh";
const _REFRESH_SUPERSEDED_RETRY_DELAYS_MS = [150, 600] as const;

function _isRefreshSuperseded(error: unknown): boolean {
  return (
    error instanceof AuthError &&
    error.status === 401 &&
    error.reasonCode === _REFRESH_SUPERSEDED_CODE
  );
}

function _delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Serialize refreshes across tabs of this origin. Tabs share the refresh cookie,
 * so a second tab must send its request only after the first tab's response has
 * replaced the cookie. Browsers without the Web Locks API fall back to the
 * gateway's superseded-token handling below.
 */
async function _withCrossTabRefreshLock<T>(task: () => Promise<T>): Promise<T> {
  const locks =
    typeof navigator === "undefined"
      ? undefined
      : (navigator as Navigator & { locks?: LockManager }).locks;
  if (!locks || typeof locks.request !== "function") return task();
  return locks.request(_REFRESH_CROSS_TAB_LOCK, () => task()) as Promise<T>;
}

async function _refreshOnce(): Promise<RefreshResult> {
  // A superseded 401 means another tab already renewed this browser's session
  // and the gateway left the shared cookies intact. Retry with the renewed
  // cookie instead of treating the session as invalid: logging out here would
  // revoke the successor that every other tab is now using.
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await _withCrossTabRefreshLock(() =>
        _post<RefreshResult>("/api/v1/auth/refresh", {}),
      );
    } catch (error) {
      const delayMs = _REFRESH_SUPERSEDED_RETRY_DELAYS_MS[attempt];
      if (!_isRefreshSuperseded(error) || delayMs === undefined) throw error;
      await _delay(delayMs);
    }
  }
}

export async function refreshAccessToken(): Promise<RefreshResult> {
  if (!_refreshInFlight) {
    _refreshInFlight = _refreshOnce().finally(() => {
      _refreshInFlight = null;
    });
  }
  return _refreshInFlight;
}

/**
 * P4.5.27: fetch wrapper that auto-refreshes on 401.
 * Business callers replace raw fetch; 401 triggers one /refresh then retry.
 * Refresh failure → throw, caller redirects /login.
 */
export async function fetchWithRefresh(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const merged: RequestInit = { credentials: "include", ...(init || {}) };
  let r = await appFetch(input, merged);
  if (r.status !== 401) return r;
  try {
    await refreshAccessToken();
  } catch {
    return r; // Refresh also fails → return original 401 for caller handling.
  }
  // Refresh succeeds; retry original request (new access cookie written).
  r = await appFetch(input, merged);
  return r;
}

async function _fetchMeUncached(): Promise<MeUser> {
  // followup-context-deck #3: expired 1h access with surviving 30d refresh cookie → /refresh
  // once then retry /me, preventing page-load gates (useAuthRedirect / app/page.tsx / user-menu) from seeing 401
  // and directly redirecting /login, root cause of logout on reopening/waiting. Refresh failure too (cookie
  // missing/expired = truly unauthenticated) → refreshAccessToken throws AuthError(401), propagating to /login as usual.
  let me: RawMeUser;
  try {
    me = await _get<RawMeUser>("/api/v1/auth/me", {
      headers: TERMINAL_CHAT_CAPABILITY_HEADERS,
    });
  } catch (e) {
    if (e instanceof AuthError && e.status === 401) {
      try {
        await refreshAccessToken();
        me = await _get<RawMeUser>("/api/v1/auth/me", {
          headers: TERMINAL_CHAT_CAPABILITY_HEADERS,
        });
      } catch (refreshError) {
        if (refreshError instanceof AuthError && refreshError.status === 401) {
          void clearInvalidSessionAndRedirect();
        }
        throw refreshError;
      }
    } else {
      throw e;
    }
  }
  const tier: MeUser["tier"] = me.tier === "ultra" ? "ultra" : "pro";
  const creditBalance =
    typeof me.credit_balance === "number" && Number.isFinite(me.credit_balance)
      ? me.credit_balance
      : 0;
  const quotaFallback = tier === "ultra" ? 30_000 : 10_000;
  const creditQuota =
    typeof me.credit_quota === "number" && Number.isFinite(me.credit_quota)
      ? me.credit_quota
      : Math.max(creditBalance, quotaFallback);
  return {
    ...me,
    tier,
    credit_balance: creditBalance,
    credit_quota: creditQuota,
  };
}

export function fetchMe(): Promise<MeUser> {
  const now = Date.now();
  if (_meCache && now - _meCache.cachedAtMs < _ME_CACHE_TTL_MS) {
    return Promise.resolve(_meCache.value);
  }
  if (_meInFlight) return _meInFlight;

  const generation = _meCacheGeneration;
  const request = _fetchMeUncached().then((value) => {
    if (generation === _meCacheGeneration) {
      _meCache = { value, cachedAtMs: Date.now() };
    }
    return value;
  });
  _meInFlight = request;
  const clear = () => {
    if (_meInFlight === request) _meInFlight = null;
  };
  request.then(clear, clear);
  return request;
}

/** Background identity refresh that bypasses the short cache without clearing it. */
export function revalidateMe(): Promise<MeUser> {
  if (_meInFlight) return _meInFlight;
  const generation = _meCacheGeneration;
  const request = _fetchMeUncached().then((value) => {
    if (generation === _meCacheGeneration) {
      _meCache = { value, cachedAtMs: Date.now() };
    }
    return value;
  });
  _meInFlight = request;
  const clear = () => {
    if (_meInFlight === request) _meInFlight = null;
  };
  request.then(clear, clear);
  return request;
}

export async function requestSpaceAssignment(): Promise<{
  status: "submitted";
}> {
  return _post("/api/v1/auth/me/space-assignment-request", {});
}

export async function forgotPassword(
  email: string,
): Promise<{ ok: boolean; message: string }> {
  return _post("/api/v1/auth/forgot-password", { email });
}

export async function resetPassword(
  token: string,
  new_password: string,
): Promise<{ ok: boolean }> {
  return _post("/api/v1/auth/reset-password", { token, new_password });
}

export async function exchangeCredentialRecovery(
  token: string,
): Promise<{
  ok: boolean;
  status: "ready";
}> {
  return _post("/api/v1/auth/credential-recovery/intent", { token });
}

export async function completeCredentialRecovery(
  newPassword: string,
): Promise<{ ok: boolean; login_required: boolean }> {
  return _post("/api/v1/auth/credential-recovery/complete", {
    new_password: newPassword,
  });
}

export async function oauthLoginRedirect(
  provider: "feishu",
  nextPath?: string,
  flow?: "invite",
): Promise<{ redirect_url: string }> {
  const params = new URLSearchParams();
  if (nextPath) params.set("next", nextPath);
  if (flow) params.set("flow", flow);
  const qs = params.size ? `?${params.toString()}` : "";
  return _get<{ redirect_url: string }>(
    `/api/v1/auth/oauth/${provider}/login${qs}`,
  );
}

export { AuthError, AuthTransportError };

export { _post as postAuthJson };

/** Start OIDC with the browser entry header before leaving the application. */
export function oidcLoginRedirect(next: string): Promise<{ redirect_url: string }> {
  return _get(`/api/v1/auth/oidc/start?format=json&next=${encodeURIComponent(next)}`);
}
