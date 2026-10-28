import { expect, test, type Page } from "@playwright/test";
import { installComposerStorageModules, STORAGE_MODULE_PATH } from "./_composer-storage-modules";

const scope = { userId: "u_revision", teamId: "team_revision", workspaceId: "ws_revision", requestedSessionId: "s_review", authSessionGeneration: "login-original" };
const target = { kind: "document_revision" as const, version: 1 as const, session_id: "s_review", job_id: "djob_original", draft_digest: "a".repeat(64) };
async function load(page: Page) {
  await installComposerStorageModules(page);
  await page.goto("/__composer_storage_test");
}

test("revision drafts retain exact targets through reload, empty edits and strict CAS", async ({ page }) => {
  await load(page);
  const first = await page.evaluate(async ({ prefix, scope, target }) => {
    const store = await import(`${prefix}/composer-ordinary-draft`);
    const input = { input_document: { schema_version: 1, nodes: [{ type: "text", text: "Change the title" }] }, attachments: [], documentRevision: { handoffId: "handoff-original", target } };
    const pending = store.writeComposerInputDraft(scope, input, null);
    input.documentRevision.target.job_id = "djob_mutated";
    input.input_document.nodes[0].text = "Mutated after capture";
    const result = await pending;
    const read = await store.readComposerInputDraft(scope);
    const textReader = await store.readComposerOrdinaryDraft(scope).then(() => "read", (error: Error) => error.name);
    const stale = await store.writeComposerInputDraft(scope, input, null);
    return { result, read, textReader, stale };
  }, { prefix: STORAGE_MODULE_PATH, scope, target });
  expect(first.read.message).toBe("Change the title");
  expect(first.read.documentRevision.target).toEqual(target);
  expect(first.textReader).toBe("ComposerPersistenceVersionError");
  expect(first.stale.status).toBe("conflict");
  await page.reload();
  const after = await page.evaluate(async ({ prefix, scope, revision }) => {
    const store = await import(`${prefix}/composer-ordinary-draft`);
    const read = await store.readComposerInputDraft(scope);
    const saved = await store.writeComposerInputDraft(scope, { input_document: { schema_version: 1, nodes: [] }, attachments: [], documentRevision: read.documentRevision }, revision);
    const empty = await store.readComposerInputDraft(scope);
    const wrongScope = await store.readComposerInputDraft({ ...scope, requestedSessionId: "s_other" });
    const otherLogin = await store.readComposerInputDraft({ ...scope, authSessionGeneration: "new-login" });
    const oldClear = await store.clearComposerInputDraft(scope, revision);
    const cleared = await store.clearComposerInputDraft(scope, saved.revision);
    return { read, empty, wrongScope, otherLogin, oldClear, cleared };
  }, { prefix: STORAGE_MODULE_PATH, scope, revision: first.result.revision });
  expect(after.empty.message).toBe("");
  expect(after.empty.documentRevision.target).toEqual(target);
  expect(after.wrongScope).toBeNull();
  expect(after.otherLogin).toBeNull();
  expect(after.oldClear).toBe(false);
  expect(after.cleared).toBe(true);
});

