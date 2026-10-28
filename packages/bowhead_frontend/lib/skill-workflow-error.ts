import { isNetworkFailure } from "./transport-error";
import { ApiError } from "./api-client";
import {
  parseErrorDiagnosticsWire,
  projectTransportFailure,
  toErrorPresentation,
} from "./error-presentation";
import type {
  ErrorPresentation,
  ErrorSurface,
  TransportFailureMessages,
} from "./error-presentation";
import { getCurrentLocale } from "./locale";
import { staticTranslator } from "./locale-messages";
import { entitlementErrorMessage } from "./entitlement-error";
import { safeUserFacingErrorMessage } from "./user-facing-error";

export interface SkillWorkflowErrorCopy {
  generic: string;
  transport: TransportFailureMessages;
}

export function isWorkflowCreatorDisabled(error: unknown): boolean {
  return (
    error instanceof ApiError
    && error.status === 404
    && error.code === "workflow_creator_disabled"
  );
}

/**
 * Project Skills/Workflow failures without ever rendering Error.message from
 * an untyped exception or an unregistered HTTP response.
 */
export function skillWorkflowErrorPresentation(
  error: unknown,
  {
    surface,
    copy,
  }: {
    surface: Extract<ErrorSurface, "settings" | "upload" | "task">;
    copy: SkillWorkflowErrorCopy;
  },
): ErrorPresentation {
  if (!(error instanceof ApiError)) {
    if (!isNetworkFailure(error)) return { message: copy.generic };
    return (
      projectTransportFailure(
        {
          phase: "send",
          timeoutTriggered: false,
          userAborted: false,
          onlineHint:
            typeof navigator === "undefined" ? undefined : navigator.onLine,
        },
        copy.transport,
      ) ?? { message: copy.generic }
    );
  }

  const publishedForSurface =
    (
      error.diagnostics?.diagnosticCode === "skill_workflow_failure"
      || error.diagnostics?.diagnosticCode
        === "request_validation_failure"
    )
    && error.diagnostics.code === error.code;
  const finiteValidation = error.code === "request_validation_failed";
  const workspaceAccessDenied = error.code?.startsWith("ws_access_denied") === true;

  if (workspaceAccessDenied) {
    return {
      message: staticTranslator("skill_workflow_error")("workspace_access_denied"),
    };
  }

  return toErrorPresentation(error, {
    locale: getCurrentLocale(),
    surface,
    allowedDisclosures: ["authenticated", "public-neutral"],
    genericMessage: copy.generic,
    messageForCode: (code) =>
      entitlementErrorMessage(error, getCurrentLocale(), {
        featureFallback: "mcp",
      })
      ?? ((publishedForSurface || finiteValidation) && code === error.code
        ? safeUserFacingErrorMessage(error.message, code)
        : null),
  });
}

/**
 * Project an HTTP-200 semantic failure only when the named backend publisher
 * authenticates the payload. Legacy prose without that marker is never shown.
 */
export function semanticSkillWorkflowErrorPresentation(
  {
    code,
    message,
    diagnostics: diagnosticsWire,
  }: {
    code?: string | null;
    message?: string | null;
    diagnostics?: unknown;
  },
  fallback: string,
): ErrorPresentation {
  const diagnostics = parseErrorDiagnosticsWire(diagnosticsWire);
  const published =
    diagnostics?.diagnosticCode === "skill_workflow_failure"
    && typeof code === "string"
    && code.length > 0;

  if (!published) return { message: fallback };

  return {
    message: safeUserFacingErrorMessage(message, code),
    diagnostics: {
      ...diagnostics,
      code,
    },
  };
}
