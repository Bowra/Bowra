import { expect, test, type Page } from "@playwright/test";
import type { ComposerPersistenceScope } from "../lib/composer-persistence-scope";
import type { ComposerReferenceSnapshot } from "../lib/composer-reference-transfer";
import { referenceDraftInput } from "./_composer-reference-fixtures";
import {
  installComposerStorageModules,
  STORAGE_MODULE_PATH,
} from "./_composer-storage-modules";

const scope: ComposerPersistenceScope = {
  userId: "u_handoff",
  teamId: "team_handoff",
  workspaceId: "ws_handoff",
  requestedSessionId: null,
  authSessionGeneration: "auth-handoff",
};
declare global {
  interface Window {
    referenceHandoffStore: {
      handoff: typeof import("../lib/composer-reference-handoff");
      transfer: typeof import("../lib/composer-reference-transfer");
      ordinary: typeof import("../lib/composer-ordinary-draft");
      persistence: typeof import("../lib/composer-persistence-scope");
      storage: typeof import("../lib/composer-local-persistence-db");
      scope: ComposerPersistenceScope;
      snapshot: ComposerReferenceSnapshot;
    };
    heldHandoffDB?: IDBDatabase;
  }
}
async function load(page: Page) {
  await installComposerStorageModules(page);
  await page.goto("/__composer_storage_test");
  await page.evaluate(
    async ({ prefix, scope, snapshot }) => {
      window.referenceHandoffStore = {
        handoff: await import(`${prefix}/composer-reference-handoff`),
        transfer: await import(`${prefix}/composer-reference-transfer`),
        ordinary: await import(`${prefix}/composer-ordinary-draft`),
        persistence: await import(`${prefix}/composer-persistence-scope`),
        storage: await import(`${prefix}/composer-local-persistence-db`),
        scope,
        snapshot: snapshot as ComposerReferenceSnapshot,
      };
    },
    { prefix: STORAGE_MODULE_PATH, scope, snapshot: referenceDraftInput },
  );
}

test("a scoped full handoff survives navigation and is acknowledged only after saving its original source", async ({
  page,
}) => {
  await load(page);
  const staged = await page.evaluate(async () => {
    const { handoff, scope, snapshot } = window.referenceHandoffStore;
    sessionStorage.setItem("bowhead:composer-prefill", "unrelated text");
    const pending = handoff.stageComposerReferenceHandoff(scope, snapshot);
    scope.workspaceId = "after-await";
    snapshot.input_document.nodes.splice(0);
    snapshot.attachments[0].filename = "changed";
    return pending;
  });
  expect(staged.status).toBe("staged");
  await load(page);
  const result = await page.evaluate(async () => {
    const { handoff, transfer, ordinary, scope, snapshot } =
      window.referenceHandoffStore;
    const before = await transfer.readComposerReferenceHandoff(scope);
    const duplicate = await handoff.stageComposerReferenceHandoff(
      scope,
      snapshot,
    );
    const saved = await handoff.acceptComposerReferenceHandoff(scope);
    return {
      before,
      duplicate,
      saved,
      read: await ordinary.readComposerReferenceDraft(scope),
      next: await handoff.acceptComposerReferenceHandoff(scope),
      text: sessionStorage.getItem("bowhead:composer-prefill"),
    };
  });
  expect(result.before).toEqual(referenceDraftInput);
  expect(result.duplicate.status).toBe("staged");
  expect(result.saved).toMatchObject({ status: "saved", acknowledged: true });
  expect(result.read).toMatchObject(referenceDraftInput);
  expect(result.next.status).toBe("missing");
  expect(result.text).toBe("unrelated text");
});

for (const field of [
  "userId",
  "teamId",
  "workspaceId",
  "requestedSessionId",
  "authSessionGeneration",
] as const) {
  test(`handoff acceptance cannot cross ${field}`, async ({ page }) => {
    await load(page);
    const result = await page.evaluate(async (field) => {
      const { handoff, transfer, ordinary, scope, snapshot } =
        window.referenceHandoffStore;
      await handoff.stageComposerReferenceHandoff(scope, snapshot);
      const other = { ...scope, [field]: "different" };
      return {
        other: await handoff.acceptComposerReferenceHandoff(other),
        draft: await ordinary.readComposerReferenceDraft(other),
        original: await transfer.readComposerReferenceHandoff(scope),
      };
    }, field);
    expect(result.other.status).toBe("missing");
    expect(result.draft).toBeNull();
    expect(result.original).toEqual(referenceDraftInput);
  });
}

