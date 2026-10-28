import { expect, test, type Page } from "@playwright/test";
import type { ComposerDraftScope } from "../lib/composer-ordinary-draft";
import type { ComposerReferenceSnapshot } from "../lib/composer-reference-transfer";
import { referenceDraftInput } from "./_composer-reference-fixtures";
import {
  installComposerStorageModules,
  STORAGE_MODULE_PATH,
} from "./_composer-storage-modules";

const scope: ComposerDraftScope = {
  userId: "u_reference_writer",
  teamId: "team_reference",
  workspaceId: "ws_reference",
  requestedSessionId: null,
  authSessionGeneration: "auth-original",
};
declare global {
  interface Window {
    referenceDraftStore: {
      drafts: typeof import("../lib/composer-ordinary-draft");
      persistence: typeof import("../lib/composer-persistence-scope");
      storage: typeof import("../lib/composer-local-persistence-db");
      scope: ComposerDraftScope;
      snapshot: ComposerReferenceSnapshot;
    };
  }
}
async function load(page: Page) {
  await installComposerStorageModules(page);
  await page.goto("/__composer_storage_test");
  await page.evaluate(
    async ({ prefix, scope, snapshot }) => {
      window.referenceDraftStore = {
        drafts: await import(`${prefix}/composer-ordinary-draft`),
        persistence: await import(`${prefix}/composer-persistence-scope`),
        storage: await import(`${prefix}/composer-local-persistence-db`),
        scope,
        snapshot: snapshot as ComposerReferenceSnapshot,
      };
    },
    { prefix: STORAGE_MODULE_PATH, scope, snapshot: referenceDraftInput },
  );
}
async function records(page: Page) {
  return page.evaluate(async () => {
    const { storage } = window.referenceDraftStore;
    const db = await storage.openComposerLocalPersistenceDB();
    try {
      const tx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readonly",
      );
      const store = tx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE);
      const result = await Promise.all([
        storage.requestValue(store.getAllKeys()),
        storage.requestValue(store.getAll()),
      ]);
      await storage.transactionDone(tx);
      return result;
    } finally {
      db.close();
    }
  });
}

test("full reference draft survives reload, detaches input and clears only its consumed revision", async ({
  page,
}) => {
  await load(page);
  const first = await page.evaluate(async () => {
    const { drafts, scope, snapshot } = window.referenceDraftStore;
    const text = await drafts.writeComposerOrdinaryDraft(
      scope,
      { message: "old text", attachments: [] },
      null,
    );
    const originalScope = { ...scope },
      original = structuredClone(snapshot);
    const pending = drafts.writeComposerReferenceDraft(
      scope,
      snapshot,
      text.revision,
    );
    scope.requestedSessionId = "s_changed_during_await";
    snapshot.input_document.nodes.splice(0);
    snapshot.attachments[0].filename = "changed.pdf";
    return {
      written: await pending,
      read: await drafts.readComposerReferenceDraft(originalScope),
      original,
      wrongScope: await drafts.readComposerReferenceDraft(scope),
    };
  });
  expect(first.written.status).toBe("written");
  expect(first.read).toMatchObject({ ...first.original });
  expect(first.wrongScope).toBeNull();
  await load(page);
  const result = await page.evaluate(async (firstRevision) => {
    const { drafts, scope } = window.referenceDraftStore;
    const restored = await drafts.readComposerReferenceDraft(scope);
    const written = await drafts.writeComposerReferenceDraft(
      scope,
      {
        schemaVersion: 2,
        message: "",
        input_document: { schema_version: 1, nodes: [] },
        attachments: [],
      },
      restored!.revision,
    );
    const oldClear = await drafts.clearComposerReferenceDraft(
      scope,
      firstRevision!,
    );
    const empty = await drafts.readComposerReferenceDraft(scope);
    const cleared = await drafts.clearComposerReferenceDraft(
      scope,
      written.revision!,
    );
    return {
      restored,
      oldClear,
      empty,
      cleared,
      final: await drafts.readComposerReferenceDraft(scope),
    };
  }, first.written.revision);
  expect(result.restored).toMatchObject({ ...first.original });
  expect(result.oldClear).toBe(false);
  expect(result.empty).toMatchObject({
    schemaVersion: 2,
    input_document: { schema_version: 1, nodes: [] },
  });
  expect(result.cleared).toBe(true);
  expect(result.final).toBeNull();
});

