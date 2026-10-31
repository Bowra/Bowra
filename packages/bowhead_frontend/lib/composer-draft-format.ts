import { copyComposerDocumentRevision } from "./composer-document-revision";
import { ComposerPersistenceVersionError } from "./composer-local-persistence-db";
import {
  inputDocumentDisplayText,
  parseInputDocument,
  type InputDocument,
} from "./composer-input-document";

/** The reader understands this envelope; understanding never grants execution. */
export function readComposerReferenceInput(
  value: object,
  allowDocumentRevision = false,
): InputDocument | null {
  const draft = value as Record<string, unknown>;
  if (allowDocumentRevision && draft.schemaVersion === 3) {
    try { copyComposerDocumentRevision(draft.documentRevision); }
    catch { throw new ComposerPersistenceVersionError("Invalid document revision draft"); }
  } else if (draft.schemaVersion !== 2) {
    assertComposerTextDraftFormat(value);
    return null;
  }
  try {
    const document = parseInputDocument(draft.input_document);
    if (draft.message !== inputDocumentDisplayText(document))
      throw new Error("display mismatch");
    return document;
  } catch {
    // Preserve invalid/unknown documents as well; never expose parser internals.
    throw new ComposerPersistenceVersionError(
      "composer reference draft cannot be decoded",
    );
  }
}

/** Guard the text-only consumers until versioned reference readers are wired. */
export function assertComposerTextDraftFormat(value: object): void {
  const draft = value as Record<string, unknown>;
  if (
    (draft.schemaVersion !== undefined && draft.schemaVersion !== 1) ||
    Object.hasOwn(draft, "input_document") || Object.hasOwn(draft, "documentRevision") || Object.hasOwn(draft, "documentRevisionTarget") || Object.hasOwn(draft, "contextMaintenance") || Object.hasOwn(draft, "terminalAction")
  ) {
    throw new ComposerPersistenceVersionError(
      "composer draft format requires a newer client",
    );
  }
}