for (const mode of [
  "text",
  "different_reference",
  "pending",
  "hidden_legacy_pending",
  "unknown_handoff",
] as const) {
  test(`keeps the handoff and ${mode} intact on conflict`, async ({ page }) => {
    await load(page);
    const result = await page.evaluate(async (mode) => {
      const {
        handoff,
        transfer,
        ordinary,
        storage,
        persistence,
        scope,
        snapshot,
      } = window.referenceHandoffStore;
      await handoff.stageComposerReferenceHandoff(scope, snapshot);
      const key = await transfer.composerReferenceHandoffKey(scope);
      if (mode === "unknown_handoff")
        sessionStorage.setItem(key, '{"schemaVersion":99,"private":"keep"}');
      if (mode === "text")
        await ordinary.writeComposerOrdinaryDraft(
          scope,
          { message: "unsent text", attachments: [] },
          null,
        );
      else if (mode !== "unknown_handoff") {
        const other = structuredClone(snapshot);
        if (mode === "different_reference")
          other.attachments[0].filename = "different.pdf";
        await ordinary.writeComposerReferenceDraft(scope, other, null);
      }
      const keys = await persistence.composerPersistenceKeys(scope);
      const canonicalKey = persistence.composerOrdinaryDraftStorageKey(
        keys.manifestKey,
        keys.authSessionGenerationHash,
      );
      const db = await storage.openComposerLocalPersistenceDB();
      const tx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readwrite",
      );
      const store = tx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE);
      const current = await storage.requestValue(store.get(canonicalKey));
      if (mode === "pending") {
        current.pendingAction = {
          actionId: "act_start_00000000000040008000000000000001",
          actionKind: "start",
          attempted: true,
          sourceRevision: current.revision,
          sessionId: null,
        };
        store.put(current, canonicalKey);
      }
      if (mode === "hidden_legacy_pending")
        store.put(
          {
            message: "pending elsewhere",
            attachments: [],
            revision: "legacy",
            updatedAt: "2026-09-14",
            authSessionGenerationHash: keys.authSessionGenerationHash,
            pendingAction: { attempted: true },
          },
          keys.manifestKey,
        );
      await storage.transactionDone(tx);
      const beforeTx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readonly",
      );
      const before = await storage.requestValue(
        beforeTx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE).getAll(),
      );
      await storage.transactionDone(beforeTx);
      const raw = sessionStorage.getItem(key);
      let result: unknown;
      try {
        result = await handoff.acceptComposerReferenceHandoff(scope);
      } catch {
        result = "preserved";
      }
      const afterTx = db.transaction(
        [storage.COMPOSER_ORDINARY_DRAFT_STORE],
        "readonly",
      );
      const after = await storage.requestValue(
        afterTx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE).getAll(),
      );
      await storage.transactionDone(afterTx);
      db.close();
      return {
        result,
        before,
        after,
        raw,
        afterRaw: sessionStorage.getItem(key),
        replace: await handoff.stageComposerReferenceHandoff(scope, {
          ...snapshot,
          attachments: [],
        }),
      };
    }, mode);
    expect(
      result.result === "preserved" ||
        (result.result as { status: string }).status === "conflict",
    ).toBe(true);
    expect(result.after).toEqual(result.before);
    expect(result.afterRaw).toBe(result.raw);
    expect(result.replace.status).toBe("conflict");
  });
}

test("a real draft write failure keeps the handoff, and a lost acknowledgement can safely retry", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { handoff, transfer, ordinary, scope, snapshot } =
      window.referenceHandoffStore;
    await handoff.stageComposerReferenceHandoff(scope, snapshot);
    const key = await transfer.composerReferenceHandoffKey(scope),
      raw = sessionStorage.getItem(key);
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function () {
      throw new DOMException("Storage failed", "QuotaExceededError");
    };
    let failed = false;
    try {
      await handoff.acceptComposerReferenceHandoff(scope);
    } catch {
      failed = true;
    } finally {
      IDBObjectStore.prototype.put = put;
    }
    const afterFailure = {
      raw: sessionStorage.getItem(key),
      draft: await ordinary.readComposerReferenceDraft(scope),
    };
    const remove = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function (name) {
      if (this === sessionStorage && name === key)
        throw new DOMException("Cannot clear", "SecurityError");
      return remove.call(this, name);
    };
    let first;
    try {
      first = await handoff.acceptComposerReferenceHandoff(scope);
    } finally {
      Storage.prototype.removeItem = remove;
    }
    const retained = sessionStorage.getItem(key);
    const retry = await handoff.acceptComposerReferenceHandoff(scope);
    return {
      failed,
      afterFailure,
      raw,
      retained,
      first,
      retry,
      read: await ordinary.readComposerReferenceDraft(scope),
      final: sessionStorage.getItem(key),
    };
  });
  expect(result.failed).toBe(true);
  expect(result.afterFailure).toEqual({ raw: result.raw, draft: null });
  expect(result.retained).toBe(result.raw);
  expect(result.first).toMatchObject({ status: "saved", acknowledged: false });
  expect(result.retry).toMatchObject({ status: "saved", acknowledged: true });
  expect(result.read).toMatchObject(referenceDraftInput);
  expect(result.final).toBeNull();
});

