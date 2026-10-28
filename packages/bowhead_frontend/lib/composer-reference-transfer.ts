import { canonicalizeComposerAttachmentRefs } from "./composer-attachment-refs";
import { readComposerReferenceInput } from "./composer-draft-format";
import type { InputDocument } from "./composer-input-document";
import {
  ComposerPersistenceVersionError,
  composerRecoveryUnavailable,
} from "./composer-local-persistence-db";
import {
  composerOrdinaryDraftStorageKey,
  composerPersistenceKeys,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";
import type { AttachmentRef } from "./types";

export interface ComposerReferenceSnapshot {
  schemaVersion: 2;
  message: string;
  input_document: InputDocument;
  attachments: AttachmentRef[];
}

/** A complete copy, never a reference to mutable caller-owned nodes/attachments. */
export function copyComposerReferenceSnapshot(value: unknown): ComposerReferenceSnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ComposerPersistenceVersionError("invalid reference transfer");
  }
  const record = value as Record<string, unknown>;
  const document = readComposerReferenceInput(record);
  const attachments = canonicalizeComposerAttachmentRefs(record.attachments);
  if (!document || !attachments) {
    throw new ComposerPersistenceVersionError("invalid reference transfer");
  }
  return {
    schemaVersion: 2,
    message: record.message as string,
    input_document: document,
    attachments,
  };
}

/** Separate from the one-shot text keys: legacy consumers must never take it. */
export async function composerReferenceHandoffKey(
  scope: ComposerPersistenceScope,
): Promise<string> {
  const keys = await composerPersistenceKeys(scope);
  return `bowhead:composer-reference-handoff:v2:${composerOrdinaryDraftStorageKey(keys.manifestKey, keys.authSessionGenerationHash)}`;
}

/** Reading does not consume the handoff or authorize sending it. */
export async function readComposerReferenceHandoff(
  scope: ComposerPersistenceScope,
): Promise<ComposerReferenceSnapshot | null> {
  return (await readComposerReferenceHandoffEnvelope(scope))?.snapshot ?? null;
}

/** The original bytes are the compare-and-delete witness after a durable save. */
export async function readComposerReferenceHandoffEnvelope(
  scope: ComposerPersistenceScope,
): Promise<{ raw: string; snapshot: ComposerReferenceSnapshot } | null> {
  const key = await composerReferenceHandoffKey(scope);
  let raw: string | null;
  try {
    raw = sessionStorage.getItem(key);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
  if (raw === null) return null;
  try {
    // Bound parsing before decoding a browser-controlled envelope. InputDocument
    // has its own stricter byte/depth limits; attachments retain existing limits.
    if (raw.length > 1_000_000) throw new Error("oversized reference handoff");
    const record = JSON.parse(raw) as Record<string, unknown>;
    if (
      !record ||
      typeof record !== "object" ||
      Array.isArray(record) ||
      Object.keys(record).some(
        (field) => !["schemaVersion", "message", "input_document", "attachments"].includes(field),
      )
    ) {
      throw new Error("unsupported reference handoff");
    }
    return { raw, snapshot: copyComposerReferenceSnapshot(record) };
  } catch {
    throw new ComposerPersistenceVersionError("reference handoff requires a compatible reader");
  }
}
