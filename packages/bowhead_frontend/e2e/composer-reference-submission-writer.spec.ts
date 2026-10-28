import { expect, test, type Page } from "@playwright/test";
import type {
  ReferenceSubmissionRecoveryDraft,
  SubmissionRecoveryScope,
} from "../lib/composer-submission-recovery";
import { referenceDraftInput } from "./_composer-reference-fixtures";
import {
  installComposerStorageModules,
  STORAGE_MODULE_PATH,
} from "./_composer-storage-modules";

const scope: SubmissionRecoveryScope = {
  userId: "u_ref_pending",
  teamId: "team_ref",
  workspaceId: "ws_ref",
  requestedSessionId: null,
  authSessionGeneration: "auth-ref",
};
const draft = {
  ...referenceDraftInput,
  wireSnapshot: {
    composerSurface: "home",
    userProfile: { display_name: "分析师", language: "zh-CN" },
    preferredProvider: "provider_original",
    preferredModel: "model_original",
    planningMode: "off",
  },
};
declare global {
  interface Window {
    referencePendingStore: {
      recovery: typeof import("../lib/composer-submission-recovery");
      ordinary: typeof import("../lib/composer-ordinary-draft");
      persistence: typeof import("../lib/composer-persistence-scope");
      storage: typeof import("../lib/composer-local-persistence-db");
      scope: SubmissionRecoveryScope;
      draft: ReferenceSubmissionRecoveryDraft;
    };
  }
}
async function load(page: Page) {
  await installComposerStorageModules(page);
  await page.goto("/__composer_storage_test");
  await page.evaluate(
    async ({ prefix, scope, draft }) => {
      window.referencePendingStore = {
        recovery: await import(`${prefix}/composer-submission-recovery`),
        ordinary: await import(`${prefix}/composer-ordinary-draft`),
        persistence: await import(`${prefix}/composer-persistence-scope`),
        storage: await import(`${prefix}/composer-local-persistence-db`),
        scope,
        draft: draft as ReferenceSubmissionRecoveryDraft,
      };
    },
    { prefix: STORAGE_MODULE_PATH, scope, draft },
  );
}
async function allRecords(page: Page) {
  return page.evaluate(async () => {
    const { storage } = window.referencePendingStore;
    const db = await storage.openComposerLocalPersistenceDB();
    try {
      const names = Array.from(db.objectStoreNames).sort();
      const tx = db.transaction(names, "readonly");
      const values = await Promise.all(
        names.map(async (name) => {
          const store = tx.objectStore(name);
          return [
            name,
            await storage.requestValue(store.getAllKeys()),
            await storage.requestValue(store.getAll()),
          ];
        }),
      );
      await storage.transactionDone(tx);
      return values;
    } finally {
      db.close();
    }
  });
}

test("reference submission persists the original source and controls across reload and reuses one identity", async ({
  page,
}) => {
  await load(page);
  const first = await page.evaluate(async () => {
    const { recovery, ordinary, scope, draft } = window.referencePendingStore;
    const { wireSnapshot: _, ...snapshot } = draft;
    await ordinary.writeComposerReferenceDraft(scope, snapshot, null);
    const original = structuredClone(draft),
      originalScope = { ...scope };
    const pending = recovery.preparePendingReferenceSubmission(scope, draft);
    scope.workspaceId = "ws_after_await";
    draft.input_document.nodes.splice(0);
    draft.attachments[0].filename = "changed.pdf";
    draft.wireSnapshot.preferredModel = "model_after_await";
    draft.wireSnapshot.userProfile!.display_name = "after await";
    const prepared = await pending;
    const second = await recovery.preparePendingReferenceSubmission(
      originalScope,
      original,
    );
    let textFenced = false;
    try {
      await recovery.readPendingSubmission(originalScope);
    } catch {
      textFenced = true;
    }
    return {
      prepared,
      second,
      original,
      textFenced,
      ordinary: await ordinary.readComposerReferenceDraft(originalScope),
      read: await recovery.readPendingReferenceSubmission(originalScope),
    };
  });
  expect(first.prepared).toEqual(first.second);
  expect(first.textFenced).toBe(true);
  expect(first.read).toMatchObject({
    ...first.original,
    clientSubmissionId: first.prepared.clientSubmissionId,
  });
  expect(first.read?.wireSnapshot).toMatchObject({
    schemaVersion: 2,
    input_document: first.original.input_document,
  });
  expect(first.ordinary).toMatchObject(referenceDraftInput);
  await load(page);
  const restored = await page.evaluate(() => {
    const { recovery, scope } = window.referencePendingStore;
    return recovery.readPendingReferenceSubmission(scope);
  });
  expect(restored).toEqual(first.read);
});