test("acknowledgement never deletes a newer handoff created while the original draft commits", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { handoff, transfer, ordinary, scope, snapshot } =
      window.referenceHandoffStore;
    await handoff.stageComposerReferenceHandoff(scope, snapshot);
    const key = await transfer.composerReferenceHandoffKey(scope);
    const next = { ...snapshot, attachments: [] };
    const put = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (...args) {
      const result = put.apply(this, args);
      if (this.name === "ordinary_drafts")
        sessionStorage.setItem(key, JSON.stringify(next));
      return result;
    };
    let accepted;
    try {
      accepted = await handoff.acceptComposerReferenceHandoff(scope);
    } finally {
      IDBObjectStore.prototype.put = put;
    }
    return {
      accepted,
      next,
      pending: await transfer.readComposerReferenceHandoff(scope),
      saved: await ordinary.readComposerReferenceDraft(scope),
    };
  });
  expect(result.accepted).toMatchObject({
    status: "saved",
    acknowledged: false,
  });
  expect(result.pending).toEqual(result.next);
  expect(result.saved).toMatchObject(referenceDraftInput);
});

for (const oldVersion of [2, 3, 4]) {
  test(`v${oldVersion} blocks handoff production until the shared upgrade preserves every store`, async ({
    page,
    context,
  }) => {
    const old = await context.newPage();
    await load(old);
    const stores = [
      "identities",
      "principals",
      "submission_fence_ledgers",
      "submission_fences",
      "manifests",
      "draft_blobs",
      "ingress_blobs",
      "content_chunk_meta",
      "content_chunk_payload",
      "ordinary_drafts",
    ];
    await old.evaluate(
      async ({ version, stores }) => {
        const db = await new Promise<IDBDatabase>((resolve, reject) => {
          const req = indexedDB.open("bowhead-composer-recovery-v1", version);
          req.onupgradeneeded = () =>
            stores.forEach((name) => req.result.createObjectStore(name));
          req.onsuccess = () => resolve(req.result);
          req.onerror = () => reject(req.error);
        });
        const tx = db.transaction(stores, "readwrite");
        stores.forEach((name) =>
          tx.objectStore(name).put({ preserved: name }, "sentinel"),
        );
        await window.referenceHandoffStore.storage.transactionDone(tx);
        window.heldHandoffDB = db;
      },
      { version: oldVersion, stores },
    );
    await load(page);
    const blocked = await page.evaluate(async () => {
      const { handoff, transfer, scope, snapshot } =
        window.referenceHandoffStore;
      let error = "";
      try {
        await handoff.stageComposerReferenceHandoff(scope, snapshot);
      } catch (e) {
        error = (e as Error).name;
      }
      return {
        error,
        staged: await transfer.readComposerReferenceHandoff(scope),
      };
    });
    expect(blocked).toEqual({
      error: "ComposerPersistenceVersionError",
      staged: null,
    });
    await old.evaluate(async (version) => {
      window.heldHandoffDB!.close();
      await new Promise<void>((resolve, reject) => {
        const req = indexedDB.open("bowhead-composer-recovery-v1", version);
        req.onsuccess = () => {
          req.result.close();
          resolve();
        };
        req.onerror = () => reject(req.error);
      });
    }, oldVersion);
    const upgraded = await page.evaluate(async () => {
      const { handoff, scope, snapshot, storage } =
        window.referenceHandoffStore;
      const staged = await handoff.stageComposerReferenceHandoff(
        scope,
        snapshot,
      );
      const db = await storage.openComposerLocalPersistenceDB();
      const names = Array.from(db.objectStoreNames);
      const tx = db.transaction(names, "readonly");
      const preserved = await Promise.all(
        names.map((name) =>
          storage.requestValue(tx.objectStore(name).get("sentinel")),
        ),
      );
      await storage.transactionDone(tx);
      const version = db.version;
      db.close();
      return { staged, version, preserved };
    });
    expect(upgraded.staged.status).toBe("staged");
    expect(upgraded.version).toBe(5);
    expect(upgraded.preserved.map((row) => row.preserved).sort()).toEqual(
      stores.sort(),
    );
    const stale = await old.evaluate(
      async (version) =>
        new Promise<string>((resolve) => {
          const req = indexedDB.open("bowhead-composer-recovery-v1", version);
          req.onerror = () => resolve(req.error!.name);
          req.onsuccess = () => {
            req.result.close();
            resolve("unexpected success");
          };
        }),
      oldVersion,
    );
    expect(stale).toBe("VersionError");
  });
}

