import { basePath } from "./base-path";
import { completeComposerScope, type ComposerPersistenceScope } from "./composer-persistence-scope";
import { sha256HexFallback } from "./sha256-fallback";
import type { ThetaClarificationQuestion } from "./types";

export const CLARIFICATION_ANSWER_MAX_LENGTH = 240;

export interface ClarificationDraft {
  selections: number[][];
  customAnswers: string[];
  remember: boolean;
}

export interface ClarificationDraftIdentity {
  requestId: string;
  fingerprint: string;
}

export function clarificationDraftIdentity(
  scope: ComposerPersistenceScope | null,
  sessionId: string | null,
  turnId: string | null,
  requestId: string,
  formSignature: string,
): ClarificationDraftIdentity | null {
  if (!completeComposerScope(scope) || !sessionId || !turnId || !requestId
    || (scope.requestedSessionId !== null && scope.requestedSessionId !== sessionId)) return null;
  const material = JSON.stringify([
    basePath(), scope.userId, scope.teamId, scope.workspaceId, scope.authSessionGeneration,
    sessionId, turnId, requestId, formSignature,
  ]);
  return { requestId, fingerprint: sha256HexFallback(new TextEncoder().encode(material)) };
}

function storageKey(requestId: string): string {
  return `bowhead.clarification-draft.v1.${requestId}`;
}

export function readClarificationDraft(
  identity: ClarificationDraftIdentity,
  questions: ThetaClarificationQuestion[],
  allowCustomAnswers: boolean,
): ClarificationDraft | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey(identity.requestId)) ?? "null") as
      | (Partial<ClarificationDraft> & { fingerprint?: string })
      | null;
    if (!saved || saved.fingerprint !== identity.fingerprint) return null;
    const { selections, customAnswers, remember } = saved;
    if (!Array.isArray(selections) || selections.length !== questions.length
      || !Array.isArray(customAnswers) || customAnswers.length !== questions.length
      || typeof remember !== "boolean") return null;
    const valid = questions.every((question, index) => {
      const choices = selections[index];
      const custom = customAnswers[index];
      return Array.isArray(choices)
        && choices.every((choice) => Number.isInteger(choice) && choice >= 0 && choice < question.options.length)
        && new Set(choices).size === choices.length
        && typeof custom === "string" && custom.length <= CLARIFICATION_ANSWER_MAX_LENGTH
        && (allowCustomAnswers || !custom)
        && (question.type !== "single_select" || choices.length + Number(Boolean(custom.trim())) <= 1);
    });
    return valid ? { selections, customAnswers, remember } : null;
  } catch {
    return null;
  }
}

export function saveClarificationDraft(
  identity: ClarificationDraftIdentity,
  draft: ClarificationDraft | null,
): boolean {
  const key = storageKey(identity.requestId);
  try {
    if (draft) sessionStorage.setItem(key, JSON.stringify({ ...draft, fingerprint: identity.fingerprint }));
    else sessionStorage.removeItem(key);
    return true;
  } catch {
    clearClarificationDraft(identity.requestId);
    return false;
  }
}

/** Request IDs are globally unique. Authoritative settlement can clean the draft even during replay. */
export function clearClarificationDraft(requestId: string): void {
  if (!requestId) return;
  try {
    sessionStorage.removeItem(storageKey(requestId));
  } catch {
    // Storage can be disabled in the browser. It must not block settlement.
  }
}