test("two real tabs racing the same revision have exactly one winner", async ({
  page,
  context,
}) => {
  const other = await context.newPage();
  await Promise.all([load(page), load(other)]);
  const first = await page.evaluate(() => {
    const { drafts, scope, snapshot } = window.referenceDraftStore;
    return drafts.writeComposerReferenceDraft(scope, snapshot, null);
  });
  const write = (target: Page, filename: string) =>
    target.evaluate(
      ({ revision, filename }) => {
        const { drafts, scope, snapshot } = window.referenceDraftStore;
        snapshot.attachments[0].filename = filename;
        return drafts.writeComposerReferenceDraft(scope, snapshot, revision);
      },
      { revision: first.revision, filename },
    );
  const results = await Promise.all([
    write(page, "one.pdf"),
    write(other, "two.pdf"),
  ]);
  expect(results.map((item) => item.status).sort()).toEqual([
    "conflict",
    "written",
  ]);
  const winner = results.findIndex((item) => item.status === "written");
  const read = await other.evaluate(() => {
    const { drafts, scope } = window.referenceDraftStore;
    return drafts.readComposerReferenceDraft(scope);
  });
  expect(read?.attachments[0].filename).toBe(
    winner === 0 ? "one.pdf" : "two.pdf",
  );
  expect(results[1 - winner].revision).toBe(read?.revision);
});

test("each identity dimension isolates saved references and incomplete scopes cannot write", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { drafts, scope, snapshot } = window.referenceDraftStore;
    const written = await drafts.writeComposerReferenceDraft(
      scope,
      snapshot,
      null,
    );
    const isolated = [];
    for (const patch of [
      { userId: "other" },
      { teamId: "other" },
      { workspaceId: "other" },
      { requestedSessionId: "other" },
      { authSessionGeneration: "other" },
    ]) {
      const other = { ...scope, ...patch };
      isolated.push(
        await drafts.readComposerReferenceDraft(other),
        await drafts.clearComposerReferenceDraft(other, written.revision!),
      );
    }
    let invalid = false;
    try {
      await drafts.writeComposerReferenceDraft(
        { ...scope, teamId: "" },
        snapshot,
        null,
      );
    } catch {
      invalid = true;
    }
    return {
      isolated,
      invalid,
      original: await drafts.readComposerReferenceDraft(scope),
    };
  });
  expect(result.isolated).toEqual(
    Array.from({ length: 5 }, () => [null, false]).flat(),
  );
  expect(result.invalid).toBe(true);
  expect(result.original).toMatchObject(referenceDraftInput);
});

