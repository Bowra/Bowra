import { copyComposerDocumentRevision, type ComposerDocumentRevision } from "./composer-document-revision";
import type { ComposerOrdinaryDraft } from "./composer-ordinary-draft";
import { assertComposerTextDraftFormat } from "./composer-draft-format";
import {
  copyComposerReferenceSnapshot,
  type ComposerReferenceSnapshot,
} from "./composer-reference-transfer";
import { ComposerPersistenceVersionError } from "./composer-local-persistence-db";

export type ComposerTransientDraft = Pick<ComposerOrdinaryDraft, "message" | "attachments"> & {
  expectedRevision: string | null;
};

type CacheEntry = {
  snapshot: ComposerTransientDraft | ComposerReferenceTransientDraft | ComposerRevisionTransientDraft;
  retainedAt: number;
};

export type ComposerRevisionTransientDraft = Omit<ComposerReferenceSnapshot, "schemaVersion"> & {
  schemaVersion: 3;
  documentRevision: ComposerDocumentRevision;
  expectedRevision: string | null;
};

export type ComposerInputTransientDraft = ComposerTransientDraft | ComposerReferenceTransientDraft | ComposerRevisionTransientDraft;

export type ComposerReferenceTransientDraft = ComposerReferenceSnapshot & {
  expectedRevision: string | null;
};

function referenceTransientCopy(
  snapshot: ComposerReferenceTransientDraft,
): ComposerReferenceTransientDraft {
  if (
    Object.keys(snapshot).some(
      (field) =>
        !["schemaVersion", "message", "input_document", "attachments", "expectedRevision"].includes(
          field,
        ),
    ) ||
    (snapshot.expectedRevision !== null &&
      (typeof snapshot.expectedRevision !== "string" ||
        !snapshot.expectedRevision ||
        snapshot.expectedRevision.length > 256))
  ) {
    throw new ComposerPersistenceVersionError("invalid reference transient draft");
  }
  return {
    ...copyComposerReferenceSnapshot(snapshot),
    expectedRevision: snapshot.expectedRevision,
  };
}

const MAX_TRANSIENT_DRAFTS = 8;
const TRANSIENT_DRAFT_TTL_MS = 30 * 60_000;
const transientDrafts = new Map<string, CacheEntry>();
function pruneTransientDrafts(now: number): void {
  for (const [scopeSignature, entry] of transientDrafts) {
    if (now - entry.retainedAt > TRANSIENT_DRAFT_TTL_MS) {
      transientDrafts.delete(scopeSignature);
    }
  }
  while (transientDrafts.size > MAX_TRANSIENT_DRAFTS) {
    const oldest = transientDrafts.keys().next().value as string | undefined;
    if (!oldest) break;
    transientDrafts.delete(oldest);
  }
}

/**
 * Same-document handoff for an edit whose final IndexedDB CAS is still in
 * flight while the user changes Session/space. It is bounded, expires, and is
 * never a recovery authority: the ordinary-draft record always wins.
 */
export function retainComposerTransientDraft(
  scopeSignature: string,
  snapshot: ComposerTransientDraft,
): void {
  if (!scopeSignature) return;
  assertComposerTextDraftFormat(snapshot);
  const current = transientDrafts.get(scopeSignature)?.snapshot;
  if (current) assertComposerTextDraftFormat(current);
  const copy = structuredClone(snapshot);
  const now = Date.now();
  transientDrafts.delete(scopeSignature);
  transientDrafts.set(scopeSignature, { snapshot: copy, retainedAt: now });
  pruneTransientDrafts(now);
}

/** Versioned in-memory contract for a future editor; text writers stay fenced. */
export function retainComposerReferenceTransientDraft(
  scopeSignature: string,
  snapshot: ComposerReferenceTransientDraft,
): void {
  if (!scopeSignature) return;
  const current = transientDrafts.get(scopeSignature)?.snapshot;
  if (current && "schemaVersion" in current && current.schemaVersion === 3)
    throw new ComposerPersistenceVersionError("Document revision requires its input owner");
  const copy = referenceTransientCopy(snapshot);
  const now = Date.now();
  transientDrafts.delete(scopeSignature);
  transientDrafts.set(scopeSignature, { snapshot: copy, retainedAt: now });
  pruneTransientDrafts(now);
}

export function readComposerReferenceTransientDraft(
  scopeSignature: string,
): ComposerReferenceTransientDraft | null {
  pruneTransientDrafts(Date.now());
  const snapshot = transientDrafts.get(scopeSignature)?.snapshot;
  if (!snapshot) return null;
  if (!("schemaVersion" in snapshot) || snapshot.schemaVersion !== 2) {
    assertComposerTextDraftFormat(snapshot);
    return null;
  }
  return referenceTransientCopy(snapshot as ComposerReferenceTransientDraft);
}

export function readComposerTransientDraft(scopeSignature: string): ComposerTransientDraft | null {
  pruneTransientDrafts(Date.now());
  const snapshot = transientDrafts.get(scopeSignature)?.snapshot;
  if (!snapshot) return null;
  assertComposerTextDraftFormat(snapshot);
  return structuredClone(snapshot);
}

export function clearComposerTransientDraft(
  scopeSignature: string,
  snapshot: ComposerTransientDraft,
): void {
  const current = transientDrafts.get(scopeSignature)?.snapshot;
  if (current && JSON.stringify(current) === JSON.stringify(snapshot)) {
    transientDrafts.delete(scopeSignature);
  }
}

/** Drops one scope's handoff after the user explicitly discarded that draft. */
export function forgetComposerTransientDraft(scopeSignature: string): void {
  transientDrafts.delete(scopeSignature);
}

export function clearAllComposerTransientDrafts(): void {
  transientDrafts.clear();
}

/** The existing bounded handoff cache, retaining every input-owned field. */
export function retainComposerInputTransientDraft(scopeSignature: string, snapshot: ComposerInputTransientDraft): void {
  if (!scopeSignature) return;
  const copy = inputTransientCopy(snapshot);
  const now = Date.now();
  transientDrafts.delete(scopeSignature);
  transientDrafts.set(scopeSignature, { snapshot: copy, retainedAt: now });
  pruneTransientDrafts(now);
}

export function readComposerInputTransientDraft(scopeSignature: string): ComposerInputTransientDraft | null {
  pruneTransientDrafts(Date.now());
  const snapshot = transientDrafts.get(scopeSignature)?.snapshot;
  return snapshot ? inputTransientCopy(snapshot) : null;
}

function inputTransientCopy(snapshot: ComposerInputTransientDraft): ComposerInputTransientDraft {
  if (!("schemaVersion" in snapshot)) {
    assertComposerTextDraftFormat(snapshot);
    return structuredClone(snapshot);
  }
  if (snapshot.schemaVersion === 2) return referenceTransientCopy(snapshot);
  if (snapshot.schemaVersion !== 3 || Object.keys(snapshot).some((key) =>
    !["schemaVersion", "message", "input_document", "attachments", "expectedRevision", "documentRevision"].includes(key)))
    throw new ComposerPersistenceVersionError("Invalid input transient draft");
  const { documentRevision, ...base } = snapshot;
  return { ...referenceTransientCopy({ ...base, schemaVersion: 2 }), schemaVersion: 3,
    documentRevision: copyComposerDocumentRevision(documentRevision) };
}
