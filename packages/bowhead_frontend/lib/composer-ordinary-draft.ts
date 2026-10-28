import { copyComposerDocumentRevision, sameDocumentRevisionTarget, type DocumentRevisionTarget, type ComposerDocumentRevision } from "./composer-document-revision";
import { isComposerAgentDraftTarget } from "./composer-chat-action";
import {
  COMPOSER_ORDINARY_DRAFT_STORE,
  ComposerRecoveryUnavailableError,
  ComposerPersistenceVersionError,
  composerRecoveryUnavailable,
  openComposerLocalPersistenceDB,
  requestValue,
  transactionDone,
} from "./composer-local-persistence-db";
import {
  completeComposerScope,
  composerOrdinaryDraftStorageKey,
  composerPersistenceKeys,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";
import { randomUuid } from "./random-id";
import {
  composerChatActionSuffix,
  type ComposerChatAction,
  type PersistedComposerChatAction,
} from "./composer-chat-action";
import type { AttachmentRef } from "./types";
import { canonicalizeComposerAttachmentRefs } from "./composer-attachment-refs";
import { assertComposerTextDraftFormat, readComposerReferenceInput } from "./composer-draft-format";
import { inputDocumentDisplayText, inputDocumentFromText, inputDocumentTextContent, parseInputDocument, type InputDocument } from "./composer-input-document";
import { copyComposerReferenceSnapshot, type ComposerReferenceSnapshot } from "./composer-reference-transfer";

export { canonicalizeComposerAttachmentRef, canonicalizeComposerAttachmentRefs } from "./composer-attachment-refs";
export type ComposerDraftScope = ComposerPersistenceScope;

export class ComposerOrdinaryDraftInvalidError extends Error {
  readonly pendingActionPresent: boolean;

  constructor(
    message = "composer ordinary draft invalid",
    pendingActionPresent = false,
  ) {
    super(message);
    this.name = "ComposerOrdinaryDraftInvalidError";
    this.pendingActionPresent = pendingActionPresent;
  }
}

export interface ComposerOrdinaryDraft {
  message: string;
  attachments: AttachmentRef[];
  updatedAt: string;
  revision: string;
  pendingAction?: PersistedComposerChatAction;
}

export interface ComposerReferenceDraft extends ComposerOrdinaryDraft {
  schemaVersion: 2;
  input_document: InputDocument;
}

/** The input owner keeps a document even when its persisted draft is plain text. */
export interface ComposerInputDraft extends ComposerOrdinaryDraft {
  input_document: InputDocument;
  documentRevision?: ComposerDocumentRevision;
}

interface OrdinaryDraftRecord extends ComposerOrdinaryDraft {
  authSessionGenerationHash: string;
  schemaVersion?: unknown;
  input_document?: unknown;
  documentRevision?: ComposerDocumentRevision;
}

function ordinaryDraftTransaction(
  db: IDBDatabase,
  mode: IDBTransactionMode,
): IDBTransaction {
  try {
    return db.transaction([COMPOSER_ORDINARY_DRAFT_STORE], mode);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

function ordinaryDraftStore(transaction: IDBTransaction): IDBObjectStore {
  try {
    return transaction.objectStore(COMPOSER_ORDINARY_DRAFT_STORE);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

async function readOrdinaryDraftRecord(
  store: IDBObjectStore,
  key: IDBValidKey,
  authSessionGenerationHash: string,
  allowReferencePreview = false,
  allowDocumentRevision = false,
): Promise<OrdinaryDraftRecord | undefined> {
  try {
    const record = await requestValue(store.get(key) as IDBRequest<OrdinaryDraftRecord | undefined>);
    // Every read/mutation (including cleanup of the legacy fallback key) must
    // preserve a newer envelope. Its pending-action layout may also be unknown.
    if (record?.authSessionGenerationHash === authSessionGenerationHash) {
      if (allowReferencePreview) {
        if (record.schemaVersion === 2 || (allowDocumentRevision && record.schemaVersion === 3)) {
          const allowed = new Set([
            "schemaVersion", "input_document", "message", "attachments",
            "revision", "updatedAt", "authSessionGenerationHash", "pendingAction",
            ...(record.schemaVersion === 3 ? ["documentRevision"] : []),
          ]);
          if (
            Object.keys(record).some((key) => !allowed.has(key))
            || typeof record.revision !== "string"
            || !record.revision
            || record.revision.length > 256
          ) {
            throw new ComposerPersistenceVersionError("composer reference envelope is unsupported");
          }
        }
        readComposerReferenceInput(record, allowDocumentRevision);
      } else assertComposerTextDraftFormat(record);
    }
    return record;
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

function ordinaryDraftPut(
  store: IDBObjectStore,
  value: OrdinaryDraftRecord,
  key: IDBValidKey,
): void {
  try {
    store.put(value, key);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

function ordinaryDraftDelete(store: IDBObjectStore, key: IDBValidKey): void {
  try {
    store.delete(key);
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

export type ComposerOrdinaryDraftWriteOutcome =
  | { status: "written"; revision: string | null }
  | { status: "conflict"; revision: string | null };

export type ComposerPendingActionWriteOutcome =
  | { status: "written"; pendingAction: PersistedComposerChatAction }
  | {
      status: "conflict";
      pendingAction: PersistedComposerChatAction | null;
      draft: ComposerOrdinaryDraft | null;
    };

export type ComposerPendingActionSettlementOutcome =
  | { status: "settled"; draftDeleted: boolean }
  | { status: "not_owner"; draft: ComposerOrdinaryDraft | null };

function ordinaryDraftRevision(record: OrdinaryDraftRecord): string {
  return typeof record.revision === "string" && record.revision
    ? record.revision
    : `legacy:${record.updatedAt}`;
}

function persistedPendingAction(value: unknown): PersistedComposerChatAction | null {
  if (!value || typeof value !== "object") return null;
  const action = value as Record<string, unknown>;
  if (
    action.attempted !== true
    || typeof action.actionId !== "string"
    || typeof action.sourceRevision !== "string"
    || !action.sourceRevision
    || action.sourceRevision.length > 256
    || (action.actionKind !== "start" && action.actionKind !== "steer")
  ) return null;
  try {
    composerChatActionSuffix(action as unknown as ComposerChatAction);
  } catch {
    return null;
  }
  if (action.actionKind === "start") {
    if (
      Object.keys(action).filter((key) => key !== "agentDraftTarget").sort().join(",")
      !== "actionId,actionKind,attempted,sessionId,sourceRevision"
      || (Object.hasOwn(action, "agentDraftTarget") && !isComposerAgentDraftTarget(action.agentDraftTarget))
    ) return null;
    if (
      action.sessionId !== null
      && (
        typeof action.sessionId !== "string"
        || !action.sessionId
        || action.sessionId.length > 128
      )
    ) {
      return null;
    }
    return {
      ...(isComposerAgentDraftTarget(action.agentDraftTarget) ? { agentDraftTarget: { ...action.agentDraftTarget } } : {}),
      actionId: action.actionId,
      actionKind: "start",
      attempted: true,
      sourceRevision: action.sourceRevision,
      sessionId: action.sessionId,
    };
  }
  if (
    Object.keys(action).sort().join(",")
      !== "actionId,actionKind,attempted,expectedExecutionGeneration,expectedTurnId,sessionId,sourceRevision"
    ||
    typeof action.sessionId !== "string"
    || !action.sessionId
    || action.sessionId.length > 128
    || typeof action.expectedTurnId !== "string"
    || !action.expectedTurnId
    || action.expectedTurnId.length > 128
    || typeof action.expectedExecutionGeneration !== "number"
    || !Number.isSafeInteger(action.expectedExecutionGeneration)
    || action.expectedExecutionGeneration < 1
  ) return null;
  return {
    actionId: action.actionId,
    actionKind: "steer",
    attempted: true,
    sourceRevision: action.sourceRevision,
    sessionId: action.sessionId,
    expectedTurnId: action.expectedTurnId,
    expectedExecutionGeneration: action.expectedExecutionGeneration,
  };
}

function readPendingAction(
  record: OrdinaryDraftRecord,
  scope: ComposerDraftScope,
): PersistedComposerChatAction | undefined {
  if (record.pendingAction === undefined) return undefined;
  const action = persistedPendingAction(record.pendingAction);
  if (!action || action.sessionId !== scope.requestedSessionId) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer pending action invalid",
      true,
    );
  }
  return action;
}

function persistableAttachments(attachments: AttachmentRef[]): AttachmentRef[] {
  const canonical = canonicalizeComposerAttachmentRefs(attachments);
  if (!canonical) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer ordinary draft attachments invalid",
    );
  }
  return canonical;
}

function validatedOrdinaryDraft(
  record: OrdinaryDraftRecord,
  scope: ComposerDraftScope,
  pendingAction = readPendingAction(record, scope),
): ComposerOrdinaryDraft {
  const attachments = canonicalizeComposerAttachmentRefs(record.attachments);
  if (
    typeof record.message !== "string"
    || typeof record.updatedAt !== "string"
    || !attachments
  ) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer ordinary draft invalid",
      record.pendingAction !== undefined,
    );
  }
  return {
    message: record.message,
    attachments,
    updatedAt: record.updatedAt,
    revision: ordinaryDraftRevision(record),
    ...(pendingAction ? { pendingAction } : {}),
  };
}

function deleteReadOnlyLegacyDraftCopy(
  store: IDBObjectStore,
  manifestKey: string,
  legacyRecord: OrdinaryDraftRecord | undefined,
  authSessionGenerationHash: string,
): void {
  // The unpartitioned key is a read-only migration fallback: no released
  // ordinary-draft writer targets it. Once a same-generation CAS succeeds,
  // retaining that copy can only resurrect content after the v2 key is cleared.
  if (legacyRecord?.authSessionGenerationHash === authSessionGenerationHash) {
    ordinaryDraftDelete(store, manifestKey);
  }
}

export async function readComposerOrdinaryDraft(
  scope: ComposerDraftScope,
): Promise<ComposerOrdinaryDraft | null> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readonly");
    const store = ordinaryDraftStore(transaction);
    const [partitionedRecord, legacyRecord] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    let record = partitionedRecord;
    // Transparently read the pre-CAS key once. The next successful write moves
    // it to the auth-generation-partitioned v2 key in the same transaction.
    if (!record) {
      record = legacyRecord;
    }
    await transactionDone(transaction);
    if (!record || record.authSessionGenerationHash !== keys.authSessionGenerationHash) {
      return null;
    }
    return validatedOrdinaryDraft(record, scope);
  } finally {
    db.close();
  }
}

/** Read a versioned draft. Text writers and action mutations stay fenced. */
export async function readComposerReferenceDraft(
  scope: ComposerDraftScope,
): Promise<ComposerReferenceDraft | null> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(keys.manifestKey, keys.authSessionGenerationHash);
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readonly");
    const store = ordinaryDraftStore(transaction);
    const [canonical, legacy] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash, true),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash, true),
    ]);
    await transactionDone(transaction);
    const record = canonical ?? legacy;
    if (!record || record.authSessionGenerationHash !== keys.authSessionGenerationHash) return null;
    if (canonical && legacy?.authSessionGenerationHash === keys.authSessionGenerationHash && legacy.schemaVersion === 2) {
      throw new ComposerPersistenceVersionError("composer reference fallback requires reconciliation");
    }
    const input_document = readComposerReferenceInput(record);
    return input_document
      ? { ...validatedOrdinaryDraft(record, scope), schemaVersion: 2, input_document }
      : null;
  } finally {
    db.close();
  }
}

