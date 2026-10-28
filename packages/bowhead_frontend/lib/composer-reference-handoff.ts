import {
  completeComposerScope,
  type ComposerPersistenceScope,
} from "./composer-persistence-scope";
import {
  ComposerPersistenceVersionError,
  composerRecoveryUnavailable,
  openComposerLocalPersistenceDB,
} from "./composer-local-persistence-db";
import {
  readComposerOrdinaryDraft,
  readComposerReferenceDraft,
  writeComposerReferenceDraft,
} from "./composer-ordinary-draft";
import {
  composerReferenceHandoffKey,
  copyComposerReferenceSnapshot,
  readComposerReferenceHandoffEnvelope,
  type ComposerReferenceSnapshot,
} from "./composer-reference-transfer";

function captureScope(
  value: ComposerPersistenceScope,
): ComposerPersistenceScope {
  const scope = { ...value };
  if (!completeComposerScope(scope))
    throw new ComposerPersistenceVersionError(
      "reference handoff scope invalid",
    );
  return scope;
}

/** A separate scoped channel. Never overwrites another pending handoff. */
export async function stageComposerReferenceHandoff(
  inputScope: ComposerPersistenceScope,
  input: ComposerReferenceSnapshot,
): Promise<{ status: "staged" | "conflict" }> {
  const scope = captureScope(inputScope);
  if (
    !input ||
    typeof input !== "object" ||
    Array.isArray(input) ||
    Object.keys(input).some(
      (key) =>
        !["schemaVersion", "input_document", "message", "attachments"].includes(
          key,
        ),
    )
  ) {
    throw new ComposerPersistenceVersionError(
      "reference handoff snapshot invalid",
    );
  }
  const raw = JSON.stringify(copyComposerReferenceSnapshot(input));
  if (raw.length > 1_000_000)
    throw new ComposerPersistenceVersionError("reference handoff too large");
  // Old v3 readers cannot see this channel. Complete the shared v4 barrier
  // before producing anything that those tabs could silently ignore.
  const db = await openComposerLocalPersistenceDB();
  db.close();
  const key = await composerReferenceHandoffKey(scope);
  try {
    const existing = sessionStorage.getItem(key);
    if (existing !== null && existing !== raw) return { status: "conflict" };
    if (existing === null) sessionStorage.setItem(key, raw);
    return { status: "staged" };
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}

export type ReferenceHandoffAcceptance =
  | { status: "missing" | "conflict" }
  | { status: "saved"; revision: string; acknowledged: boolean };

/** Commit to the ordinary draft before acknowledging the captured handoff.
 * Nonempty competing drafts and pending actions remain intact. A retry after
 * a lost acknowledgement may save the same complete source again under CAS.
 * Neither this operation nor staging submits a task or expands permissions.
 */
export async function acceptComposerReferenceHandoff(
  inputScope: ComposerPersistenceScope,
  shown?: ComposerReferenceSnapshot,
): Promise<ReferenceHandoffAcceptance> {
  const scope = captureScope(inputScope);
  const expected = shown ? copyComposerReferenceSnapshot(shown) : null;
  const key = await composerReferenceHandoffKey(scope);
  const envelope = await readComposerReferenceHandoffEnvelope(scope);
  if (!envelope) return { status: "missing" };
  if (expected && JSON.stringify(expected) !== JSON.stringify(envelope.snapshot)) return { status: "conflict" };
  const reference = await readComposerReferenceDraft(scope);
  const current = reference ?? (await readComposerOrdinaryDraft(scope));
  if (current?.pendingAction !== undefined) return { status: "conflict" };
  const same =
    reference &&
    JSON.stringify(copyComposerReferenceSnapshot(reference)) ===
      JSON.stringify(envelope.snapshot);
  if (
    current &&
    !same &&
    (current.message !== "" || current.attachments.length !== 0)
  )
    return { status: "conflict" };
  // Always use the original owner: it rechecks hidden legacy/pending fences as
  // well as the revision. An identical visible draft is not a bypass.
  const written = await writeComposerReferenceDraft(
    scope,
    envelope.snapshot,
    current?.revision ?? null,
  );
  if (written.status !== "written" || written.revision === null)
    return { status: "conflict" };
  let acknowledged = false;
  try {
    const latest = sessionStorage.getItem(key);
    if (latest === null) acknowledged = true;
    else if (latest === envelope.raw) {
      sessionStorage.removeItem(key);
      acknowledged = true;
    }
  } catch {
    /* Saved data remains authoritative; retry acknowledgement later. */
  }
  return { status: "saved", revision: written.revision, acknowledged };
}

/** Explicitly keep the existing draft, discarding only the handoff shown to the user. */
export async function discardComposerReferenceHandoff(
  inputScope: ComposerPersistenceScope,
  shown: ComposerReferenceSnapshot,
): Promise<boolean> {
  const scope = captureScope(inputScope);
  const snapshot = copyComposerReferenceSnapshot(shown);
  const key = await composerReferenceHandoffKey(scope);
  const current = await readComposerReferenceHandoffEnvelope(scope);
  if (!current) return true;
  if (JSON.stringify(current.snapshot) !== JSON.stringify(snapshot)) return false;
  try {
    if (sessionStorage.getItem(key) !== current.raw) return false;
    sessionStorage.removeItem(key);
    return true;
  } catch (error) {
    throw composerRecoveryUnavailable(error);
  }
}
