import { parseDocumentRevisionTarget, type DocumentRevisionTarget } from "./composer-document-revision";
import { parseContextMaintenanceAction, parseMaintenanceChatAction, type ContextMaintenanceAction } from "./context-maintenance";
import type { ComposerChatAction } from "./composer-chat-action";
import type { AttachmentRef } from "./types";
import { apiFetch, readApiError } from "./api-client";
import {
  completeComposerScope,
  composerPersistenceKeys,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";
import {
  COMPOSER_SUBMISSION_STORES,
  ComposerRecoveryUnavailableError,
  openComposerLocalPersistenceDB,
  requestValue,
  transactionDone,
} from "./composer-local-persistence-db";
import { randomUuid } from "./random-id";
import { sha256Hex } from "./sha256-fallback";
import { assertComposerTextDraftFormat, readComposerReferenceInput } from "./composer-draft-format";
import { inputDocumentFromText, validateInputDocumentTask, type InputDocument } from "./composer-input-document";
import { copyComposerReferenceSnapshot, type ComposerReferenceSnapshot } from "./composer-reference-transfer";
import { canonicalizeComposerAttachmentRefs } from "./composer-attachment-refs";
import { parseCoachCanvasBinding, type CoachCanvasBinding } from "./coach-canvas-target";
import type { WorkspaceSettingsCanvasBinding } from "./workspace-settings-canvas-bridge";

export { ComposerRecoveryUnavailableError } from "./composer-local-persistence-db";
export type SubmissionRecoveryScope = ComposerPersistenceScope;

const CHUNK_BYTES = 64 * 1024;

export interface ComposerSubmissionWireSnapshot {
  contextMaintenance?: import("./context-maintenance").ContextMaintenanceAction;
  composerSurface: "home" | "workbench";
  userProfile: Record<string, string> | null;
  preferredProvider: string | null;
  preferredModel: string | null;
  // Freeze only the two non-default values; `auto` is the backend default and stays off the wire (old submissions only had
  // "planning"/null, structurally compatible; recovery does not validate this field, only passes it through).
  planningMode: "planning" | "off" | null;
  draftChatBinding?: { draftId: string; version: number; dirty: boolean } | null;
  workspaceSettingsBinding?: WorkspaceSettingsCanvasBinding | null;
  coachCanvasBinding?: CoachCanvasBinding | null;
  /** Continue button: saved helper runs this message resumes (sent as client_info.continue_checkpoints). */
  continueCheckpointIds?: string[] | null;
  documentRevisionTarget?: DocumentRevisionTarget;
  /** Original input-owner revision, retained only for exact revision recovery. */
  sourceDraftRevision?: string;
}

export interface SubmissionRecoveryDraft {
  terminalAction?: ComposerChatAction;
  schemaVersion?: number;
  sourceDraftRevision?: string | null;
  message: string;
  attachments: AttachmentRef[];
  submissionKind?: "start" | "replay" | "steer";
  replaySourceTurnId?: string | null;
  expectedParentTurnId?: string | null;
  expectedExecutionGeneration?: number | null;
  wireSnapshot?: ComposerSubmissionWireSnapshot | null;
}

/** Initial references keep the full source plus the original send controls. */
export interface ReferenceSubmissionRecoveryDraft extends ComposerReferenceSnapshot {
  submissionKind?: "start" | "steer";
  expectedParentTurnId?: string | null;
  expectedExecutionGeneration?: number | null;
  /** Exact local draft revision accepted by this submission. */
  sourceDraftRevision?: string | null;
  wireSnapshot: ComposerSubmissionWireSnapshot;
}

export interface RecoveredSubmission extends SubmissionRecoveryDraft {
  clientSubmissionId: string;
  contentChecksum: string;
  createdAt: string;
}

export interface RecoveredReferenceSubmission extends RecoveredSubmission {
  schemaVersion: 2;
  input_document: InputDocument;
  sourceDraftRevision?: string | null;
}

export interface SubmissionAbandonOutcome {
  client_submission_id: string;
  outcome: "abandoned" | "started" | "buffered" | "cancelled" | "completed" | "rejected";
  response?: {
    session_id?: string;
    turn_id?: string;
    stream_url?: string;
    status?: string;
    execution_generation?: number;
    document_revision_target?: DocumentRevisionTarget;
  };
}

interface ManifestRecord {
  maintenanceIdentity?: { action: ComposerChatAction | null; context: ContextMaintenanceAction };
  manifestKey: string;
  identityKey: string;
  principalKey: string;
  authSessionGenerationHash: string;
  workspaceId: string;
  requestedSessionId: string | null;
  clientSubmissionId: string;
  contentChecksum: string;
  contentId: string;
  chunkCount: number;
  createdAt: string;
}

/** A durable submission fence was found but could not be reconciled safely. */
export class ComposerSubmissionFenceUnresolvedError extends Error {
  readonly clientSubmissionId: string;

  constructor(clientSubmissionId: string, cause: unknown) {
    super(
      cause instanceof Error
        ? cause.message
        : "composer submission fence reconciliation failed",
    );
    this.name = "ComposerSubmissionFenceUnresolvedError";
    this.clientSubmissionId = clientSubmissionId;
  }
}

function encodeDraft(draft: SubmissionRecoveryDraft): Uint8Array {
  assertComposerTextDraftFormat(draft);
  if (draft.wireSnapshot) assertComposerTextDraftFormat(draft.wireSnapshot);
  return new TextEncoder().encode(JSON.stringify({
    schemaVersion: 1,
    message: draft.message,
    attachments: draft.attachments,
    submissionKind: draft.submissionKind ?? "start",
    replaySourceTurnId: draft.replaySourceTurnId ?? null,
    expectedParentTurnId: draft.expectedParentTurnId ?? null,
    expectedExecutionGeneration: draft.expectedExecutionGeneration ?? null,
    wireSnapshot: draft.wireSnapshot ?? null,
  }));
}

export async function preparePendingSubmission(
  scope: SubmissionRecoveryScope,
  draft: SubmissionRecoveryDraft,
): Promise<{ clientSubmissionId: string; contentChecksum: string }> {
  const capturedScope = { ...scope };
  if (draft.wireSnapshot?.contextMaintenance) {
    throw new ComposerRecoveryUnavailableError("Maintenance requires its independent journal");
  }
  const revision = draft.wireSnapshot && Object.hasOwn(draft.wireSnapshot, "documentRevisionTarget")
    ? captureDocumentRevisionSubmission(capturedScope, draft) : null;
  return prepareEncodedPendingSubmission(capturedScope, revision
    ? new TextEncoder().encode(JSON.stringify(revision)) : encodeDraft(draft));
}

export interface PendingContextMaintenance {
  journalId: string;
  action: ComposerChatAction | null;
  context: ContextMaintenanceAction;
  draft: RecoveredSubmission | null;
}

export async function prepareContextMaintenanceJournal(scope: SubmissionRecoveryScope, draft: SubmissionRecoveryDraft): Promise<PendingContextMaintenance> {
  const captured = captureContextMaintenanceSubmission(scope, draft);
  const identity = { action: captured.terminalAction ?? null, context: captured.wireSnapshot!.contextMaintenance! };
  await prepareEncodedPendingSubmission(scope, new TextEncoder().encode(JSON.stringify(captured)), identity);
  const pending = await readContextMaintenanceJournal(scope);
  if (!pending?.draft) throw new ComposerRecoveryUnavailableError("Maintenance request could not be restored");
  return pending;
}

export async function readContextMaintenanceJournal(scope: SubmissionRecoveryScope): Promise<PendingContextMaintenance | null> {
  if (!completeComposerScope(scope) || !scope.requestedSessionId) return null;
  const keys = await composerPersistenceKeys(scope);
  const db = await openComposerLocalPersistenceDB();
  let manifest: ManifestRecord | undefined;
  try {
    const transaction = db.transaction(["manifests"], "readonly");
    manifest = await requestValue(transaction.objectStore("manifests").get(`${keys.manifestKey}:context-maintenance.v1`) as IDBRequest<ManifestRecord | undefined>);
    await transactionDone(transaction);
  } finally { db.close(); }
  if (!manifest) return null;
  const context = parseContextMaintenanceAction(manifest.maintenanceIdentity?.context);
  const action = manifest.maintenanceIdentity?.action === null ? null : parseMaintenanceChatAction(manifest.maintenanceIdentity?.action, scope.requestedSessionId);
  if (!context || (manifest.maintenanceIdentity?.action !== null && !action)
    || manifest.manifestKey !== `${keys.manifestKey}:context-maintenance.v1`
    || manifest.principalKey !== keys.principalKey || manifest.identityKey !== keys.identityKey
    || manifest.workspaceId !== scope.workspaceId || manifest.requestedSessionId !== scope.requestedSessionId
    || typeof manifest.clientSubmissionId !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(manifest.clientSubmissionId))
    throw new ComposerRecoveryUnavailableError("Maintenance journal identity is invalid");
  let draft: RecoveredSubmission | null = null;
  if (manifest.authSessionGenerationHash === keys.authSessionGenerationHash) {
    try {
      const restored = await readPendingSubmissionRecord(scope, false, true);
      if (restored?.schemaVersion === 4 && restored.contentChecksum === manifest.contentChecksum
        && JSON.stringify(restored.terminalAction ?? null) === JSON.stringify(action)
        && JSON.stringify(restored.wireSnapshot?.contextMaintenance) === JSON.stringify(context)) draft = restored;
    } catch { /* Retain the exact original identity for lookup after payload loss. */ }
  }
  return { journalId: manifest.clientSubmissionId, context, action, draft };
}

export async function clearContextMaintenanceJournal(scope: SubmissionRecoveryScope, journalId: string): Promise<void> {
  await clearPendingSubmission(scope, journalId, true);
}

export function captureContextMaintenanceSubmission(scope: SubmissionRecoveryScope, draft: SubmissionRecoveryDraft): SubmissionRecoveryDraft & { schemaVersion: 4 } {
  const wire = draft.wireSnapshot;
  const action = parseContextMaintenanceAction(wire?.contextMaintenance);
  if (!completeComposerScope(scope) || !scope.requestedSessionId || !action || !wire
    || Object.keys(draft).some((key) => !["schemaVersion", "message", "attachments", "submissionKind", "replaySourceTurnId", "expectedParentTurnId", "expectedExecutionGeneration", "wireSnapshot", "terminalAction"].includes(key))
    || (draft.schemaVersion !== undefined && draft.schemaVersion !== 4)
    || Object.keys(wire).some((key) => !["contextMaintenance", "composerSurface", "userProfile", "preferredProvider", "preferredModel", "planningMode", "draftChatBinding", "workspaceSettingsBinding", "coachCanvasBinding", "continueCheckpointIds"].includes(key))
    || draft.message !== "" || !Array.isArray(draft.attachments) || draft.attachments.length || draft.replaySourceTurnId || draft.expectedParentTurnId || draft.expectedExecutionGeneration != null
    || (draft.submissionKind && draft.submissionKind !== "start") || wire.planningMode === "planning" || wire.documentRevisionTarget
    || wire.draftChatBinding || wire.workspaceSettingsBinding || wire.coachCanvasBinding || wire.continueCheckpointIds?.length)
    throw new ComposerRecoveryUnavailableError("Context maintenance requires its original idle conversation");
  const terminalAction = draft.terminalAction === undefined ? undefined : parseMaintenanceChatAction(draft.terminalAction, scope.requestedSessionId);
  if (terminalAction === null) throw new ComposerRecoveryUnavailableError("Invalid maintenance action identity");
  return structuredClone({ schemaVersion: 4, message: "", attachments: [], submissionKind: "start", wireSnapshot: { ...wire, contextMaintenance: action }, ...(terminalAction ? { terminalAction } : {}) });
}

/** One immutable revision envelope; neither a text nor reference reader may strip its target. */
export function captureDocumentRevisionSubmission(scope: SubmissionRecoveryScope, draft: SubmissionRecoveryDraft): SubmissionRecoveryDraft & { schemaVersion: 3; sourceDraftRevision: string; wireSnapshot: ComposerSubmissionWireSnapshot } {
  const wire = draft.wireSnapshot;
  if (!completeComposerScope(scope) || !scope.requestedSessionId || !wire ||
      Object.keys(draft).some((key) => !["schemaVersion", "message", "attachments", "submissionKind", "replaySourceTurnId", "expectedParentTurnId", "expectedExecutionGeneration", "sourceDraftRevision", "wireSnapshot"].includes(key)) ||
      (draft.schemaVersion !== undefined && draft.schemaVersion !== 3) ||
      typeof draft.message !== "string" || !draft.message.trim() || !Array.isArray(draft.attachments) || draft.attachments.length ||
      (draft.submissionKind !== undefined && draft.submissionKind !== "start") || draft.replaySourceTurnId ||
      draft.expectedParentTurnId || draft.expectedExecutionGeneration != null ||
      wire.planningMode === "planning" || wire.draftChatBinding != null || wire.workspaceSettingsBinding != null || wire.coachCanvasBinding != null)
    throw new ComposerRecoveryUnavailableError("Invalid document revision submission");
  let target: DocumentRevisionTarget;
  try { target = parseDocumentRevisionTarget(wire.documentRevisionTarget); }
  catch { throw new ComposerRecoveryUnavailableError("Invalid document revision target"); }
  const revision = wire.sourceDraftRevision;
  if (target.session_id !== scope.requestedSessionId || typeof revision !== "string" || !revision || revision.length > 256 ||
      (draft.sourceDraftRevision !== undefined && draft.sourceDraftRevision !== revision))
    throw new ComposerRecoveryUnavailableError("Document revision source mismatch");
  const { documentRevisionTarget: _target, sourceDraftRevision: _revision, workspaceSettingsBinding: _settings, coachCanvasBinding: _coach, ...baseWire } = wire;
  const ordinary = captureReferenceSubmissionDraft(scope, { schemaVersion: 2, input_document: inputDocumentFromText(draft.message), message: draft.message,
    attachments: [], sourceDraftRevision: revision, wireSnapshot: baseWire });
  const snapshot = { schemaVersion: 3 as const, message: ordinary.message, attachments: [], submissionKind: "start" as const,
    replaySourceTurnId: null, expectedParentTurnId: null, expectedExecutionGeneration: null, sourceDraftRevision: revision,
    wireSnapshot: { ...ordinary.wireSnapshot, documentRevisionTarget: target, sourceDraftRevision: revision } };
  if (new TextEncoder().encode(JSON.stringify(snapshot)).byteLength > 1024 * 1024)
    throw new ComposerRecoveryUnavailableError("Document revision submission is too large");
  return snapshot;
}

/** Detach the complete source and original controls before any asynchronous work. */
export function captureReferenceSubmissionDraft(
  inputScope: SubmissionRecoveryScope,
  draft: ReferenceSubmissionRecoveryDraft,
): ReferenceSubmissionRecoveryDraft {
  const scope = { ...inputScope };
  if (
    !completeComposerScope(scope) ||
    !draft ||
    typeof draft !== "object" ||
    Array.isArray(draft) ||
    Object.keys(draft).some(
      (key) =>
        ![
          "schemaVersion",
          "input_document",
          "message",
          "attachments",
          "submissionKind",
          "expectedParentTurnId",
          "expectedExecutionGeneration",
          "sourceDraftRevision",
          "wireSnapshot",
        ].includes(key),
    ) ||
    (draft.submissionKind !== undefined && !["start", "steer"].includes(draft.submissionKind))
  ) {
    throw new ComposerRecoveryUnavailableError(
      "composer reference submission invalid",
    );
  }
  const snapshot = copyComposerReferenceSnapshot(draft);
  const submissionKind = draft.submissionKind ?? "start";
  const expectedParentTurnId = draft.expectedParentTurnId ?? null;
  const expectedExecutionGeneration = draft.expectedExecutionGeneration ?? null;
  const sourceDraftRevision = draft.sourceDraftRevision ?? null;
  if (
    (submissionKind === "start" && (expectedParentTurnId !== null || expectedExecutionGeneration !== null))
    || (submissionKind === "steer" && (
      scope.requestedSessionId === null
      || typeof expectedParentTurnId !== "string"
      || expectedParentTurnId.length === 0
      || !Number.isSafeInteger(expectedExecutionGeneration)
      || (expectedExecutionGeneration ?? 0) <= 0
    ))
    || (sourceDraftRevision !== null && (
      typeof sourceDraftRevision !== "string"
      || sourceDraftRevision.length === 0
      || sourceDraftRevision.length > 256
    ))
  ) {
    throw new ComposerRecoveryUnavailableError(
      "composer reference submission target invalid",
    );
  }
  validateInputDocumentTask(
    snapshot.input_document,
    snapshot.attachments.length > 0,
  );
  const rawWire = structuredClone(draft.wireSnapshot);
  if (rawWire && typeof rawWire === "object") {
    const record = rawWire as unknown as Record<string, unknown>;
    // Recovery stores a second source projection with the controls. It must
    // agree with the root before removing that storage-only envelope.
    if (
      Object.hasOwn(record, "input_document") ||
      Object.hasOwn(record, "schemaVersion")
    ) {
      const source = readComposerReferenceInput({
        ...record,
        message: snapshot.message,
      });
      if (JSON.stringify(source) !== JSON.stringify(snapshot.input_document))
        throw new ComposerRecoveryUnavailableError(
          "composer reference wire source changed",
        );
      delete record.input_document;
      delete record.schemaVersion;
    }
  }
  const wire = rawWire;
  const wireFields = [
    "composerSurface",
    "userProfile",
    "preferredProvider",
    "preferredModel",
    "planningMode",
    "draftChatBinding",
  ];
  if (
    !wire ||
    typeof wire !== "object" ||
    Array.isArray(wire) ||
    Object.keys(wire).some((key) => !wireFields.includes(key)) ||
    wireFields.filter((key) => key !== "draftChatBinding").some((key) => !Object.hasOwn(wire, key)) ||
    wire.composerSurface !==
      (scope.requestedSessionId === null ? "home" : "workbench") ||
    [wire.preferredProvider, wire.preferredModel].some(
      (value) => value !== null && typeof value !== "string",
    ) ||
    ![null, "planning", "off"].includes(wire.planningMode) ||
    (wire.draftChatBinding !== undefined && wire.draftChatBinding !== null && (
      typeof wire.draftChatBinding !== "object" ||
      Array.isArray(wire.draftChatBinding) ||
      Object.keys(wire.draftChatBinding).some((key) => !["draftId", "version", "dirty"].includes(key)) ||
      typeof wire.draftChatBinding.draftId !== "string" ||
      !/^sad_[A-Za-z0-9_]{1,76}$/.test(wire.draftChatBinding.draftId) ||
      !Number.isSafeInteger(wire.draftChatBinding.version) ||
      wire.draftChatBinding.version < 1 ||
      wire.draftChatBinding.version > 999_999_999 ||
      typeof wire.draftChatBinding.dirty !== "boolean"
    )) ||
    (wire.userProfile !== null &&
      (typeof wire.userProfile !== "object" ||
        Array.isArray(wire.userProfile) ||
        ![Object.prototype, null].includes(
          Object.getPrototypeOf(wire.userProfile),
        ) ||
        Object.entries(wire.userProfile).some(
          ([key, value]) =>
            !["display_name", "language", "answer_style"].includes(key) ||
            typeof value !== "string",
        )))
  ) {
    throw new ComposerRecoveryUnavailableError(
      "composer reference wire invalid",
    );
  }
  return {
    ...snapshot,
    submissionKind,
    expectedParentTurnId,
    expectedExecutionGeneration,
    sourceDraftRevision,
    wireSnapshot: wire,
  };
}

/** Save before any HTTP send. No submission or execution is started here. */
export async function preparePendingReferenceSubmission(
  inputScope: SubmissionRecoveryScope,
  draft: ReferenceSubmissionRecoveryDraft,
): Promise<{ clientSubmissionId: string; contentChecksum: string }> {
  const scope = { ...inputScope };
  const {
    wireSnapshot: wire,
    submissionKind,
    expectedParentTurnId,
    expectedExecutionGeneration,
    sourceDraftRevision,
    ...snapshot
  } = captureReferenceSubmissionDraft(scope, draft);
  // Both projections use one captured document; the caller cannot replace it
  // with a second document hidden in the wire snapshot.
  const content = new TextEncoder().encode(
    JSON.stringify({
      ...snapshot,
      submissionKind,
      replaySourceTurnId: null,
      expectedParentTurnId,
      expectedExecutionGeneration,
      ...(sourceDraftRevision ? { sourceDraftRevision } : {}),
      wireSnapshot: {
        ...wire,
        schemaVersion: 2,
        input_document: snapshot.input_document,
      },
    }),
  );
  if (content.byteLength > 1024 * 1024)
    throw new ComposerRecoveryUnavailableError(
      "composer reference submission too large",
    );
  const prepared = await prepareEncodedPendingSubmission(scope, content);
  // Reusing an identity is only useful if its original recovery bytes remain
  // readable. A damaged pending record keeps its ID for lookup, never a new ID.
  try {
    const stored = await readPendingReferenceSubmission(scope);
    if (
      !stored ||
      stored.clientSubmissionId !== prepared.clientSubmissionId ||
      stored.contentChecksum !== prepared.contentChecksum
    ) {
      throw new ComposerRecoveryUnavailableError(
        "composer reference submission changed",
      );
    }
  } catch (error) {
    throw new ComposerSubmissionFenceUnresolvedError(
      prepared.clientSubmissionId,
      error,
    );
  }
  return prepared;
}

async function prepareEncodedPendingSubmission(
  scope: SubmissionRecoveryScope,
  content: Uint8Array,
  maintenanceIdentity?: ManifestRecord["maintenanceIdentity"],
): Promise<{ clientSubmissionId: string; contentChecksum: string }> {
  const keys = await composerPersistenceKeys(scope);
  if (maintenanceIdentity) keys.manifestKey += ":context-maintenance.v1";
  const contentChecksum = await sha256Hex(content);
  const db = await openComposerLocalPersistenceDB();
  let transaction: IDBTransaction | null = null;
  let settled: Promise<void> | undefined;
  try {
    transaction = db.transaction([...COMPOSER_SUBMISSION_STORES], "readwrite");
    settled = transactionDone(transaction);
    void settled.catch(() => {});
    const manifests = transaction.objectStore("manifests");
    const existing = await requestValue(
      manifests.get(keys.manifestKey) as IDBRequest<ManifestRecord | undefined>,
    );
    if (existing) {
      if (existing.authSessionGenerationHash !== keys.authSessionGenerationHash) {
        transaction.abort();
        throw new ComposerRecoveryUnavailableError("stale auth session requires reconciliation");
      }
      if (existing.contentChecksum !== contentChecksum) {
        transaction.abort();
        throw new ComposerRecoveryUnavailableError("unresolved submission blocks a different draft");
      }
      await settled;
      return {
        clientSubmissionId: existing.clientSubmissionId,
        contentChecksum,
      };
    }

    const clientSubmissionId = randomUuid();
    const contentId = randomUuid();
    const createdAt = new Date().toISOString();
    const chunkCount = Math.max(1, Math.ceil(content.byteLength / CHUNK_BYTES));
    const manifest: ManifestRecord = {
      ...(maintenanceIdentity ? { maintenanceIdentity } : {}),
      manifestKey: keys.manifestKey,
      identityKey: keys.identityKey,
      principalKey: keys.principalKey,
      authSessionGenerationHash: keys.authSessionGenerationHash,
      workspaceId: scope.workspaceId,
      requestedSessionId: scope.requestedSessionId,
      clientSubmissionId,
      contentChecksum,
      contentId,
      chunkCount,
      createdAt,
    };
    transaction.objectStore("identities").put({
      identityKey: keys.identityKey,
      currentAuthSessionGenerationHash: keys.authSessionGenerationHash,
    }, keys.identityKey);
    transaction.objectStore("principals").put({
      principalKey: keys.principalKey,
      identityKey: keys.identityKey,
      authSessionGenerationHash: keys.authSessionGenerationHash,
    }, keys.principalKey);
    if (!maintenanceIdentity?.action) transaction.objectStore("submission_fences").put({
      identityKey: keys.identityKey,
      principalUserId: scope.userId,
      teamId: scope.teamId,
      workspaceId: scope.workspaceId,
      requestedSessionId: scope.requestedSessionId,
      clientSubmissionId,
      state: "unresolved",
      createdAt,
    }, `${keys.identityKey}:${clientSubmissionId}`);
    manifests.put(manifest, keys.manifestKey);
    transaction.objectStore("draft_blobs").put({
      manifestKey: keys.manifestKey,
      contentId,
      chunkCount,
      contentChecksum,
      encodedBytes: content.byteLength,
    }, keys.manifestKey);
    const metaStore = transaction.objectStore("content_chunk_meta");
    const payloadStore = transaction.objectStore("content_chunk_payload");
    for (let index = 0; index < chunkCount; index += 1) {
      const chunk = content.slice(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES);
      const chunkKey = `${contentId}:${String(index).padStart(6, "0")}`;
      metaStore.put({ contentId, index, byteLength: chunk.byteLength }, chunkKey);
      payloadStore.put(chunk.buffer, chunkKey);
    }
    await settled;
    return { clientSubmissionId, contentChecksum };
  } catch (error) {
    if (transaction) {
      try { transaction.abort(); } catch { /* already settled */ }
      await settled?.catch(() => {});
    }
    if (error instanceof ComposerRecoveryUnavailableError) throw error;
    throw new ComposerRecoveryUnavailableError(
      error instanceof Error ? error.message : String(error),
    );
  } finally {
    db.close();
  }
}

export async function readPendingSubmission(
  scope: SubmissionRecoveryScope,
): Promise<RecoveredSubmission | null> {
  return readPendingSubmissionRecord(scope, false);
}

/** Inspection only: never returns a reference envelope to a legacy resend owner. */
export async function readPendingReferenceSubmission(
  scope: SubmissionRecoveryScope,
): Promise<RecoveredReferenceSubmission | null> {
  const record = await readPendingSubmissionRecord(scope, true);
  return record && "input_document" in record ? record : null;
}

async function readPendingSubmissionRecord(
  scope: SubmissionRecoveryScope,
  referencePreview: boolean,
  maintenance = false,
): Promise<RecoveredSubmission | RecoveredReferenceSubmission | null> {
  const keys = await composerPersistenceKeys(scope);
  if (maintenance) keys.manifestKey += ":context-maintenance.v1";
  const db = await openComposerLocalPersistenceDB();
  let pendingSubmissionId: string | null = null;
  try {
    const transaction = db.transaction(
      ["manifests", "content_chunk_payload"],
      "readonly",
    );
    const manifest = await requestValue(
      transaction.objectStore("manifests").get(keys.manifestKey) as IDBRequest<ManifestRecord | undefined>,
    );
    if (!manifest) {
      await transactionDone(transaction);
      return null;
    }
    pendingSubmissionId = manifest.clientSubmissionId;
    // Never read an old login generation's payload.
    if (manifest.authSessionGenerationHash !== keys.authSessionGenerationHash) {
      transaction.abort();
      return null;
    }
    const chunks: Uint8Array[] = [];
    for (let index = 0; index < manifest.chunkCount; index += 1) {
      const chunkKey = `${manifest.contentId}:${String(index).padStart(6, "0")}`;
      const payload = await requestValue(
        transaction.objectStore("content_chunk_payload").get(chunkKey) as IDBRequest<ArrayBuffer | undefined>,
      );
      if (!payload) throw new ComposerRecoveryUnavailableError("composer recovery chunk missing");
      chunks.push(new Uint8Array(payload));
    }
    await transactionDone(transaction);
    const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0);
    const content = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      content.set(chunk, offset);
      offset += chunk.byteLength;
    }
    if (await sha256Hex(content) !== manifest.contentChecksum) {
      throw new ComposerRecoveryUnavailableError("composer recovery checksum mismatch");
    }
    const decoded = JSON.parse(new TextDecoder().decode(content)) as {
      schemaVersion: number;
      message: string;
      attachments: AttachmentRef[];
      submissionKind?: "start" | "replay" | "steer";
      replaySourceTurnId?: string | null;
      expectedParentTurnId?: string | null;
      expectedExecutionGeneration?: number | null;
      sourceDraftRevision?: string | null;
      wireSnapshot?: ComposerSubmissionWireSnapshot | null;
    };
    if (decoded.schemaVersion === 4) {
      if (!maintenance) throw new ComposerRecoveryUnavailableError("Maintenance is not a Composer submission");
      const captured = captureContextMaintenanceSubmission(scope, decoded);
      return { ...captured, clientSubmissionId: manifest.clientSubmissionId, contentChecksum: manifest.contentChecksum, createdAt: manifest.createdAt };
    }
    if (decoded.schemaVersion === 3) {
      if (Object.keys(decoded).some((key) => !["schemaVersion", "message", "attachments", "submissionKind", "replaySourceTurnId", "expectedParentTurnId", "expectedExecutionGeneration", "sourceDraftRevision", "wireSnapshot"].includes(key)))
        throw new ComposerRecoveryUnavailableError("Invalid document revision recovery envelope");
      const captured = captureDocumentRevisionSubmission(scope, decoded as SubmissionRecoveryDraft);
      return { ...captured, clientSubmissionId: manifest.clientSubmissionId, contentChecksum: manifest.contentChecksum, createdAt: manifest.createdAt };
    }
    if (
      (decoded.schemaVersion !== 1 && !(referencePreview && decoded.schemaVersion === 2))
      || typeof decoded.message !== "string"
      || !Array.isArray(decoded.attachments)
    ) {
      throw new ComposerRecoveryUnavailableError("composer recovery payload invalid");
    }
    const document = referencePreview ? readComposerReferenceInput(decoded) : null;
    if (!document) assertComposerTextDraftFormat(decoded);
    const sourceDraftRevision = document
      ? decoded.sourceDraftRevision ?? null
      : null;
    if (sourceDraftRevision !== null && (
      typeof sourceDraftRevision !== "string"
      || sourceDraftRevision.length === 0
      || sourceDraftRevision.length > 256
    )) {
      throw new ComposerRecoveryUnavailableError(
        "composer reference source revision invalid",
      );
    }
    // A wire snapshot that carries a document must agree with the frozen root;
    // it cannot silently replace the source or reset parameters during recovery.
    if (decoded.wireSnapshot) {
      if (decoded.wireSnapshot.coachCanvasBinding && !parseCoachCanvasBinding(decoded.wireSnapshot.coachCanvasBinding))
        throw new ComposerRecoveryUnavailableError("Original resource target is invalid");
      const snapshot = decoded.wireSnapshot as unknown as Record<string, unknown>;
      if (document && Object.hasOwn(snapshot, "input_document")) {
        const wireDocument = readComposerReferenceInput({ ...snapshot, message: decoded.message });
        if (JSON.stringify(wireDocument) !== JSON.stringify(document)) {
          throw new ComposerRecoveryUnavailableError("composer recovery document mismatch");
        }
      } else assertComposerTextDraftFormat(snapshot);
    }
    const attachments = document ? canonicalizeComposerAttachmentRefs(decoded.attachments) : decoded.attachments;
    if (!attachments) throw new ComposerRecoveryUnavailableError("composer recovery attachments invalid");
    return {
      clientSubmissionId: manifest.clientSubmissionId,
      contentChecksum: manifest.contentChecksum,
      createdAt: manifest.createdAt,
      message: decoded.message,
      attachments,
      ...(document ? {
        schemaVersion: 2 as const,
        input_document: document,
        sourceDraftRevision,
      } : {}),
      submissionKind: decoded.submissionKind ?? "start",
      replaySourceTurnId: decoded.replaySourceTurnId ?? null,
      expectedParentTurnId: decoded.expectedParentTurnId ?? null,
      expectedExecutionGeneration: decoded.expectedExecutionGeneration ?? null,
      wireSnapshot: decoded.wireSnapshot ?? null,
    };
  } catch (error) {
    if (pendingSubmissionId) {
      throw new ComposerSubmissionFenceUnresolvedError(
        pendingSubmissionId,
        error,
      );
    }
    throw error;
  } finally {
    db.close();
  }
}