/** One consistent read of the current draft and its migration fallback. */
export async function readComposerInputDraft(inputScope: ComposerDraftScope): Promise<ComposerInputDraft | null> {
  const scope = { ...inputScope };
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(keys.manifestKey, keys.authSessionGenerationHash);
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readonly");
    const store = ordinaryDraftStore(transaction);
    const [canonical, legacy] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash, true, true),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash, true, true),
    ]);
    await transactionDone(transaction);
    const record = canonical ?? legacy;
    if (!record || record.authSessionGenerationHash !== keys.authSessionGenerationHash) return null;
    if (canonical && legacy?.authSessionGenerationHash === keys.authSessionGenerationHash && (legacy.schemaVersion === 2 || legacy.schemaVersion === 3)) {
      throw new ComposerPersistenceVersionError("composer reference fallback requires reconciliation");
    }
    if ((record.schemaVersion === 2 || record.schemaVersion === 3) && record.pendingAction !== undefined) {
      throw new ComposerPersistenceVersionError("reference Terminal actions require their matching input adapter");
    }
    const draft = validatedOrdinaryDraft(record, scope);
    return { ...draft, input_document: readComposerReferenceInput(record, true) ?? inputDocumentFromText(draft.message),
      ...(record.schemaVersion === 3 ? { documentRevision: copyComposerDocumentRevision(record.documentRevision, scope.requestedSessionId) } : {}),
    };
  } finally {
    db.close();
  }
}