for (const fence of [
  "canonical_pending",
  "legacy_pending",
  "legacy_reference",
  "unknown_legacy",
  "unknown_canonical",
  "damaged_document",
  "damaged_attachments",
] as const) {
  test(`preserves the actual ${fence} envelope on save and clear`, async ({
    page,
  }) => {
    await load(page);
    const revision = await page.evaluate(async (fence) => {
      const { drafts, scope, snapshot, storage, persistence } =
        window.referenceDraftStore;
      const written = await drafts.writeComposerReferenceDraft(
        scope,
        snapshot,
        null,
      );
      const keys = await persistence.composerPersistenceKeys(scope);
      const key = persistence.composerOrdinaryDraftStorageKey(
        keys.manifestKey,
        keys.authSessionGenerationHash,
      );
      const db = await storage.openComposerLocalPersistenceDB();
      const tx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readwrite",
      );
      const store = tx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE);
      const record = await storage.requestValue(store.get(key));
      if (fence === "canonical_pending")
        record.pendingAction = { attempted: true, opaque: "unresolved" };
      if (fence === "unknown_canonical") record.schemaVersion = 99;
      if (fence === "damaged_document")
        record.input_document.schema_version = 99;
      if (fence === "damaged_attachments")
        record.attachments = [{ fileId: null }];
      store.put(record, key);
      if (fence.startsWith("legacy") || fence === "unknown_legacy") {
        const legacy =
          fence === "legacy_pending"
            ? {
                message: "old unsent",
                attachments: [],
                updatedAt: record.updatedAt,
                revision: "old",
                authSessionGenerationHash: keys.authSessionGenerationHash,
                pendingAction: { attempted: true },
              }
            : { ...record, schemaVersion: fence === "unknown_legacy" ? 99 : 2 };
        store.put(legacy, keys.manifestKey);
      }
      await storage.transactionDone(tx);
      db.close();
      return written.revision;
    }, fence);
    const before = await records(page);
    const result = await page.evaluate(async (revision) => {
      const { drafts, scope, snapshot } = window.referenceDraftStore;
      let save: unknown, clear: unknown;
      try {
        save = await drafts.writeComposerReferenceDraft(
          scope,
          snapshot,
          revision,
        );
      } catch {
        save = "preserved";
      }
      try {
        clear = await drafts.clearComposerReferenceDraft(scope, revision!);
      } catch {
        clear = "preserved";
      }
      let inputSave: unknown, inputClear: unknown;
      try { inputSave = await drafts.writeComposerInputDraft(scope, { input_document: snapshot.input_document, attachments: snapshot.attachments }, revision); }
      catch { inputSave = "preserved"; }
      try { inputClear = await drafts.clearComposerInputDraft(scope, revision!); }
      catch { inputClear = "preserved"; }
      return { save, clear, inputSave, inputClear };
    }, revision);
    expect(
      result.save === "preserved" ||
        (result.save as { status: string }).status === "conflict",
    ).toBe(true);
    expect(result.clear === "preserved" || result.clear === false).toBe(true);
    expect(result.inputSave === "preserved" || (result.inputSave as { status: string }).status === "conflict").toBe(true);
    expect(result.inputClear === "preserved" || result.inputClear === false).toBe(true);
    expect(await records(page)).toEqual(before);
  });
}

for (const failure of ["abort", "delete_failure"] as const) {
  test(`legacy text adoption commits atomically and ${failure} keeps its fallback`, async ({
    page,
  }) => {
    await load(page);
    await page.evaluate(async () => {
      const { scope, storage, persistence } = window.referenceDraftStore;
      const keys = await persistence.composerPersistenceKeys(scope);
      const db = await storage.openComposerLocalPersistenceDB();
      const tx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readwrite",
      );
      tx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE).put(
        {
          message: "legacy",
          attachments: [],
          revision: "legacy:1",
          updatedAt: "2026-09-14",
          authSessionGenerationHash: keys.authSessionGenerationHash,
        },
        keys.manifestKey,
      );
      await storage.transactionDone(tx);
      db.close();
    });
    const before = await records(page);
    const aborted = await page.evaluate(async (failure) => {
      const { drafts, scope, snapshot } = window.referenceDraftStore;
      const put = IDBObjectStore.prototype.put;
      const remove = IDBObjectStore.prototype.delete;
      IDBObjectStore.prototype.put = function (...args) {
        const request = put.apply(this, args);
        if (failure === "abort") queueMicrotask(() => this.transaction.abort());
        return request;
      };
      if (failure === "delete_failure")
        IDBObjectStore.prototype.delete = function () {
          throw new DOMException("Storage failed", "QuotaExceededError");
        };
      try {
        await drafts.writeComposerReferenceDraft(scope, snapshot, "legacy:1");
        return false;
      } catch {
        return true;
      } finally {
        IDBObjectStore.prototype.put = put;
        IDBObjectStore.prototype.delete = remove;
      }
    }, failure);
    expect(aborted).toBe(true);
    expect(await records(page)).toEqual(before);
    const adopted = await page.evaluate(async () => {
      const { drafts, scope, snapshot } = window.referenceDraftStore;
      const written = await drafts.writeComposerReferenceDraft(
        scope,
        snapshot,
        "legacy:1",
      );
      return { written, read: await drafts.readComposerReferenceDraft(scope) };
    });
    expect(adopted.written.status).toBe("written");
    expect(adopted.read).toMatchObject(referenceDraftInput);
    const after = await records(page);
    expect(after[0]).toHaveLength(1);
    expect(after[0]).not.toEqual(before[0]);
  });
}