test("an edit committed after the acceptance read wins CAS and keeps the handoff", async ({
  page,
}) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const {
      handoff,
      transfer,
      ordinary,
      storage,
      persistence,
      scope,
      snapshot,
    } = window.referenceHandoffStore;
    await handoff.stageComposerReferenceHandoff(scope, snapshot);
    const keys = await persistence.composerPersistenceKeys(scope);
    const canonicalKey = persistence.composerOrdinaryDraftStorageKey(
      keys.manifestKey,
      keys.authSessionGenerationHash,
    );
    const get = IDBObjectStore.prototype.get;
    let reads = 0;
    // Queue a real competing write after the final empty read has captured its
    // value, before acceptance opens its CAS transaction. No CAS is mocked.
    IDBObjectStore.prototype.get = function (...args) {
      const request = get.apply(this, args);
      if (
        this.name === storage.COMPOSER_ORDINARY_DRAFT_STORE &&
        this.transaction.mode === "readonly"
      ) {
        request.addEventListener("success", () => {
          if (++reads !== 3) return;
          const tx = this.transaction.db.transaction(
            [storage.COMPOSER_ORDINARY_DRAFT_STORE],
            "readwrite",
          );
          tx.objectStore(storage.COMPOSER_ORDINARY_DRAFT_STORE).put(
            {
              message: "typed elsewhere",
              attachments: [],
              revision: "competing-edit",
              updatedAt: "2026-09-14",
              authSessionGenerationHash: keys.authSessionGenerationHash,
            },
            canonicalKey,
          );
        });
      }
      return request;
    };
    let accepted;
    try {
      accepted = await handoff.acceptComposerReferenceHandoff(scope);
    } finally {
      IDBObjectStore.prototype.get = get;
    }
    return {
      accepted,
      pending: await transfer.readComposerReferenceHandoff(scope),
      draft: await ordinary.readComposerOrdinaryDraft(scope),
    };
  });
  expect(result.accepted.status).toBe("conflict");
  expect(result.pending).toEqual(referenceDraftInput);
  expect(result.draft).toMatchObject({
    message: "typed elsewhere",
    revision: "competing-edit",
  });
});

test("accept and discard operate only on the handoff actually shown to the user", async ({ page }) => {
  await load(page);
  const result = await page.evaluate(async () => {
    const { handoff, transfer, ordinary, scope, snapshot } = window.referenceHandoffStore;
    await handoff.stageComposerReferenceHandoff(scope, snapshot);
    const shown = structuredClone(snapshot);
    shown.attachments[0].filename = "old-preview.pdf";
    const wrongAccept = await handoff.acceptComposerReferenceHandoff(scope, shown);
    const wrongDiscard = await handoff.discardComposerReferenceHandoff(scope, shown);
    const original = await transfer.readComposerReferenceHandoff(scope);
    const removed = await handoff.discardComposerReferenceHandoff(scope, snapshot);
    return { wrongAccept, wrongDiscard, original, removed, final: await transfer.readComposerReferenceHandoff(scope), draft: await ordinary.readComposerReferenceDraft(scope) };
  });
  expect(result.wrongAccept.status).toBe("conflict");
  expect(result.wrongDiscard).toBe(false);
  expect(result.original).toEqual(referenceDraftInput);
  expect(result.removed).toBe(true);
  expect(result.final).toBeNull();
  expect(result.draft).toBeNull();
});