/** Authoring may add or remove the last reference. Persist plain input as the
 * existing text envelope in the same CAS, never as a label-only conversion. */
export async function writeComposerInputDraft(
  scope: ComposerDraftScope,
  input: { input_document: InputDocument; attachments: AttachmentRef[]; documentRevision?: ComposerDocumentRevision },
  expectedRevision: string | null,
): Promise<ComposerOrdinaryDraftWriteOutcome> {
  if (!input || Object.keys(input).some((key) => !["input_document", "attachments", "documentRevision"].includes(key))) {
    throw new ComposerPersistenceVersionError("composer input envelope is unsupported");
  }
  const document = parseInputDocument(input.input_document);
  const snapshot = copyComposerReferenceSnapshot({
    schemaVersion: 2, input_document: document, message: inputDocumentDisplayText(document), attachments: input.attachments,
  });
  return mutateComposerReferenceDraft(scope, snapshot, expectedRevision, "input",
    input.documentRevision === undefined ? undefined : copyComposerDocumentRevision(input.documentRevision, scope.requestedSessionId));
}

/** Same acknowledgement/CAS ownership for either supported input format. */
export async function clearComposerInputDraft(scope: ComposerDraftScope, expectedRevision: string): Promise<boolean> {
  if (!expectedRevision) throw new ComposerOrdinaryDraftInvalidError();
  return (await mutateComposerReferenceDraft(scope, null, expectedRevision, "input")).status === "written";
}