test("invalid new snapshots and a reference acknowledgement preserve an ordinary draft", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { drafts, scope, snapshot } = window.referenceDraftStore;
    const original = await drafts.writeComposerOrdinaryDraft(
      scope,
      { message: "ordinary unsent", attachments: [] },
      null,
    );
    const rejected = [];
    for (const invalid of [
      { ...snapshot, message: "mismatched display" },
      { ...snapshot, schemaVersion: 3 },
      { ...snapshot, pendingAction: {} },
      { ...snapshot, attachments: [{ fileId: null }] },
    ]) {
      try {
        await drafts.writeComposerReferenceDraft(
          scope,
          invalid as ComposerReferenceSnapshot,
          original.revision,
        );
        rejected.push(false);
      } catch {
        rejected.push(true);
      }
    }
    const cleared = await drafts.clearComposerReferenceDraft(
      scope,
      original.revision!,
    );
    return {
      rejected,
      cleared,
      original,
      read: await drafts.readComposerOrdinaryDraft(scope),
    };
  });
  expect(result.rejected).toEqual([true, true, true, true]);
  expect(result.cleared).toBe(false);
  expect(result.read).toMatchObject({
    message: "ordinary unsent",
    revision: result.original.revision,
  });
});

test("input owner transitions between text and references under one revision fence", async ({ page }) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { drafts, scope, snapshot } = window.referenceDraftStore;
    const plain = await drafts.writeComposerOrdinaryDraft(scope, { message: "before", attachments: [] }, null);
    const readPlain = await drafts.readComposerInputDraft(scope);
    const pending = drafts.writeComposerInputDraft(scope, { input_document: snapshot.input_document, attachments: snapshot.attachments }, plain.revision);
    const original = structuredClone(snapshot);
    snapshot.input_document.nodes.splice(0);
    const rich = await pending;
    const readRich = await drafts.readComposerInputDraft(scope);
    const stale = await drafts.writeComposerInputDraft(scope, { input_document: { schema_version: 1, nodes: [{ type: "text", text: "stale" }] }, attachments: [] }, plain.revision);
    const text = await drafts.writeComposerInputDraft(scope, { input_document: { schema_version: 1, nodes: [{ type: "text", text: "after" }] }, attachments: [] }, rich.revision);
    const oldReader = await drafts.readComposerOrdinaryDraft(scope);
    const staleClear = await drafts.clearComposerInputDraft(scope, rich.revision!);
    const finalClear = await drafts.clearComposerInputDraft(scope, text.revision!);
    return { readPlain, readRich, original, stale, oldReader, staleClear, finalClear, final: await drafts.readComposerInputDraft(scope) };
  });
  expect(result.readPlain?.input_document).toEqual({ schema_version: 1, nodes: [{ type: "text", text: "before" }] });
  expect(result.readRich?.input_document).toEqual(result.original.input_document);
  expect(result.readRich?.attachments).toEqual(result.original.attachments);
  expect(result.stale.status).toBe("conflict");
  expect(result.oldReader?.message).toBe("after");
  expect(result.oldReader).not.toHaveProperty("input_document");
  expect(result.staleClear).toBe(false);
  expect(result.finalClear).toBe(true);
  expect(result.final).toBeNull();
});
