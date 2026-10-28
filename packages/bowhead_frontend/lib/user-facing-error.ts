import { getCurrentLocale } from "./locale";

const GENERIC_ERROR_MESSAGES = {
  "zh-CN": "操作失败，请稍后重试；如问题持续，请联系管理员。",
  "en-US":
    "Something went wrong. Please try again. If the problem continues, contact your administrator.",
  "de-DE":
    "Ein Fehler ist aufgetreten. Bitte versuchen Sie es erneut. Wenn das Problem weiterhin besteht, wenden Sie sich an die Administration.",
  "fr-FR":
    "Une erreur s’est produite. Réessayez. Si le problème persiste, contactez votre administrateur.",
} as const;

export function genericUserFacingErrorMessage(): string {
  return GENERIC_ERROR_MESSAGES[getCurrentLocale()];
}

/**
 * The message of a coded API error (the gateway fills it from the localized error
 * catalog), or `fallback` when the error has no code or no usable copy. Uncoded
 * errors (network failures, raw machine codes) never reach the page verbatim.
 */
export function codedErrorMessage(
  error: unknown,
  fallback: string,
  codePrefixes: readonly string[],
): string {
  if (!error || typeof error !== "object") return fallback;
  const { code, message } = error as { code?: unknown; message?: unknown };
  // Only codes this surface publishes through the catalog; anything else may carry provider text.
  if (typeof code !== "string" || !codePrefixes.some((prefix) => code.startsWith(prefix))) return fallback;
  const text = typeof message === "string" ? message.trim() : "";
  if (!text || text === code || text === genericUserFacingErrorMessage()) return fallback;
  return text;
}

export function safeUserFacingErrorMessage(
  message: unknown,
  code?: string,
): string {
  const candidate = typeof message === "string" ? message.trim() : "";
  if (!candidate || (code && candidate === code)) {
    return genericUserFacingErrorMessage();
  }
  return candidate;
}
