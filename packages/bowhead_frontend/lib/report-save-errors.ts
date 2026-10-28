import { isNetworkFailure } from "@/lib/transport-error";
/**
 * Report-edit autosave failures → user-visible explanations.
 *
 * Autosave runs outside the React tree (debounced callbacks, global retry queue), without useTranslations,
 * so `staticTranslator()` reads messages `report_save_errors` by UI language
 * (untranslated languages have English placeholders in the catalog).
 * Choose sentences only by failure kind, without status codes, raw API wording or block IDs; status/request IDs stay on the failure object,
 * copied through the UI's Copy Diagnostics action.
 */

import { staticTranslator } from "@/lib/locale-messages";

/** Failure kinds, also keys under messages `report_save_errors`. */
export type ReportSaveFailure = "network" | "session_expired" | "forbidden" | "generic";

export function reportSaveFailureKind(error: unknown): ReportSaveFailure {
  const status =
    error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number"
      ? (error as { status: number }).status
      : undefined;
  if (status === 401) return "session_expired";
  if (status === 403) return "forbidden";
  if (status === undefined && isNetworkFailure(error)) return "network";
  return "generic";
}

export function reportSaveFailureMessage(error: unknown): string {
  return staticTranslator("report_save_errors")(reportSaveFailureKind(error));
}

/** Copy Diagnostics labels for out-of-tree Toasts: directly read matching errors.diagnostics keys. */
export function reportSaveCopyLabels(): {
  copyShort: string;
  copied: string;
  copyShortFailed: string;
  copyFallbackPrompt: string;
} {
  const t = staticTranslator("errors.diagnostics");
  return {
    copyShort: t("copyShort"),
    copied: t("copied"),
    copyShortFailed: t("copyShortFailed"),
    copyFallbackPrompt: t("copyFallbackPrompt"),
  };
}