/** Clear one server-accepted reference snapshot without erasing a newer edit. */
export async function clearAcceptedComposerReferenceDraft(
  scope: ComposerDraftScope,
  accepted: Pick<ComposerReferenceSnapshot, "input_document" | "attachments"> & {
    sourceDraftRevision?: string | null;
  },
): Promise<"absent" | "cleared" | "superseded" | "unbound"> {
  const expectedDocument = parseInputDocument(accepted.input_document);
  const expectedAttachments = canonicalizeComposerAttachmentRefs(accepted.attachments);
  if (!expectedAttachments) throw new ComposerOrdinaryDraftInvalidError();
  const current = await readComposerInputDraft(scope);
  if (!current) return "absent";
  if (!accepted.sourceDraftRevision) return "unbound";
  if (
    current.revision !== accepted.sourceDraftRevision
    || JSON.stringify(current.input_document) !== JSON.stringify(expectedDocument)
    || JSON.stringify(current.attachments) !== JSON.stringify(expectedAttachments)
  ) return "superseded";
  if (!await clearComposerInputDraft(scope, accepted.sourceDraftRevision)) {
    throw new ComposerRecoveryUnavailableError(
      "accepted reference draft cleanup did not settle",
    );
  }
  return "cleared";
}

/** Receipt recovery may clear only the exact original revision and target. */
export async function clearAcceptedComposerDocumentRevisionDraft(
  scope: ComposerDraftScope,
  accepted: { message: string; target: DocumentRevisionTarget; sourceDraftRevision: string },
): Promise<"absent" | "cleared" | "superseded" | "unbound"> {
  const current = await readComposerInputDraft(scope);
  if (!current) return "absent";
  if (!accepted.sourceDraftRevision) return "unbound";
  if (current.revision !== accepted.sourceDraftRevision || current.message !== accepted.message || current.attachments.length ||
      !sameDocumentRevisionTarget(current.documentRevision?.target, accepted.target)) return "superseded";
  if (!await clearComposerInputDraft(scope, accepted.sourceDraftRevision))
    throw new ComposerRecoveryUnavailableError("Accepted document revision cleanup did not settle");
  return "cleared";
}