test("reference steer recovery preserves its exact parent fence across reload", async ({
  page,
}) => {
  await load(page);
  const stored = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    const steerScope = { ...scope, requestedSessionId: "s_parent" };
    const steerDraft = {
      ...structuredClone(draft),
      submissionKind: "steer" as const,
      expectedParentTurnId: "t_parent",
      expectedExecutionGeneration: 7,
      sourceDraftRevision: "rev_steer_source",
      wireSnapshot: {
        ...structuredClone(draft.wireSnapshot),
        composerSurface: "workbench" as const,
      },
    };
    const prepared = await recovery.preparePendingReferenceSubmission(
      steerScope,
      steerDraft,
    );
    return {
      prepared,
      read: await recovery.readPendingReferenceSubmission(steerScope),
    };
  });
  expect(stored.read).toMatchObject({
    clientSubmissionId: stored.prepared.clientSubmissionId,
    submissionKind: "steer",
    expectedParentTurnId: "t_parent",
    expectedExecutionGeneration: 7,
    sourceDraftRevision: "rev_steer_source",
    input_document: referenceDraftInput.input_document,
  });
  await load(page);
  const restored = await page.evaluate(() => {
    const { recovery, scope } = window.referencePendingStore;
    return recovery.readPendingReferenceSubmission({
      ...scope,
      requestedSessionId: "s_parent",
    });
  });
  expect(restored).toEqual(stored.read);
});

test("accepted reference cleanup removes only its exact ordinary draft", async ({
  page,
  context,
}) => {
  const other = await context.newPage();
  await Promise.all([load(page), load(other)]);
  const written = await page.evaluate(async () => {
    const { ordinary, scope, draft } = window.referencePendingStore;
    const { wireSnapshot: _, ...snapshot } = draft;
    return ordinary.writeComposerReferenceDraft(scope, snapshot, null);
  });
  expect(written.status).toBe("written");

  const replaced = await other.evaluate(async (revision) => {
    const { ordinary, scope, draft } = window.referencePendingStore;
    const { wireSnapshot: _, ...snapshot } = structuredClone(draft);
    const outcome = await ordinary.writeComposerInputDraft(
      scope,
      {
        input_document: snapshot.input_document,
        attachments: snapshot.attachments,
      },
      revision,
    );
    return {
      outcome,
      current: await ordinary.readComposerReferenceDraft(scope),
    };
  }, written.revision);
  expect(replaced.outcome.status).toBe("written");
  expect(replaced.outcome.revision).not.toBe(written.revision);

  const preserved = await page.evaluate(async (sourceDraftRevision) => {
    const { ordinary, scope, draft } = window.referencePendingStore;
    return {
      result: await ordinary.clearAcceptedComposerReferenceDraft(scope, {
        ...draft,
        sourceDraftRevision,
      }),
      current: await ordinary.readComposerReferenceDraft(scope),
    };
  }, written.revision);
  expect(preserved.result).toBe("superseded");
  expect(preserved.current?.input_document).toEqual(
    replaced.current?.input_document,
  );

  const cleared = await page.evaluate(async () => {
    const { ordinary, scope } = window.referencePendingStore;
    const current = await ordinary.readComposerReferenceDraft(scope);
    if (!current) throw new Error("reference draft missing");
    return {
      result: await ordinary.clearAcceptedComposerReferenceDraft(scope, {
        ...current,
        sourceDraftRevision: current.revision,
      }),
      current: await ordinary.readComposerReferenceDraft(scope),
    };
  });
  expect(cleared).toEqual({ result: "cleared", current: null });

  const legacy = await page.evaluate(async () => {
    const { ordinary, scope, draft } = window.referencePendingStore;
    const { wireSnapshot: _, ...snapshot } = draft;
    await ordinary.writeComposerReferenceDraft(scope, snapshot, null);
    const result = await ordinary.clearAcceptedComposerReferenceDraft(
      scope,
      draft,
    );
    return {
      result,
      current: await ordinary.readComposerReferenceDraft(scope),
    };
  });
  expect(legacy.result).toBe("unbound");
  expect(legacy.current?.input_document).toEqual(
    referenceDraftInput.input_document,
  );
});

