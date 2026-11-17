import { getCurrentLocale } from "./locale";
import type { Locale } from "./locale";

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

/** The Gateway's code for an unhandled failure; it names no specific cause. */
export const GENERIC_ERROR_CODE = "E_USER_VISIBLE_GENERIC";

/**
 * The Gateway's code for a save that kept losing its storage lock to running work for its whole
 * wait (503). Nothing was written, so every surface gets the same reason and next step.
 */
export const STORE_BUSY_CODE = "store_busy";

/**
 * Why an HTTP request failed when the response names no specific error code: the
 * application or an intermediate proxy answered with only a status, or with an HTML page.
 */
export type HttpFailureKind =
  | "service_unavailable"
  | "gateway_timeout"
  | "server_error"
  | "rate_limited"
  | "payload_too_large"
  | "blocked_by_gateway"
  | "store_busy";

const HTTP_FAILURE_MESSAGES: Record<Locale, Record<HttpFailureKind, string>> = {
  "zh-CN": {
    service_unavailable: "服务暂时不可用，可能正在重启或维护，请稍后重试。",
    gateway_timeout: "服务响应超时，这次操作的结果尚未确认；请先刷新查看，再决定是否重试。",
    server_error: "服务内部出错，请稍后重试；如果持续出现，请联系管理员。",
    rate_limited: "请求过于频繁，请稍等片刻再试。",
    payload_too_large: "提交的内容过大，请减小后重试。",
    blocked_by_gateway: "请求被网关或代理拦截，没有到达应用；请联系管理员检查网关规则。",
    store_busy: "服务器正忙于其他任务，这次修改没有保存，也没有丢失任何内容。请几秒后重试。",
  },
  "en-US": {
    service_unavailable: "The service is temporarily unavailable, possibly restarting or under maintenance. Try again shortly.",
    gateway_timeout: "The service took too long to respond, so the result is unconfirmed. Refresh to check before trying again.",
    server_error: "The service ran into an internal error. Try again shortly; if it keeps happening, contact your administrator.",
    rate_limited: "Too many requests. Wait a moment and try again.",
    payload_too_large: "What you submitted is too large. Make it smaller and try again.",
    blocked_by_gateway:
      "A gateway or proxy blocked the request before it reached the application. Ask your administrator to check the gateway rules.",
    store_busy: "The server was busy with other work, so this change was not saved and nothing was lost. Try again in a few seconds.",
  },
  "de-DE": {
    service_unavailable:
      "Der Dienst ist vorübergehend nicht verfügbar, möglicherweise wegen eines Neustarts oder einer Wartung. Versuchen Sie es in Kürze erneut.",
    gateway_timeout:
      "Der Dienst hat zu lange nicht geantwortet, daher ist das Ergebnis unbestätigt. Aktualisieren Sie die Ansicht, bevor Sie es erneut versuchen.",
    server_error:
      "Im Dienst ist ein interner Fehler aufgetreten. Versuchen Sie es in Kürze erneut; wenn das weiterhin passiert, wenden Sie sich an die Administration.",
    rate_limited: "Zu viele Anfragen. Warten Sie einen Moment und versuchen Sie es erneut.",
    payload_too_large: "Die übermittelten Daten sind zu groß. Verkleinern Sie sie und versuchen Sie es erneut.",
    blocked_by_gateway:
      "Ein Gateway oder Proxy hat die Anfrage blockiert, bevor sie die Anwendung erreicht hat. Bitten Sie die Administration, die Gateway-Regeln zu prüfen.",
    store_busy:
      "Der Server war mit anderen Aufgaben beschäftigt, daher wurde diese Änderung nicht gespeichert; es ging nichts verloren. Versuchen Sie es in einigen Sekunden erneut.",
  },
  "fr-FR": {
    service_unavailable:
      "Le service est temporairement indisponible, peut-être en cours de redémarrage ou de maintenance. Réessayez dans un instant.",
    gateway_timeout:
      "Le service a mis trop de temps à répondre ; le résultat n’est donc pas confirmé. Actualisez pour vérifier avant de réessayer.",
    server_error:
      "Le service a rencontré une erreur interne. Réessayez dans un instant ; si cela se reproduit, contactez votre administrateur.",
    rate_limited: "Trop de requêtes. Patientez un instant, puis réessayez.",
    payload_too_large: "Le contenu envoyé est trop volumineux. Réduisez-le, puis réessayez.",
    blocked_by_gateway:
      "Une passerelle ou un proxy a bloqué la requête avant qu’elle n’atteigne l’application. Demandez à votre administrateur de vérifier les règles de la passerelle.",
    store_busy:
      "Le serveur était occupé par d’autres tâches : cette modification n’a pas été enregistrée et rien n’a été perdu. Réessayez dans quelques secondes.",
  },
};

/**
 * Classify a failed HTTP response that carries no specific error code. Only parsed
 * responses (`ApiError`, `HttpStatusError`, `HttpResponseFailure`) qualify: client-side errors that reuse a
 * status for an invalid payload, and responses with a domain code, keep their own copy.
 * A domain error built from a response keeps that response as its `cause` (see
 * `attachHttpCause`), so the response, not the domain wrapper, is classified.
 */