/** Explicit removal, with the same scope/revision/action ownership as text CAS. */
export async function removeComposerDraftReferences(
  scope: ComposerDraftScope,
  expectedRevision: string,
): Promise<boolean> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(keys.manifestKey, keys.authSessionGenerationHash);
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    const [canonical, legacy] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash, true),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash, true),
    ]);
    const record = canonical ?? legacy;
    if (canonical && legacy?.authSessionGenerationHash === keys.authSessionGenerationHash && legacy.schemaVersion === 2) {
      throw new ComposerPersistenceVersionError("composer reference fallback requires reconciliation");
    }
    if (!record || record.authSessionGenerationHash !== keys.authSessionGenerationHash
      || ordinaryDraftRevision(record) !== expectedRevision) {
      await transactionDone(transaction);
      return false;
    }
    // A fallback can carry a different unresolved fence: do not clean it up.
    if (record.pendingAction !== undefined
      || (legacy?.authSessionGenerationHash === keys.authSessionGenerationHash
        && legacy.pendingAction !== undefined)) {
      await transactionDone(transaction);
      return false;
    }
    const input = readComposerReferenceInput(record);
    if (!input) {
      await transactionDone(transaction);
      return false;
    }
    const draft = validatedOrdinaryDraft(record, scope);
    const message = inputDocumentTextContent(input);
    if (message || draft.attachments.length) {
      ordinaryDraftPut(store, {
        message, attachments: draft.attachments,
        revision: randomUuid(), updatedAt: new Date().toISOString(),
        authSessionGenerationHash: keys.authSessionGenerationHash,
      }, storageKey);
    } else ordinaryDraftDelete(store, storageKey);
    deleteReadOnlyLegacyDraftCopy(store, keys.manifestKey, legacy, keys.authSessionGenerationHash);
    await transactionDone(transaction);
    return true;
  } finally {
    db.close();
  }
}

/** Store the full editor snapshot without converting references to display text.
 * Captures caller-owned values before the first await. Publication/readiness is
 * the caller's responsibility; persistence itself never grants execution.
 */
export async function writeComposerReferenceDraft(
  scope: ComposerDraftScope,
  draft: ComposerReferenceSnapshot,
  expectedRevision: string | null,
): Promise<ComposerOrdinaryDraftWriteOutcome> {
  if (
    !draft ||
    typeof draft !== "object" ||
    Array.isArray(draft) ||
    Object.keys(draft).some(
      (key) =>
        !["schemaVersion", "input_document", "message", "attachments"].includes(
          key,
        ),
    )
  ) {
    throw new ComposerPersistenceVersionError(
      "composer reference envelope is unsupported",
    );
  }
  const snapshot = copyComposerReferenceSnapshot(draft);
  return mutateComposerReferenceDraft(scope, snapshot, expectedRevision);
}

/** Clear only the exact version consumed by an acknowledged submission. */
export async function clearComposerReferenceDraft(
  scope: ComposerDraftScope,
  expectedRevision: string,
): Promise<boolean> {
  if (!expectedRevision) throw new ComposerOrdinaryDraftInvalidError();
  return (
    (await mutateComposerReferenceDraft(scope, null, expectedRevision))
      .status === "written"
  );
}

