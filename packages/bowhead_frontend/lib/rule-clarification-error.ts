import { isNetworkFailure } from "./transport-error";
import { ApiError } from "./api-client";
import {
  projectTransportFailure,
  toErrorPresentation,
} from "./error-presentation";
import type {
  ErrorDiagnostics,
  ErrorPresentation,
  ErrorSurface,
  TransportFailureMessages,
} from "./error-presentation";
import { entitlementErrorMessage } from "./entitlement-error";
import { getCurrentLocale } from "./locale";
import { safeUserFacingErrorMessage } from "./user-facing-error";

const SAFE_RULE_CLARIFICATION_CODES = new Set([
  "E_USER_VISIBLE_GENERIC",
  "authz_lookup_failed",
  "authz_unavailable",
  "clarification_access_denied",
  "clarification_answer_required",
  "clarification_context_selection_invalid",
  "clarification_context_selection_unavailable",
  "clarification_cancel_directive_invalid",
  "clarification_input_rejected",
  "clarification_not_found",
  "clarification_not_resolvable",
  "clarification_operation_failed",
  "clarification_pause_pending",
  "clarification_orchestrator_unavailable",
  "clarification_plan_reapprove_required",
  "clarification_resume_not_ready",
  "clarification_resume_unavailable",
  "clarification_store_unavailable",
  "clock_tamper",
  "entitlement_denied",
  "entitlement_unavailable",
  "feature_unlicensed",
  "needs_license",
  "not_workspace_member",
  "over_grace",
  "request_validation_failed",
  "seat_exceeded",
  "session_deleted",
  "suspended",
  "tamper",
  "team_pool_exhausted",
  "user_credit_exhausted",
  "user_not_active",
  "user_store_unavailable",
  "workspace_archived",
  "workspace_required",
  "workspace_role_unknown",
  "workspace_unavailable",
  "ws_access_denied",
]);

export interface RuleClarificationErrorCopy {
  generic: string;
  entitlementDenied: string;
  transport: TransportFailureMessages;
}

/**
 * Present Business Rules/Clarification failures without promoting arbitrary
 * Error.message or unregistered backend prose into the UI.
 */
export function ruleClarificationErrorPresentation(
  error: unknown,
  {
    surface,
    copy,
    transportPhase = "send",
    retryAction = false,
  }: {
    surface: Extract<ErrorSurface, "chat" | "settings" | "task">;
    copy: RuleClarificationErrorCopy;
    transportPhase?: "connect" | "send" | "receive";
    retryAction?: boolean;
  },
): ErrorPresentation {
  if (!(error instanceof ApiError)) {
    if (!isNetworkFailure(error)) return { message: copy.generic };
    const presentation =
      projectTransportFailure(
        {
          phase: transportPhase,
          timeoutTriggered: false,
          userAborted: false,
          onlineHint:
            typeof navigator === "undefined" ? undefined : navigator.onLine,
        },
        copy.transport,
      ) ?? { message: copy.generic };
    return retryAction
      ? presentation
      : { ...presentation, action: undefined };
  }

  const publishedForSurface =
    (
      error.diagnostics?.diagnosticCode === "rule_clarification_failure"
      || error.diagnostics?.diagnosticCode
        === "request_validation_failure"
    )
    && error.diagnostics.code === error.code;
  const entitlementDenied =
    error.status === 402
    || error.code === "entitlement_denied"
    || error.code === "feature_unlicensed"
    || error.code === "needs_license"
    || error.code === "over_grace"
    || error.code === "seat_exceeded"
    || error.code === "suspended"
    || error.code === "tamper"
    || error.code === "team_pool_exhausted"
    || error.code === "user_credit_exhausted";

  return toErrorPresentation(
    {
      ...error,
      diagnostics:
        error.diagnostics
        ?? transportDiagnostics(error),
    },
    {
      locale: getCurrentLocale(),
      surface,
      allowedDisclosures: ["authenticated", "public-neutral"],
      genericMessage: copy.generic,
      messageForCode: (code) => {
        // Exhausted credit is not an authorization problem: show Credit Exhausted copy instead of falling through to Authorization Denied
        if (
          error.code === "team_pool_exhausted"
          || error.code === "user_credit_exhausted"
        ) {
          return entitlementErrorMessage(error, getCurrentLocale());
        }
        if (entitlementDenied) return copy.entitlementDenied;
        return publishedForSurface && code === error.code
          ? safeUserFacingErrorMessage(error.message, code)
          : null;
      },
    },
  );
}

function transportDiagnostics(error: ApiError): ErrorDiagnostics {
  const code =
    error.code && SAFE_RULE_CLARIFICATION_CODES.has(error.code)
      ? error.code
      : undefined;
  return {
    schemaVersion: 1,
    disclosure: "authenticated",
    status: error.status,
    ...(code ? { code } : {}),
    ...(error.requestId ? { requestId: error.requestId } : {}),
    observedAt: new Date().toISOString(),
  };
}
