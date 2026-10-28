import type { DocumentDraftOpen } from "./document-canvas-events";
import {
  documentCanvasScopeKey,
  type DocumentCanvasScope,
} from "./document-canvas-navigation";

const storageKey = "bowhead.canvas.document.dismissals.v1";
const limit = 128;

/** Tab-local view intent contains identifiers only and never grants document access. */
export function documentDismissalIdentity(
  scope: DocumentCanvasScope,
  pointer: DocumentDraftOpen,
): string {
  return JSON.stringify([
    documentCanvasScopeKey(scope),
    pointer.job_id,
    pointer.turn_id,
    pointer.draft_digest,
  ]);
}

export function readDocumentDismissals(): Set<string> {
  try {
    const value: unknown = JSON.parse(
      window.sessionStorage.getItem(storageKey) ?? "[]",
    );
    if (!Array.isArray(value) || value.length > limit) return new Set();
    return new Set(
      value.filter((entry): entry is string => {
        if (typeof entry !== "string" || entry.length > 2048) return false;
        const parts: unknown = JSON.parse(entry);
        return (
          Array.isArray(parts) &&
          parts.length === 4 &&
          parts.every((part) => typeof part === "string")
        );
      }),
    );
  } catch {
    return new Set();
  }
}

export function persistDocumentDismissals(identities: Set<string>): void {
  while (identities.size > limit)
    identities.delete(identities.values().next().value!);
  try {
    window.sessionStorage.setItem(storageKey, JSON.stringify([...identities]));
  } catch {
    // Closing still works in memory when browser storage is unavailable.
  }
}

export function clearDocumentDismissal(
  identities: Set<string>,
  scope: DocumentCanvasScope,
  jobId: string,
): void {
  for (const identity of identities) {
    const [savedScope, savedJob] = JSON.parse(identity) as string[];
    if (savedScope === documentCanvasScopeKey(scope) && savedJob === jobId)
      identities.delete(identity);
  }
  persistDocumentDismissals(identities);
}