test("revision submission recovery freezes target and clears only its accepted input revision", async ({ page }) => {
  await load(page);
  const result = await page.evaluate(async ({ prefix, scope, target }) => {
    const drafts = await import(`${prefix}/composer-ordinary-draft`);
    const recovery = await import(`${prefix}/composer-submission-recovery`);
    const input = { input_document: { schema_version: 1, nodes: [{ type: "text", text: "Shorten the title" }] }, attachments: [], documentRevision: { handoffId: "handoff-recovery", target } };
    const saved = await drafts.writeComposerInputDraft(scope, input, null);
    const wireSnapshot = { composerSurface: "workbench", userProfile: null, preferredProvider: "original", preferredModel: "model-original", planningMode: "off", documentRevisionTarget: target, sourceDraftRevision: saved.revision };
    const pending = recovery.preparePendingSubmission(scope, { message: "Shorten the title", attachments: [], wireSnapshot });
    wireSnapshot.documentRevisionTarget.draft_digest = "b".repeat(64);
    const receipt = await pending;
    const restored = await recovery.readPendingSubmission(scope);
    const revised = await drafts.writeComposerInputDraft(scope, { ...input, input_document: { schema_version: 1, nodes: [{ type: "text", text: "My newer edit" }] } }, saved.revision);
    const cleanup = await drafts.clearAcceptedComposerDocumentRevisionDraft(scope, { message: restored.message, target: restored.wireSnapshot.documentRevisionTarget, sourceDraftRevision: restored.sourceDraftRevision });
    const kept = await drafts.readComposerInputDraft(scope);
    const secondSend = await recovery.preparePendingSubmission(scope, { message: "Changed request", attachments: [], wireSnapshot: { ...restored.wireSnapshot, sourceDraftRevision: revised.revision } }).then(() => "accepted", (error: Error) => error.name);
    const foreign = await recovery.readPendingSubmission({ ...scope, authSessionGeneration: "new-login" });
    return { receipt, restored, cleanup, kept, secondSend, foreign };
  }, { prefix: STORAGE_MODULE_PATH, scope, target });
  expect(result.restored.schemaVersion).toBe(3);
  expect(result.restored.wireSnapshot.documentRevisionTarget).toEqual(target);
  expect(result.restored.clientSubmissionId).toBe(result.receipt.clientSubmissionId);
  expect(result.cleanup).toBe("superseded");
  expect(result.kept.message).toBe("My newer edit");
  expect(result.secondSend).toBe("ComposerRecoveryUnavailableError");
  expect(result.foreign).toBeNull();
});

test("upgrade preserves existing records and refuses an old version-four client", async ({ page }) => {
  await load(page);
  const result = await page.evaluate(async ({ prefix, scope, target }) => {
    const keysModule = await import(`${prefix}/composer-persistence-scope`);
    const oldScope = { ...scope, requestedSessionId: "s_legacy" };
    const keys = await keysModule.composerPersistenceKeys(oldScope);
    const key = keysModule.composerOrdinaryDraftStorageKey(keys.manifestKey, keys.authSessionGenerationHash);
    await new Promise<void>((resolve, reject) => {
      const opening = indexedDB.open("bowhead-composer-recovery-v1", 4);
      opening.onupgradeneeded = () => opening.result.createObjectStore("ordinary_drafts");
      opening.onerror = () => reject(opening.error);
      opening.onsuccess = () => {
        const db = opening.result;
        const tx = db.transaction("ordinary_drafts", "readwrite");
        tx.objectStore("ordinary_drafts").put({ message: "Legacy preserved", attachments: [], revision: "legacy-revision", updatedAt: new Date().toISOString(), authSessionGenerationHash: keys.authSessionGenerationHash }, key);
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onerror = () => reject(tx.error);
      };
    });
    const store = await import(`${prefix}/composer-ordinary-draft`);
    const result = await store.writeComposerInputDraft(scope, { input_document: { schema_version: 1, nodes: [{ type: "text", text: "Kept" }] }, attachments: [], documentRevision: { handoffId: "upgrade", target } }, null);
    const old = await new Promise<string>((resolve) => {
      const request = indexedDB.open("bowhead-composer-recovery-v1", 4);
      request.onerror = () => resolve(request.error!.name);
      request.onsuccess = () => { request.result.close(); resolve("opened"); };
    });
    return { old, read: await store.readComposerInputDraft(scope), revision: result.revision, legacy: await store.readComposerOrdinaryDraft(oldScope) };
  }, { prefix: STORAGE_MODULE_PATH, scope, target });
  expect(result.old).toBe("VersionError");
  expect(result.read.revision).toBe(result.revision);
  expect(result.read.documentRevision.target).toEqual(target);
  expect(result.legacy).toMatchObject({ message: "Legacy preserved", revision: "legacy-revision" });
});