/**
 * Reconcile an unresolved fence left by an older login generation without
 * reading its draft payload. The server is authoritative: a completed receipt
 * is returned as-is, while an absent/inflight submission is durably abandoned.
 */
export async function reconcileStaleSubmissionFence(
  scope: SubmissionRecoveryScope,
): Promise<SubmissionAbandonOutcome | null> {
  const keys = await composerPersistenceKeys(scope);
  const db = await openComposerLocalPersistenceDB();
  let staleSubmissionId: string | null = null;
  try {
    const transaction = db.transaction(["manifests"], "readonly");
    const manifest = await requestValue(
      transaction.objectStore("manifests").get(keys.manifestKey) as IDBRequest<ManifestRecord | undefined>,
    );
    if (
      manifest
      && manifest.authSessionGenerationHash !== keys.authSessionGenerationHash
    ) {
      staleSubmissionId = manifest.clientSubmissionId;
    }
    await transactionDone(transaction);
  } catch (error) {
    if (staleSubmissionId) {
      throw new ComposerSubmissionFenceUnresolvedError(
        staleSubmissionId,
        error,
      );
    }
    throw error;
  } finally {
    db.close();
  }
  if (!staleSubmissionId) return null;
  let outcome: SubmissionAbandonOutcome;
  try {
    outcome = await abandonPendingSubmission(scope, staleSubmissionId);
  } catch (error) {
    throw new ComposerSubmissionFenceUnresolvedError(
      staleSubmissionId,
      error,
    );
  }
  if (outcome.outcome === "abandoned" || outcome.outcome === "rejected") {
    await clearPendingSubmission(scope, staleSubmissionId);
  }
  return outcome;
}

