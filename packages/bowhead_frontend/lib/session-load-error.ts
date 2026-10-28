
import { isNetworkFailure } from "@/lib/transport-error";
import { dismissToast, toast, type Toast } from "@/lib/use-toast";

/**
 * Session-opening failure → key under `session.*` for user-facing copy.
 *
 * Select by status only; never display raw text such as “session detail: 500” or “canvas: 500”;
 * callers put status and request ID in Copy Diagnostics.
 */
export type SessionLoadFailedKey =
  | "load_failed_forbidden"
  | "load_failed_gone"
  | "load_failed_network"
  | "load_failed_plain";

export function sessionLoadFailedKey(error: unknown): SessionLoadFailedKey {
  const status =
    error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number"
      ? (error as { status: number }).status
      : undefined;
  if (status === 403) return "load_failed_forbidden";
  if (status === 404 || status === 410) return "load_failed_gone";
  if (status === undefined && isNetworkFailure(error)) return "load_failed_network";
  return "load_failed_plain";
}

// Session-load error Toasts need manual dismissal (errors never auto-dismiss); Toasts with Copy Diagnostics actions
// are not text-deduplicated by use-toast, so repeated clicks stack them. Keep only the latest; dismiss on successful opening.
let _sessionLoadToastId: string | null = null;

/** Show a session-load failure Toast, dismissing the previous similar failure so only the latest remains. */
export function showSessionLoadFailure(input: Omit<Toast, "id" | "count">): void {
  if (_sessionLoadToastId) dismissToast(_sessionLoadToastId);
  _sessionLoadToastId = toast(input);
}

/** Call after a session opens successfully: the old failure Toast no longer applies; dismiss it. */
export function clearSessionLoadFailure(): void {
  if (!_sessionLoadToastId) return;
  dismissToast(_sessionLoadToastId);
  _sessionLoadToastId = null;
}
