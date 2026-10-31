import { expect, test, type Page } from "@playwright/test";
import { installComposerStorageModules, STORAGE_MODULE_PATH } from "./_composer-storage-modules";
import { seedReferenceDraft } from "./_composer-reference-fixtures";

const scope = { userId: "u_e2e", teamId: "team_test", workspaceId: "ws_test", requestedSessionId: "s_context", authSessionGeneration: "auth-generation-e2e-1" };
const action = { actionId: `act_start_${"a".repeat(32)}`, actionKind: "start", attempted: true, sourceRevision: null, sessionId: scope.requestedSessionId };
const draft = { message: "", attachments: [], terminalAction: action, wireSnapshot: {
  composerSurface: "workbench", userProfile: null, preferredProvider: "provider", preferredModel: "model", planningMode: "off",
  contextMaintenance: { contract: "context-maintenance.v1", expected_generation: `mcg_${"b".repeat(32)}` },
} };

async function prepare(page: Page, override = draft) {
  return page.evaluate(async ({ prefix, scope, draft }) => {
    const recovery = await import(`${prefix}/composer-submission-recovery`);
    return recovery.prepareContextMaintenanceJournal(scope, draft);
  }, { prefix: STORAGE_MODULE_PATH, scope, draft: override });
}

test.beforeEach(async ({ page }) => {
  await installComposerStorageModules(page);
  await page.goto("/__composer_storage_test");
});

for (const kind of ["ordinary", "reference"] as const) {
  test(`maintenance leaves ${kind} editable drafts and pending submissions intact`, async ({ page }) => {
    if (kind === "reference") await seedReferenceDraft(page, { source: "ordinary", sessionId: scope.requestedSessionId });
    const original = await page.evaluate(async ({ prefix, scope, kind }) => {
      const ordinary = await import(`${prefix}/composer-ordinary-draft`);
      const recovery = await import(`${prefix}/composer-submission-recovery`);
      if (kind === "ordinary") await ordinary.writeComposerOrdinaryDraft(scope, { message: "My unsent request", attachments: [] }, null);
      const editable = await ordinary.readComposerInputDraft(scope);
      const submission = await recovery.preparePendingSubmission(scope, { message: "Another unresolved request", attachments: [] });
      return { editable, submission };
    }, { prefix: STORAGE_MODULE_PATH, scope, kind });
    const pending = await prepare(page);
    await page.reload();
    const observed = await page.evaluate(async ({ prefix, scope, id }) => {
      const ordinary = await import(`${prefix}/composer-ordinary-draft`);
      const recovery = await import(`${prefix}/composer-submission-recovery`);
      const restored = await recovery.readContextMaintenanceJournal(scope);
      await recovery.clearContextMaintenanceJournal(scope, "00000000-0000-4000-8000-000000000000");
      const retained = await recovery.readContextMaintenanceJournal(scope);
      await recovery.clearContextMaintenanceJournal(scope, id);
      return { restored, retained, maintenance: await recovery.readContextMaintenanceJournal(scope),
        editable: await ordinary.readComposerInputDraft(scope), submission: await recovery.readPendingSubmission(scope) };
    }, { prefix: STORAGE_MODULE_PATH, scope, id: pending.journalId });
    expect(observed.restored).toEqual(pending);
    expect(observed.retained).toEqual(pending);
    expect(observed.maintenance).toBeNull();
    expect(observed.editable).toEqual(original.editable);
    expect(observed.submission.clientSubmissionId).toEqual(original.submission.clientSubmissionId);
    expect(observed.submission.message).toBe("Another unresolved request");
  });
}

test("two tabs retain the same original action and cannot replace it with another request", async ({ page, context }) => {
  const other = await context.newPage();
  await installComposerStorageModules(other);
  await other.goto("/__composer_storage_test");
  const [first, second] = await Promise.all([prepare(page), prepare(other)]);
  expect(first.journalId).toEqual(second.journalId);
  const changed = { ...draft, terminalAction: { ...action, actionId: `act_start_${"c".repeat(32)}` } };
  await expect(prepare(other, changed)).rejects.toThrow("unresolved submission blocks a different draft");
  expect(await prepare(page)).toEqual(first);
});

for (const damage of ["missing_chunk", "stale_auth"] as const) {
  test(`${damage} preserves only original identity and never enters legacy abandonment`, async ({ page }) => {
    const pending = await prepare(page);
    const restored = await page.evaluate(async ({ prefix, scope, damage }) => {
      const recovery = await import(`${prefix}/composer-submission-recovery`);
      const storage = await import(`${prefix}/composer-local-persistence-db`);
      if (damage === "missing_chunk") {
        const db = await storage.openComposerLocalPersistenceDB();
        const transaction = db.transaction(["content_chunk_payload"], "readwrite");
        transaction.objectStore("content_chunk_payload").clear();
        await storage.transactionDone(transaction);
        db.close();
      }
      const selected = damage === "stale_auth" ? { ...scope, authSessionGeneration: "auth-new" } : scope;
      return { pending: await recovery.readContextMaintenanceJournal(selected),
        ordinary: await recovery.readPendingSubmission(selected), reference: await recovery.readPendingReferenceSubmission(selected),
        legacy: await recovery.reconcileStaleSubmissionFence(selected) };
    }, { prefix: STORAGE_MODULE_PATH, scope, damage });
    expect(restored.pending).toEqual({ ...pending, draft: null });
    expect(restored.ordinary).toBeNull();
    expect(restored.reference).toBeNull();
    expect(restored.legacy).toBeNull();
  });
}
