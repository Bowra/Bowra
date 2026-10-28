import { sha256Hex } from "./sha256-fallback";

export interface ComposerPersistenceScope {
  userId: string;
  teamId: string;
  workspaceId: string;
  requestedSessionId: string | null;
  authSessionGeneration: string;
}

/** UI readers must reject incomplete owners before displaying a draft. */
export function completeComposerScope(
  scope: ComposerPersistenceScope | null,
): scope is ComposerPersistenceScope {
  return Boolean(
    scope &&
    [scope.userId, scope.teamId, scope.workspaceId, scope.authSessionGeneration]
      .every((value) => typeof value === "string" && value.trim()) &&
    (scope.requestedSessionId === null ||
      (typeof scope.requestedSessionId === "string" && scope.requestedSessionId.trim())),
  );
}

function lengthPrefixedKey(domain: string, values: string[]): string {
  const encoder = new TextEncoder();
  const parts = [domain, ...values].map((value) => encoder.encode(value));
  const size = parts.reduce((total, part) => total + 4 + part.byteLength, 0);
  const bytes = new Uint8Array(size);
  const view = new DataView(bytes.buffer);
  let offset = 0;
  for (const part of parts) {
    view.setUint32(offset, part.byteLength, false);
    offset += 4;
    bytes.set(part, offset);
    offset += part.byteLength;
  }
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export async function composerPersistenceKeys(scope: ComposerPersistenceScope) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const identityKey = lengthPrefixedKey("recovery-identity.v1", [origin, scope.userId]);
  const principalKey = lengthPrefixedKey(
    "recovery-principal.v1",
    [origin, scope.userId, scope.teamId],
  );
  const manifestKey = lengthPrefixedKey(
    "conversation-draft.v1",
    [principalKey, scope.workspaceId, scope.requestedSessionId ?? ""],
  );
  const authBytes = new TextEncoder().encode(
    `bowhead-auth-session-generation.v1\0${scope.authSessionGeneration}`,
  );
  return {
    identityKey,
    principalKey,
    manifestKey,
    authSessionGenerationHash: await sha256Hex(authBytes),
  };
}

export function composerOrdinaryDraftStorageKey(
  manifestKey: string,
  authSessionGenerationHash: string,
): string {
  return lengthPrefixedKey(
    "ordinary-draft.v2",
    [manifestKey, authSessionGenerationHash],
  );
}

export function composerDraftScopeSignature(
  scope: ComposerPersistenceScope | null,
): string {
  if (!scope) return "";
  return JSON.stringify([
    scope.userId,
    scope.teamId,
    scope.workspaceId,
    scope.requestedSessionId ?? "",
    scope.authSessionGeneration,
  ]);
}