export function httpFailureKind(error: unknown): HttpFailureKind | null {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && current && typeof current === "object"; depth += 1) {
    const { name } = current as { name?: unknown };
    if (name === "ApiError" || name === "HttpStatusError" || name === "HttpResponseFailure") {
      return classifyHttpResponse(current);
    }
    current = (current as { cause?: unknown }).cause;
  }
  return null;
}

function classifyHttpResponse(response: object): HttpFailureKind | null {
  const { status, code, nonApiResponse } = response as {
    status?: unknown;
    code?: unknown;
    nonApiResponse?: unknown;
  };
  if (typeof status !== "number") return null;
  if (code === STORE_BUSY_CODE) return "store_busy";
  if (typeof code === "string" && code.trim() && code !== GENERIC_ERROR_CODE) return null;
  if (status === 504) return "gateway_timeout";
  if (status === 502 || status === 503) return "service_unavailable";
  if (status >= 500 && status < 600) return "server_error";
  if (status === 429) return "rate_limited";
  if (status === 413) return "payload_too_large";
  if (nonApiResponse === true && status >= 400) return "blocked_by_gateway";
  return null;
}

/**
 * The status and code of a failed response read by a module that cannot use
 * `readApiError` (authentication and public invitation pages). Attach it as a domain
 * error's `cause`; it is never shown.
 */
export class HttpResponseFailure extends Error {
  readonly status: number;
  readonly code?: string;
  readonly nonApiResponse: boolean;

  constructor(status: number, code?: string, nonApiResponse = false) {
    super(`HTTP ${status}`);
    this.name = "HttpResponseFailure";
    this.status = status;
    this.code = code;
    this.nonApiResponse = nonApiResponse;
  }
}

/** An HTML body answered a request: a proxy or WAF page, not this application's JSON. */
export function isHtmlErrorBody(raw: string, contentType?: string | null): boolean {
  return (
    (contentType?.toLowerCase().includes("text/html") ?? false) ||
    /^\s*<!doctype\s+html\b/i.test(raw) ||
    /^\s*<html\b/i.test(raw)
  );
}

/**
 * Keep the parsed response behind a domain error, so shared copy can still say why an
 * uncoded request failed. Only attach a real response: a client-side validation failure
 * that reuses a status code must not look like a server or proxy failure.
 */
export function attachHttpCause<T extends Error>(error: T, response: Error): T {
  error.cause = response;
  return error;
}

export function httpFailureMessage(kind: HttpFailureKind, locale: Locale = getCurrentLocale()): string {
  return HTTP_FAILURE_MESSAGES[locale][kind];
}

const READ_TIMEOUT_MESSAGES: Record<Locale, string> = {
  "zh-CN": "服务响应超时，请稍后刷新重试。",
  "en-US": "The service took too long to respond. Refresh to try again shortly.",
  "de-DE": "Der Dienst hat zu lange nicht geantwortet. Aktualisieren Sie die Ansicht in Kürze, um es erneut zu versuchen.",
  "fr-FR": "Le service a mis trop de temps à répondre. Actualisez dans un instant pour réessayer.",
};

const SENTENCE_END_RE = /[。！？；.!?;…]$/;

export interface HttpFailureReasonOptions {
  locale?: Locale;
  /**
   * The failed request only read data. A timeout then changed nothing, so the surface's
   * own sentence stays and a plain retry hint follows instead of the unconfirmed-result copy.
   */
  read?: boolean;
}

/**
 * Add the reason for an uncoded HTTP failure to a surface's own failure sentence, so
 * "what failed" stays and "why / what to do" follows. The surface's own next step was
 * written for an unknown cause ("Refresh and try again", "Check the name"), so it is
 * dropped and the reason's next step applies; facts such as "Your draft was preserved"
 * stay. A timeout of a change replaces the sentence: its result is unconfirmed, so a
 * claim that nothing was saved would be wrong.
 */
export function withHttpFailureReason(
  message: string,
  error: unknown,
  options: HttpFailureReasonOptions = {},
): string {
  const kind = httpFailureKind(error);
  if (!kind) return message;
  const locale = options.locale ?? getCurrentLocale();
  const reason = kind === "gateway_timeout" && options.read
    ? READ_TIMEOUT_MESSAGES[locale]
    : httpFailureMessage(kind, locale);
  const trimmed = message.trim();
  // A message that already names a reason (a parsed error message, or a caller that added it) stays as is.
  if (hasHttpFailureReason(trimmed, locale)) return message;
  if ((kind === "gateway_timeout" && !options.read) || !trimmed || trimmed === GENERIC_ERROR_MESSAGES[locale]) return reason;
  const base = withoutOwnNextStep(trimmed, locale);
  if (locale === "zh-CN") return `${base}${SENTENCE_END_RE.test(base) ? "" : "。"}${reason}`;
  return `${base}${SENTENCE_END_RE.test(base) ? "" : "."} ${reason}`;
}