test("a large full document spans actual chunks and corruption retains its original lookup identity", async ({
  page,
}) => {
  await load(page);
  const first = await page.evaluate(async () => {
    const { recovery, scope, draft, storage, persistence } =
      window.referencePendingStore;
    const prefix = "/__composer_storage_modules";
    const codec = await import(`${prefix}/composer-input-document`);
    draft.input_document.nodes.push({
      type: "text",
      text: "数据".repeat(22000),
    });
    draft.message = codec.inputDocumentDisplayText(draft.input_document);
    const prepared = await recovery.preparePendingReferenceSubmission(
      scope,
      draft,
    );
    const read = await recovery.readPendingReferenceSubmission(scope);
    const keys = await persistence.composerPersistenceKeys(scope);
    const db = await storage.openComposerLocalPersistenceDB();
    const tx = db.transaction(
      ["manifests", "content_chunk_payload"],
      "readwrite",
    );
    const manifest = await storage.requestValue(
      tx.objectStore("manifests").get(keys.manifestKey),
    );
    tx.objectStore("content_chunk_payload").delete(
      `${manifest.contentId}:000001`,
    );
    await storage.transactionDone(tx);
    db.close();
    return {
      prepared,
      matchesOriginal:
        JSON.stringify(read?.input_document) ===
        JSON.stringify(codec.parseInputDocument(draft.input_document)),
      chunkCount: manifest.chunkCount,
    };
  });
  expect(first.chunkCount).toBeGreaterThan(2);
  expect(first.matchesOriginal).toBe(true);
  const before = await allRecords(page);
  const failure = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    try {
      await recovery.preparePendingReferenceSubmission(scope, draft);
      return null;
    } catch (error) {
      return {
        name: (error as Error).name,
        id: (error as { clientSubmissionId?: string }).clientSubmissionId,
      };
    }
  });
  expect(failure).toEqual({
    name: "ComposerSubmissionFenceUnresolvedError",
    id: first.prepared.clientSubmissionId,
  });
  expect(await allRecords(page)).toEqual(before);
});

for (const change of [
  "parameters",
  "resource",
  "controls",
  "attachments",
  "auth",
] as const) {
  test(`a pending reference prevents a different ${change} from replacing its identity`, async ({
    page,
  }) => {
    await load(page);
    await page.evaluate(async () => {
      const { recovery, scope, draft } = window.referencePendingStore;
      await recovery.preparePendingReferenceSubmission(scope, draft);
    });
    const before = await allRecords(page);
    const refused = await page.evaluate(async (change) => {
      const { recovery, scope, draft } = window.referencePendingStore;
      const node = draft.input_document.nodes.find(
        (node) => node.type === "reference" && node.resource.kind === "skill",
      )!;
      if (node.type !== "reference")
        throw new Error("fixture requires exact Skill");
      if (change === "parameters") node.parameters = { changed: true };
      if (change === "resource" && node.resource.kind === "skill")
        node.resource.version = `sha256:${"b".repeat(64)}`;
      if (change === "controls")
        draft.wireSnapshot.preferredModel = "another_model";
      if (change === "attachments")
        draft.attachments[0].filename = "another.pdf";
      if (change === "auth") scope.authSessionGeneration = "another_login";
      try {
        await recovery.preparePendingReferenceSubmission(scope, draft);
        return false;
      } catch {
        return true;
      }
    }, change);
    expect(refused).toBe(true);
    expect(await allRecords(page)).toEqual(before);
  });
}

test("two real tabs store one pending identity and stale cleanup cannot erase its replacement", async ({
  page,
  context,
}) => {
  const other = await context.newPage();
  await Promise.all([load(page), load(other)]);
  const prepare = (target: Page) =>
    target.evaluate(() => {
      const { recovery, scope, draft } = window.referencePendingStore;
      return recovery.preparePendingReferenceSubmission(scope, draft);
    });
  const both = await Promise.all([prepare(page), prepare(other)]);
  expect(both[0]).toEqual(both[1]);
  const result = await page.evaluate(async (oldId) => {
    const { recovery, scope, draft } = window.referencePendingStore;
    await recovery.clearPendingSubmission(scope, oldId);
    const replacement = await recovery.preparePendingReferenceSubmission(
      scope,
      draft,
    );
    await recovery.clearPendingSubmission(scope, oldId);
    return {
      replacement,
      read: await recovery.readPendingReferenceSubmission(scope),
    };
  }, both[0].clientSubmissionId);
  expect(result.replacement.clientSubmissionId).not.toBe(
    both[0].clientSubmissionId,
  );
  expect(result.read?.clientSubmissionId).toBe(
    result.replacement.clientSubmissionId,
  );
});