async function mutateComposerReferenceDraft(
  inputScope: ComposerDraftScope,
  snapshot: ComposerReferenceSnapshot | null,
  expectedRevision: string | null,
  format: "reference" | "input" = "reference",
  documentRevision?: ComposerDocumentRevision,
): Promise<ComposerOrdinaryDraftWriteOutcome> {
  const scope = { ...inputScope };
  if (
    !completeComposerScope(scope) ||
    (expectedRevision !== null &&
      (typeof expectedRevision !== "string" ||
        !expectedRevision ||
        expectedRevision.length > 256))
  ) {
    throw new ComposerOrdinaryDraftInvalidError();
  }
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  let transaction: IDBTransaction | null = null;
  let settled: Promise<void> | undefined;
  try {
    transaction = ordinaryDraftTransaction(db, "readwrite");
    settled = transactionDone(transaction);
    void settled.catch(() => {});
    const store = ordinaryDraftStore(transaction);
    const [canonical, legacy] = await Promise.all([
      readOrdinaryDraftRecord(
        store,
        storageKey,
        keys.authSessionGenerationHash,
        true,
        format === "input",
      ),
      readOrdinaryDraftRecord(
        store,
        keys.manifestKey,
        keys.authSessionGenerationHash,
        true,
        format === "input",
      ),
    ]);
    const ownedLegacy =
      legacy?.authSessionGenerationHash === keys.authSessionGenerationHash
        ? legacy
        : undefined;
    if (canonical && (ownedLegacy?.schemaVersion === 2 || ownedLegacy?.schemaVersion === 3)) {
      throw new ComposerPersistenceVersionError(
        "composer reference fallback requires reconciliation",
      );
    }
    const current = canonical ?? ownedLegacy;
    const revision = current ? ordinaryDraftRevision(current) : null;
    if (
      (current &&
        current.authSessionGenerationHash !== keys.authSessionGenerationHash) ||
      revision !== expectedRevision ||
      current?.pendingAction !== undefined ||
      ownedLegacy?.pendingAction !== undefined ||
      (!snapshot && format === "reference" && current?.schemaVersion !== 2)
    ) {
      await settled;
      return { status: "conflict", revision };
    }
    // A damaged existing envelope is recoverable data, not free space to reuse.
    if (current) validatedOrdinaryDraft(current, scope);
    const content = snapshot && documentRevision
      ? { ...snapshot, schemaVersion: 3, documentRevision }
      : snapshot && format === "input" && !snapshot.input_document.nodes.some((node) => node.type === "reference")
      ? { message: snapshot.message, attachments: snapshot.attachments }
      : snapshot;
    const keep = content && (content.message !== "" || content.attachments.length > 0 || format === "reference" || documentRevision !== undefined);
    const nextRevision = keep ? randomUuid() : null;
    if (keep) {
      ordinaryDraftPut(
        store,
        {
          ...content,
          revision: nextRevision!,
          updatedAt: new Date().toISOString(),
          authSessionGenerationHash: keys.authSessionGenerationHash,
        },
        storageKey,
      );
    } else ordinaryDraftDelete(store, storageKey);
    deleteReadOnlyLegacyDraftCopy(
      store,
      keys.manifestKey,
      ownedLegacy,
      keys.authSessionGenerationHash,
    );
    await settled;
    return { status: "written", revision: nextRevision };
  } catch (error) {
    // A synchronous failure after put (for example deleting the fallback) must
    // roll back the already queued write, not leave two competing envelopes.
    if (transaction) {
      try {
        transaction.abort();
      } catch {
        /* already settled */
      }
      await settled?.catch(() => {});
    }
    throw error;
  } finally {
    db.close();
  }
}

export async function writeComposerOrdinaryDraft(
  scope: ComposerDraftScope,
  draft: Pick<ComposerOrdinaryDraft, "message" | "attachments">,
  expectedRevision: string | null,
): Promise<ComposerOrdinaryDraftWriteOutcome> {
  assertComposerTextDraftFormat(draft);
  const attachments = persistableAttachments(draft.attachments);
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    let sourceKey = storageKey;
    const [partitionedRecord, legacyRecord] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    let current = partitionedRecord;
    if (!current) {
      if (legacyRecord?.authSessionGenerationHash === keys.authSessionGenerationHash) {
        current = legacyRecord;
        sourceKey = keys.manifestKey;
      }
    }
    const currentRevision = current ? ordinaryDraftRevision(current) : null;
    if (
      current?.authSessionGenerationHash !== undefined
      && current.authSessionGenerationHash !== keys.authSessionGenerationHash
    ) {
      await transactionDone(transaction);
      return { status: "conflict", revision: currentRevision };
    }
    if (currentRevision !== expectedRevision) {
      await transactionDone(transaction);
      return { status: "conflict", revision: currentRevision };
    }
    const pendingAction = current ? readPendingAction(current, scope) : undefined;
    if (pendingAction) {
      await transactionDone(transaction);
      return { status: "conflict", revision: currentRevision };
    }
    if (!draft.message && attachments.length === 0 && !pendingAction) {
      if (current) ordinaryDraftDelete(store, sourceKey);
      if (sourceKey !== storageKey) ordinaryDraftDelete(store, storageKey);
      deleteReadOnlyLegacyDraftCopy(
        store,
        keys.manifestKey,
        legacyRecord,
        keys.authSessionGenerationHash,
      );
      await transactionDone(transaction);
      return { status: "written", revision: null };
    } else {
      const revision = randomUuid();
      ordinaryDraftPut(
        store,
        {
          message: draft.message,
          attachments,
          updatedAt: new Date().toISOString(),
          revision,
          authSessionGenerationHash: keys.authSessionGenerationHash,
          ...(pendingAction ? { pendingAction } : {}),
        } satisfies OrdinaryDraftRecord,
        storageKey,
      );
      if (sourceKey !== storageKey) ordinaryDraftDelete(store, sourceKey);
      deleteReadOnlyLegacyDraftCopy(
        store,
        keys.manifestKey,
        legacyRecord,
        keys.authSessionGenerationHash,
      );
      await transactionDone(transaction);
      return { status: "written", revision };
    }
  } finally {
    db.close();
  }
}