/**
 * The start of a sentence or clause that tells the reader what to do next: retry,
 * refresh or reload, reopen, check or confirm, wait, or save again. Chinese also accepts
 * a short lead-in before 请 ("启用前请确认"); 邀请 and 申请 never qualify.
 */
const NEXT_STEP_START: Record<Locale, RegExp> = {
  "en-US": /^(?:please\s+)?(?:try|retry|refresh|reload|reopen|check|wait|save|confirm|make sure)\b/i,
  "zh-CN":
    /^(?:[^，,。；;：:]{0,12}?[前后时])?请(?:稍后|稍等|重试|再|刷新|重新|检查|先|核对|确认|等待|保存)|^或(?:刷新|重试|重新)|^稍后(?:再试|重试)|^重试|^刷新后/u,
  "de-DE":
    /^(?:(?:bitte\s+)?(?:versuchen|aktualisieren|laden|öffnen|prüfen|überprüfen|warten|wiederholen|speichern|bestätigen)\s+Sie\b|stellen\s+Sie\s+sicher\b|wählen\s+Sie\b.*\berneut\b|(?:versuche|prüfe|aktualisiere|lade|öffne|warte|speichere)\b|bitte\s+(?:erneut|aktualisieren|versuchen|warten|neu\s+laden)\b|erneut\s+versuchen\b|prüfen\s+und\b)/iu,
  "fr-FR":
    /^(?:veuillez\s+)?(?:réessayez|essayez|actualisez|rechargez|rouvrez|vérifiez|patientez|recommencez|relancez|enregistrez|confirmez|assurez-vous)/iu,
};
const LATIN_SENTENCE_END = /[.!?;](?=\s)\s*|\s[;—–]\s+/gu;
const LATIN_CLAUSE_END = /[,:](?=\s)\s*/gu;
const CJK_SENTENCE_END = /[。！？；;]/gu;
const CJK_CLAUSE_END = /[，,：:]/gu;
/** A Chinese lead-in that means nothing without the instruction it introduces. */
const CJK_LEAD_IN = /^(?:如果|如|若|假如|要是|要|一旦|为了|为|在|当|对于|关于|请)|[前后时]$/u;
const TRAILING_SEPARATOR = /[\s,，、;；:：—–]+$/u;

function splitAfter(text: string, separators: RegExp): string[] {
  const parts: string[] = [];
  let start = 0;
  for (const match of text.matchAll(separators)) {
    const end = (match.index ?? 0) + match[0].length;
    parts.push(text.slice(start, end));
    start = end;
  }
  if (start < text.length) parts.push(text.slice(start));
  return parts;
}

/**
 * Drop a failure message's own next step, from the first sentence or clause that gives
 * one to the end. The message's first clause (what failed) always stays. In a later
 * sentence, the words before the instruction stay only when they state a fact in
 * Chinese ("草稿已保留，请重试"); elsewhere they are a lead-in or a companion
 * instruction ("Before enabling, check …"), so the whole sentence goes.
 */
export function withoutOwnNextStep(message: string, locale: Locale): string {
  const cjk = locale === "zh-CN";
  const nextStep = NEXT_STEP_START[locale];
  const sentences = splitAfter(message, cjk ? CJK_SENTENCE_END : LATIN_SENTENCE_END);
  for (let index = 0; index < sentences.length; index += 1) {
    const clauses = splitAfter(sentences[index], cjk ? CJK_CLAUSE_END : LATIN_CLAUSE_END);
    const at = clauses.findIndex((clause, position) => (index > 0 || position > 0) && nextStep.test(clause.trim()));
    if (at < 0) continue;
    const before = clauses.slice(0, at).join("").replace(TRAILING_SEPARATOR, "");
    const keepBefore = before !== "" && (index === 0 || (cjk && !CJK_LEAD_IN.test(before.trim())));
    const failed = `${sentences.slice(0, index).join("")}${keepBefore ? before : ""}`.replace(TRAILING_SEPARATOR, "");
    // A fragment with no statement of its own (", please wait.") stays whole.
    return failed || message;
  }
  return message;
}

function hasHttpFailureReason(message: string, locale: Locale): boolean {
  return (
    message.includes(READ_TIMEOUT_MESSAGES[locale]) ||
    Object.values(HTTP_FAILURE_MESSAGES[locale]).some((reason) => message.includes(reason))
  );
}

/**
 * The message of a coded API error (the gateway fills it from the localized error
 * catalog), or `fallback` when the error has no code or no usable copy. Uncoded
 * errors (network failures, raw machine codes) never reach the page verbatim; an
 * uncoded HTTP failure adds its kind to `fallback`.
 */
export function codedErrorMessage(
  error: unknown,
  fallback: string,
  codePrefixes: readonly string[],
): string {
  if (!error || typeof error !== "object") return fallback;
  const { code, message } = error as { code?: unknown; message?: unknown };
  // Only codes this surface publishes through the catalog; anything else may carry provider text.
  if (typeof code !== "string" || !codePrefixes.some((prefix) => code.startsWith(prefix))) {
    return withHttpFailureReason(fallback, error);
  }
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