export async function clearPendingSubmission(
  scope: SubmissionRecoveryScope,
  clientSubmissionId: string,
  maintenance = false,
): Promise<void> {
  const keys = await composerPersistenceKeys(scope);
  if (maintenance) keys.manifestKey += ":context-maintenance.v1";
  const db = await openComposerLocalPersistenceDB();
  try {
    const transaction = db.transaction([...COMPOSER_SUBMISSION_STORES], "readwrite");
    const manifests = transaction.objectStore("manifests");
    const manifest = await requestValue(
      manifests.get(keys.manifestKey) as IDBRequest<ManifestRecord | undefined>,
    );
    if (!manifest || manifest.clientSubmissionId !== clientSubmissionId) {
      await transactionDone(transaction);
      return;
    }
    manifests.delete(keys.manifestKey);
    transaction.objectStore("draft_blobs").delete(keys.manifestKey);
    transaction.objectStore("submission_fences").delete(
      `${keys.identityKey}:${clientSubmissionId}`,
    );
    for (let index = 0; index < manifest.chunkCount; index += 1) {
      const chunkKey = `${manifest.contentId}:${String(index).padStart(6, "0")}`;
      transaction.objectStore("content_chunk_meta").delete(chunkKey);
      transaction.objectStore("content_chunk_payload").delete(chunkKey);
    }
    await transactionDone(transaction);
  } finally {
    db.close();
  }
}

export async function abandonPendingSubmission(
  scope: SubmissionRecoveryScope,
  clientSubmissionId: string,
): Promise<SubmissionAbandonOutcome> {
  const response = await apiFetch(
    `/api/v1/chat/submissions/${encodeURIComponent(clientSubmissionId)}/abandon`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        workspace_id: scope.workspaceId,
        requested_session_id: scope.requestedSessionId,
      }),
    },
  );
  if (!response.ok) throw await readApiError(response);
  const outcome = await response.json() as SubmissionAbandonOutcome;
  if (
    outcome.client_submission_id !== clientSubmissionId
    || ![
      "abandoned",
      "started",
      "buffered",
      "cancelled",
      "completed",
      "rejected",
    ].includes(outcome.outcome)
  ) {
    throw new ComposerRecoveryUnavailableError(
      "composer submission abandon response invalid",
    );
  }
  return outcome;
}