export async function clearComposerOrdinaryDraft(
  scope: ComposerDraftScope,
  expectedRevision: string,
): Promise<boolean> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    let sourceKey = storageKey;
    const [partitionedRecord, legacyRecord] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    let record = partitionedRecord;
    if (!record) {
      if (legacyRecord?.authSessionGenerationHash === keys.authSessionGenerationHash) {
        record = legacyRecord;
        sourceKey = keys.manifestKey;
      }
    }
    if (
      !record
      || record.authSessionGenerationHash !== keys.authSessionGenerationHash
      || ordinaryDraftRevision(record) !== expectedRevision
      || record.pendingAction !== undefined
    ) {
      await transactionDone(transaction);
      return false;
    }
    ordinaryDraftDelete(store, sourceKey);
    deleteReadOnlyLegacyDraftCopy(
      store,
      keys.manifestKey,
      legacyRecord,
      keys.authSessionGenerationHash,
    );
    await transactionDone(transaction);
    return true;
  } finally {
    db.close();
  }
}

async function mutateComposerPendingAction(
  scope: ComposerDraftScope,
  action: ComposerChatAction,
  expectedRevision: string | null,
  expectedPendingActionId: string | null,
  draftSnapshot: Pick<ComposerOrdinaryDraft, "message" | "attachments"> | null,
): Promise<ComposerPendingActionWriteOutcome> {
  if (draftSnapshot) assertComposerTextDraftFormat(draftSnapshot);
  const canonicalDraftSnapshot = draftSnapshot
    ? {
        ...draftSnapshot,
        attachments: persistableAttachments(draftSnapshot.attachments),
      }
    : null;
  if (action.sourceRevision !== expectedRevision) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer pending action source revision mismatch",
    );
  }
  if (action.sessionId !== scope.requestedSessionId) {
    throw new ComposerOrdinaryDraftInvalidError(
      "composer pending action scope mismatch",
    );
  }
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    let sourceKey = storageKey;
    const [partitionedRecord, legacyRecord] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    let record = partitionedRecord;
    if (!record && legacyRecord?.authSessionGenerationHash === keys.authSessionGenerationHash) {
      record = legacyRecord;
      sourceKey = keys.manifestKey;
    }
    const revision = record ? ordinaryDraftRevision(record) : null;
    const currentAction = record ? readPendingAction(record, scope) ?? null : null;
    if (
      (!record && (!canonicalDraftSnapshot || expectedRevision !== null))
      || (
        record
        && record.authSessionGenerationHash !== keys.authSessionGenerationHash
      )
      || revision !== expectedRevision
      || (currentAction?.actionId ?? null) !== expectedPendingActionId
    ) {
      await transactionDone(transaction);
      return {
        status: "conflict",
        pendingAction: currentAction,
        draft: record?.authSessionGenerationHash === keys.authSessionGenerationHash
          ? validatedOrdinaryDraft(record, scope, currentAction ?? undefined)
          : null,
      };
    }
    // The marker write is itself a draft revision.  Older tabs still holding
    // the pre-marker revision will therefore lose their next CAS instead of
    // silently erasing the durable action fence.
    const markerRevision = randomUuid();
    const persistedAction = {
      ...action,
      sourceRevision: markerRevision,
    } as PersistedComposerChatAction;
    ordinaryDraftPut(
      store,
      {
        ...(record ?? {
          message: canonicalDraftSnapshot!.message,
          attachments: canonicalDraftSnapshot!.attachments,
          authSessionGenerationHash: keys.authSessionGenerationHash,
        }),
        ...(canonicalDraftSnapshot ? {
          message: canonicalDraftSnapshot.message,
          attachments: canonicalDraftSnapshot.attachments,
        } : {}),
        revision: markerRevision,
        updatedAt: new Date().toISOString(),
        pendingAction: persistedAction,
      },
      storageKey,
    );
    if (sourceKey !== storageKey) ordinaryDraftDelete(store, sourceKey);
    deleteReadOnlyLegacyDraftCopy(
      store,
      keys.manifestKey,
      legacyRecord,
      keys.authSessionGenerationHash,
    );
    await transactionDone(transaction);
    return { status: "written", pendingAction: persistedAction };
  } finally {
    db.close();
  }
}