test("a real payload write failure rolls back all submission stores and leaves the ordinary draft intact", async ({
  page,
}) => {
  await load(page);
  await page.evaluate(async () => {
    const { ordinary, scope, draft } = window.referencePendingStore;
    const { wireSnapshot: _, ...snapshot } = draft;
    await ordinary.writeComposerReferenceDraft(scope, snapshot, null);
  });
  const before = await allRecords(page);
  const failed = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (...args) {
      if (this.name === "content_chunk_payload")
        throw new DOMException("Quota exceeded", "QuotaExceededError");
      return put.apply(this, args);
    };
    try {
      await recovery.preparePendingReferenceSubmission(scope, draft);
      return false;
    } catch {
      return true;
    } finally {
      IDBObjectStore.prototype.put = put;
    }
  });
  expect(failed).toBe(true);
  expect(await allRecords(page)).toEqual(before);
  const success = await page.evaluate(() => {
    const { recovery, scope, draft } = window.referencePendingStore;
    return recovery.preparePendingReferenceSubmission(scope, draft);
  });
  expect(success.clientSubmissionId).toMatch(/^[0-9a-f-]{36}$/);
});

test("invalid reference wire and unsupported entry shapes cannot create a pending fence", async ({
  page,
}) => {
  await load(page);
  const before = await allRecords(page);
  const rejected = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    const values = [
      { ...draft, submissionKind: "steer" },
      { ...draft, sourceDraftRevision: "" },
      { ...draft, schemaVersion: 99 },
      { ...draft, message: "different display" },
      {
        ...draft,
        wireSnapshot: { ...draft.wireSnapshot, composerSurface: "workbench" },
      },
      {
        ...draft,
        wireSnapshot: {
          ...draft.wireSnapshot,
          input_document: draft.input_document,
        },
      },
      {
        ...draft,
        wireSnapshot: { ...draft.wireSnapshot, userProfile: new Date() },
      },
      {
        ...draft,
        wireSnapshot: {
          ...draft.wireSnapshot,
          preferredModel: "x".repeat(1024 * 1024),
        },
      },
    ];
    const result = [];
    for (const value of values) {
      try {
        await recovery.preparePendingReferenceSubmission(
          scope,
          value as ReferenceSubmissionRecoveryDraft,
        );
        result.push(false);
      } catch {
        result.push(true);
      }
    }
    return result;
  });
  expect(rejected).toEqual(Array(8).fill(true));
  expect(await allRecords(page)).toEqual(before);
});

test("the shared writer preserves legacy text bytes and detaches controls before hashing", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    const originalScope = { ...scope };
    const text = {
      message: "ordinary pending",
      attachments: [],
      wireSnapshot: structuredClone(draft.wireSnapshot),
    };
    const original = structuredClone(text);
    const pending = recovery.preparePendingSubmission(scope, text);
    scope.requestedSessionId = "s_changed";
    text.message = "changed while hashing";
    text.wireSnapshot.preferredModel = "changed";
    const prepared = await pending;
    return {
      prepared,
      original,
      read: await recovery.readPendingSubmission(originalScope),
      same: await recovery.preparePendingSubmission(originalScope, original),
    };
  });
  expect(result.same).toEqual(result.prepared);
  expect(result.read).toMatchObject({
    ...result.original,
    clientSubmissionId: result.prepared.clientSubmissionId,
  });
});

test("an unresolved text submission keeps its identity when a reference submission is attempted", async ({
  page,
}) => {
  await load(page);
  await page.evaluate(async () => {
    const { recovery, scope } = window.referencePendingStore;
    await recovery.preparePendingSubmission(scope, {
      message: "text pending",
      attachments: [],
    });
  });
  const before = await allRecords(page);
  const refused = await page.evaluate(async () => {
    const { recovery, scope, draft } = window.referencePendingStore;
    try {
      await recovery.preparePendingReferenceSubmission(scope, draft);
      return false;
    } catch {
      return true;
    }
  });
  expect(refused).toBe(true);
  expect(await allRecords(page)).toEqual(before);
});