/** First-winner CAS committed before a Terminal v1 mutation emits network bytes. */
export function prepareComposerPendingAction(
  scope: ComposerDraftScope,
  action: ComposerChatAction,
  expectedRevision: string | null,
  draftSnapshot: Pick<ComposerOrdinaryDraft, "message" | "attachments">,
): Promise<ComposerPendingActionWriteOutcome> {
  return mutateComposerPendingAction(
    scope,
    action,
    expectedRevision,
    null,
    draftSnapshot,
  );
}

/** Explicit user-authorized replacement after lookup could not confirm an old action. */
export function replaceComposerPendingAction(
  scope: ComposerDraftScope,
  action: ComposerChatAction,
  expectedRevision: string,
  expectedPendingActionId: string,
): Promise<ComposerPendingActionWriteOutcome> {
  return mutateComposerPendingAction(
    scope,
    action,
    expectedRevision,
    expectedPendingActionId,
    null,
  );
}

export async function settleComposerPendingAction(
  scope: ComposerDraftScope,
  actionId: string,
  disposition: "adopted" | "rejected",
): Promise<ComposerPendingActionSettlementOutcome> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    let sourceKey = storageKey;
    const [partitionedRecord, legacyRecord] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    let record = partitionedRecord;
    if (!record && legacyRecord?.authSessionGenerationHash === keys.authSessionGenerationHash) {
      record = legacyRecord;
      sourceKey = keys.manifestKey;
    }
    const pendingAction = record ? readPendingAction(record, scope) : undefined;
    if (
      !record
      || record.authSessionGenerationHash !== keys.authSessionGenerationHash
      || pendingAction?.actionId !== actionId
    ) {
      await transactionDone(transaction);
      return {
        status: "not_owner",
        draft: record?.authSessionGenerationHash === keys.authSessionGenerationHash
          ? validatedOrdinaryDraft(record, scope, pendingAction)
          : null,
      };
    }
    const draftDeleted = disposition === "adopted"
      && ordinaryDraftRevision(record) === pendingAction.sourceRevision;
    if (
      draftDeleted
    ) {
      ordinaryDraftDelete(store, sourceKey);
    } else {
      const { pendingAction: _pendingAction, ...preserved } = record;
      ordinaryDraftPut(store, preserved, storageKey);
      if (sourceKey !== storageKey) ordinaryDraftDelete(store, sourceKey);
    }
    deleteReadOnlyLegacyDraftCopy(
      store,
      keys.manifestKey,
      legacyRecord,
      keys.authSessionGenerationHash,
    );
    await transactionDone(transaction);
    return { status: "settled", draftDeleted };
  } finally {
    db.close();
  }
}

/** Explicit user-authorized escape hatch after a read failure. */
export async function discardComposerOrdinaryDraft(
  scope: ComposerDraftScope,
): Promise<void> {
  const keys = await composerPersistenceKeys(scope);
  const storageKey = composerOrdinaryDraftStorageKey(
    keys.manifestKey,
    keys.authSessionGenerationHash,
  );
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = ordinaryDraftTransaction(db, "readwrite");
    const store = ordinaryDraftStore(transaction);
    const [partitioned, legacy] = await Promise.all([
      readOrdinaryDraftRecord(store, storageKey, keys.authSessionGenerationHash),
      readOrdinaryDraftRecord(store, keys.manifestKey, keys.authSessionGenerationHash),
    ]);
    const partitionedOwned = partitioned?.authSessionGenerationHash
      === keys.authSessionGenerationHash;
    const legacyOwned = legacy?.authSessionGenerationHash
      === keys.authSessionGenerationHash;
    if (
      (partitionedOwned && partitioned.pendingAction !== undefined)
      || (legacyOwned && legacy.pendingAction !== undefined)
    ) {
      throw new ComposerOrdinaryDraftInvalidError(
        "composer pending action cannot be discarded",
        true,
      );
    }
    if (partitionedOwned) ordinaryDraftDelete(store, storageKey);
    if (legacyOwned) {
      ordinaryDraftDelete(store, keys.manifestKey);
    }
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}
