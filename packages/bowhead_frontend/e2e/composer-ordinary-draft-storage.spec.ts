import { expect, test, type Page, type Route } from "@playwright/test";
import { referenceDraftInput, referenceTextContent, seedReferenceDraft } from "./_composer-reference-fixtures";
import { installComposerStorageModules, STORAGE_MODULE_PATH } from "./_composer-storage-modules";
import { mockAuthedApp } from "./_app-mocks";

import {
  routeSubmissionCapability,
  routeTerminalChatExecution,
} from "./_composer-recovery-fixtures";

const LEGACY_RECOVERY_DB_NAME = "bowhead-composer-recovery-v1";
const COMPOSER_RECOVERY_DB_VERSION = 5;
const LEGACY_RECOVERY_STORES = [
  "content_chunk_meta",
  "content_chunk_payload",
  "draft_blobs",
  "identities",
  "ingress_blobs",
  "manifests",
  "ordinary_drafts",
  "principals",
  "submission_fence_ledgers",
  "submission_fences",
] as const;

type RecoveryTransactionCall = {
  databaseName: string;
  mode: IDBTransactionMode;
  storeNames: string[];
};

type OrdinaryDraftStorageRecord = {
  authSessionGenerationHash: string;
  message: string;
  revision: string;
  storageKind: "canonical" | "legacy";
  pendingAction?: {
    actionId: string;
    actionKind: string;
    attempted: boolean;
    sourceRevision: string;
    sessionId: string | null;
  };
};

type OrdinaryDraftStorageMutation = {
  canonicalMutation?: {
    attachments?: unknown[];
    schemaVersion?: number;
    input_document?: unknown;
    message: string;
    revision: string;
  };
  canonicalPendingAction?: Record<string, unknown> | null;
  legacyAuthSessionGenerationHash?: string;
  legacyMessage?: string;
};

async function legacyRecoveryStoreNames(page: Page): Promise<string[]> {
  return await page.evaluate(async ({ databaseName }) => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(databaseName);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("legacy recovery database open blocked"));
    });
    try {
      return Array.from(db.objectStoreNames).sort();
    } finally {
      db.close();
    }
  }, {
    databaseName: LEGACY_RECOVERY_DB_NAME,
  });
}

async function legacyOrdinaryDraftRecords(
  page: Page,
  mutation?: OrdinaryDraftStorageMutation,
): Promise<OrdinaryDraftStorageRecord[]> {
  return await page.evaluate(async ({ databaseName, storageMutation }) => {
    const requestValue = <T,>(request: IDBRequest<T>) => new Promise<T>(
      (resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      },
    );
    const transactionDone = (transaction: IDBTransaction) => new Promise<void>(
      (resolve, reject) => {
        transaction.oncomplete = () => resolve();
        transaction.onerror = () => reject(transaction.error);
        transaction.onabort = () => reject(transaction.error);
      },
    );
    const decodeLengthPrefixedKey = (key: string): string[] => {
      const normalized = key.replaceAll("-", "+").replaceAll("_", "/");
      const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
      const binary = atob(padded);
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const view = new DataView(bytes.buffer);
      const decoder = new TextDecoder();
      const parts: string[] = [];
      for (let offset = 0; offset < bytes.byteLength;) {
        const byteLength = view.getUint32(offset, false);
        offset += 4;
        parts.push(decoder.decode(bytes.slice(offset, offset + byteLength)));
        offset += byteLength;
      }
      return parts;
    };
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(databaseName);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("legacy recovery database open blocked"));
    });
    try {
      const transaction = db.transaction(["ordinary_drafts"], "readonly");
      const store = transaction.objectStore("ordinary_drafts");
      const [keys, records] = await Promise.all([
        requestValue(store.getAllKeys()),
        requestValue(store.getAll()),
      ]);
      await transactionDone(transaction);
      const entries = records.flatMap((record, index) => {
        const key = keys[index];
        if (
          typeof key !== "string"
          || typeof record !== "object"
          || record === null
          || typeof (record as { message?: unknown }).message !== "string"
          || typeof (record as { revision?: unknown }).revision !== "string"
          || typeof (record as { authSessionGenerationHash?: unknown })
            .authSessionGenerationHash !== "string"
        ) return [];
        return [{
          key,
          keyParts: decodeLengthPrefixedKey(key),
          record: record as {
            attachments: unknown[];
            authSessionGenerationHash: string;
            message: string;
            revision: string;
            pendingAction?: OrdinaryDraftStorageRecord["pendingAction"];
          },
        }];
      });
      if (storageMutation) {
        const canonical = entries.find((entry) => entry.keyParts[0] === "ordinary-draft.v2");
        const manifestKey = canonical?.keyParts[1];
        if (!canonical || !manifestKey) throw new Error("canonical ordinary draft missing");
        const writeTransaction = db.transaction(["ordinary_drafts"], "readwrite");
        const writeStore = writeTransaction.objectStore("ordinary_drafts");
        if (
          storageMutation.canonicalMutation
          || storageMutation.canonicalPendingAction !== undefined
        ) {
          const pendingActionUpdate = storageMutation.canonicalPendingAction
            === null
            ? (() => {
                const { pendingAction: _pendingAction, ...withoutPending } =
                  canonical.record;
                return withoutPending;
              })()
            : canonical.record;
          writeStore.put({
            ...pendingActionUpdate,
            ...(storageMutation.canonicalMutation ?? {}),
            ...(storageMutation.canonicalPendingAction
              ? { pendingAction: storageMutation.canonicalPendingAction }
              : {}),
            updatedAt: new Date().toISOString(),
          }, canonical.key);
        }
        if (storageMutation.legacyMessage !== undefined) {
          writeStore.put({
            ...canonical.record,
            message: storageMutation.legacyMessage,
            ...(storageMutation.legacyAuthSessionGenerationHash
              ? {
                  authSessionGenerationHash:
                    storageMutation.legacyAuthSessionGenerationHash,
                }
              : {}),
          }, manifestKey);
        }
        await transactionDone(writeTransaction);
      }
      return entries.map(({ keyParts, record }) => ({
        authSessionGenerationHash: record.authSessionGenerationHash,
        message: record.message,
        revision: record.revision,
        pendingAction: record.pendingAction,
        storageKind: keyParts[0] === "ordinary-draft.v2"
          ? "canonical" as const
          : "legacy" as const,
      }));
    } finally {
      db.close();
    }
  }, {
    databaseName: LEGACY_RECOVERY_DB_NAME,
    storageMutation: mutation,
  });
}

async function legacyOrdinaryDraftMessages(page: Page): Promise<string[]> {
  return (await legacyOrdinaryDraftRecords(page))
    .map((record) => record.message)
    .sort();
}

async function seedReadOnlyLegacyOrdinaryDraftCopy(
  page: Page,
  message: string,
  options: {
    canonicalMutation?: { message: string; revision: string };
    legacyAuthSessionGenerationHash?: string;
  } = {},
): Promise<void> {
  await legacyOrdinaryDraftRecords(page, {
    ...options,
    legacyMessage: message,
  });
}

async function observedRecoveryTransactions(page: Page): Promise<RecoveryTransactionCall[]> {
  return await page.evaluate(() => (
    window as unknown as {
      __composerRecoveryTransactionCalls?: RecoveryTransactionCall[];
    }
  ).__composerRecoveryTransactionCalls ?? []);
}

async function installOneShotMarkerWriteFailure(page: Page): Promise<void> {
  await page.addInitScript(() => {
    Object.defineProperty(window, "__failNextComposerMarkerWrite", {
      configurable: true,
      writable: true,
      value: false,
    });
    const originalTransaction = IDBDatabase.prototype.transaction;
    Object.defineProperty(IDBDatabase.prototype, "transaction", {
      configurable: true,
      value: function (
        this: IDBDatabase,
        storeNames: string | string[],
        mode?: IDBTransactionMode,
        options?: IDBTransactionOptions,
      ): IDBTransaction {
        const names = typeof storeNames === "string"
          ? [storeNames]
          : Array.from(storeNames);
        const state = window as unknown as {
          __failNextComposerMarkerWrite: boolean;
        };
        if (
          state.__failNextComposerMarkerWrite
          && mode === "readwrite"
          && names.includes("ordinary_drafts")
        ) {
          state.__failNextComposerMarkerWrite = false;
          throw new DOMException("forced marker write failure", "QuotaExceededError");
        }
        return Reflect.apply(
          originalTransaction,
          this,
          [storeNames, mode, options],
        ) as IDBTransaction;
      },
    });
  });
}

async function setNextMarkerWriteFailure(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as unknown as { __failNextComposerMarkerWrite: boolean })
      .__failNextComposerMarkerWrite = true;
  });
}

async function installComposerSettlementFailure(page: Page): Promise<void> {
  await page.addInitScript(() => {
    Object.defineProperty(window, "__failComposerSettlement", {
      configurable: true,
      writable: true,
      value: window.sessionStorage.getItem("fail-composer-settlement") === "1",
    });
    Object.defineProperty(window, "__failComposerSettlementOnce", {
      configurable: true,
      writable: true,
      value: false,
    });
    const originalTransaction = IDBDatabase.prototype.transaction;
    Object.defineProperty(IDBDatabase.prototype, "transaction", {
      configurable: true,
      value: function (
        this: IDBDatabase,
        storeNames: string | string[],
        mode?: IDBTransactionMode,
        options?: IDBTransactionOptions,
      ): IDBTransaction {
        const names = typeof storeNames === "string"
          ? [storeNames]
          : Array.from(storeNames);
        if (
          (window as unknown as { __failComposerSettlement: boolean })
            .__failComposerSettlement
          && mode === "readwrite"
          && names.includes("ordinary_drafts")
        ) {
          const state = window as unknown as {
            __failComposerSettlement: boolean;
            __failComposerSettlementOnce: boolean;
          };
          if (state.__failComposerSettlementOnce) {
            state.__failComposerSettlement = false;
            state.__failComposerSettlementOnce = false;
          }
          throw new DOMException("forced settlement failure", "UnknownError");
        }
        return Reflect.apply(
          originalTransaction,
          this,
          [storeNames, mode, options],
        ) as IDBTransaction;
      },
    });
  });
}

test.describe("Composer ordinary draft storage boundary", () => {
  test.skip(
    process.env.BOWHEAD_E2E_PROFILE !== "sse",
    "requires NEXT_PUBLIC_BOWHEAD_SSE_MODE=sse",
  );

  test("retains a completed upload through a real competing-tab draft merge and reload", async ({ page, context }) => {
    await mockAuthedApp(page);
    let releaseUpload!: () => void;
    const uploadGate = new Promise<void>((resolve) => { releaseUpload = resolve; });
    let uploadStarted = false;
    await page.route("**/api/v1/files/upload", async (route) => {
      uploadStarted = true;
      await uploadGate;
      await route.fulfill({ json: { file_id: "art_conflict", filename: "conflict.txt", size: 4, mime: "text/plain", url: "/api/v1/files/art_conflict" } });
    });
    const sent: Array<{ message: string; attachment_refs: Array<{ fileId: string }> }> = [];
    await page.route("**/api/v1/chat/send", async (route) => {
      sent.push(route.request().postDataJSON());
      await route.fulfill({ json: { session_id: "s_upload_merge", turn_id: "t_upload_merge", stream_url: "/api/v1/chat/stream/t_upload_merge", status: "started", execution_generation: 1 } });
    });
    await page.goto("/");
    const input = page.locator("textarea").first();
    await input.fill("Initial draft");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual(["Initial draft"]);
    const other = await context.newPage();
    try {
      await mockAuthedApp(other);
      await other.goto("/");
      await expect(other.locator("textarea").first()).toHaveValue("Initial draft", { timeout: 15_000 });
      await page.locator('input[type="file"]').setInputFiles({ name: "conflict.txt", mimeType: "text/plain", buffer: Buffer.from("test") });
      await expect.poll(() => uploadStarted).toBe(true);
      await other.locator("textarea").first().fill("Other tab intent");
      await expect.poll(() => legacyOrdinaryDraftMessages(other)).toEqual(["Other tab intent"]);
      await input.fill("Local intent");
      const merge = page.getByRole("button", { name: "合并最新草稿", exact: true });
      await expect(merge).toBeVisible();
      releaseUpload();
      await expect(page.locator(".v2-chip--done")).toHaveCount(1);
      await expect(merge).toBeVisible();
      await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
      expect(await legacyOrdinaryDraftMessages(page)).toEqual(["Other tab intent"]);
      expect(sent).toEqual([]);
      await merge.click();
      await expect(input).toHaveValue("Local intent\n\nOther tab intent");
      await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual(["Local intent\n\nOther tab intent"]);
      await other.close();
      await page.reload();
      await expect(page.locator(".v2-chip--done")).toHaveCount(1);
      await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
      await input.press("Enter");
      await expect.poll(() => sent.length).toBe(1);
      expect(sent[0].attachment_refs).toEqual([{ fileId: "art_conflict", filename: "conflict.txt", size: 4, mime: "text/plain" }]);
    } catch (error) {
      if (!other.isClosed()) await test.info().attach("competing-tab", { body: await other.screenshot(), contentType: "image/png" });
      throw error;
    } finally {
      releaseUpload();
      if (!other.isClosed()) await other.close();
    }
  });

  for (const finalText of ["Synthetic A", "Synthetic B", ""]) {
    test(`hands the final draft ${JSON.stringify(finalText)} from Canvas home to ordinary home before Send`, async ({ page }) => {
      await mockAuthedApp(page);
      await page.route("**/api/v1/auth/me", (route) => route.fulfill({ json: {
        id: "u_smoke", team_id: "team_smoke", email: "smoke@example.com", name: "Smoke User", tier: "pro",
        auth_session_generation: "auth-generation-smoke-1",
        capabilities: { settings_pages_canvas: true, composer: { submission_protocol: false, reference_input: false } },
      } }));
      await page.route("**/api/v1/auth/me/workspaces", (route) => route.fulfill({ json: {
        items: [{ id: "ws_smoke", workspace_id: "ws_smoke", name: "Smoke workspace", role: "admin" }], active_id: "ws_smoke",
      } }));
      await page.route("**/api/v1/workspace-settings/ws_smoke/metadata", (route) => route.fulfill({ json: { name: "Smoke workspace", description: "", revision: 1 } }));
      const sent: Array<{ message: string }> = [];
      await page.route("**/api/v1/chat/send", (route) => {
        sent.push(route.request().postDataJSON());
        return route.fulfill({ json: {
          session_id: "s_draft_handoff", turn_id: "t_draft_handoff", stream_url: "/api/v1/chat/stream/t_draft_handoff",
          status: "started", execution_generation: 1,
        } });
      });
      await page.goto("/?canvas_settings=space_general&canvas_user=u_smoke&canvas_team=team_smoke&canvas_space=ws_smoke");
      const input = page.locator("textarea").first();
      await expect(input).toBeEditable();
      await expect(page.getByRole("button", { name: /^关闭「.+」$/ })).toBeVisible();
      await input.fill("Synthetic seed");
      await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual(["Synthetic seed"]);
      // A real readwrite transaction holds subsequent transactions open. No
      // production read results, component state, or CAS outcomes are replaced.
      await page.evaluate(async () => {
        const request = indexedDB.open("bowhead-composer-recovery-v1");
        const db = await new Promise<IDBDatabase>((resolve, reject) => {
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        const state = window as unknown as { releaseDraftGate: () => void; queuedDraftWrites: number };
        let held = true;
        state.releaseDraftGate = () => { held = false; };
        state.queuedDraftWrites = 0;
        const tx = db.transaction("ordinary_drafts", "readwrite");
        const store = tx.objectStore("ordinary_drafts");
        const started = Date.now();
        const hold = () => {
          store.get("test-gate").onsuccess = () => { if (held && Date.now() - started < 15_000) hold(); };
        };
        tx.oncomplete = () => db.close();
        hold();
        const transaction = IDBDatabase.prototype.transaction;
        IDBDatabase.prototype.transaction = function (...args) {
          if (args[1] === "readwrite" && Array.from(typeof args[0] === "string" ? [args[0]] : args[0]).includes("ordinary_drafts")) state.queuedDraftWrites += 1;
          return Reflect.apply(transaction, this, args);
        };
      });
      const previous = await input.elementHandle();
      try {
        await input.fill("Synthetic A");
        await expect.poll(() => page.evaluate(() => (window as unknown as { queuedDraftWrites: number }).queuedDraftWrites)).toBeGreaterThan(0);
        await input.fill(finalText);
        await page.getByRole("button", { name: /^关闭「.+」$/ }).click();
        await expect(page).not.toHaveURL(/canvas_settings=/);
        await expect.poll(() => previous!.evaluate((node) => node.isConnected)).toBe(false);
        await page.evaluate(() => (window as unknown as { releaseDraftGate: () => void }).releaseDraftGate());
        await expect(input).toBeEditable();
        await expect(input).toHaveValue(finalText);
        await expect(page.getByText(/草稿已在另一个窗口/)).toHaveCount(0);
        if (finalText) {
          await input.press("Enter");
          await expect.poll(() => sent.length).toBe(1);
          expect(sent[0].message).toBe(finalText);
        } else {
          await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
          expect(sent).toEqual([]);
        }
      } finally {
        await page.evaluate(() => (window as unknown as { releaseDraftGate: () => void }).releaseDraftGate());
      }
    });
  }

  test("persists an ordinary draft across F5 and preserves all legacy stores after the v3 upgrade", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await expect(textarea).toBeEditable();
    await expect.poll(async () => await page.evaluate(async (databaseName) => {
      const database = (await indexedDB.databases()).find(
        (candidate) => candidate.name === databaseName,
      );
      return database?.version ?? null;
    }, LEGACY_RECOVERY_DB_NAME)).toBe(COMPOSER_RECOVERY_DB_VERSION);
    await expect.poll(() => legacyRecoveryStoreNames(page)).toEqual(
      [...LEGACY_RECOVERY_STORES],
    );

    const message = "default-off draft survives F5";
    await textarea.fill(message);
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toContain(message);
    await page.reload();

    await expect(page.locator("textarea").first())
      .toHaveValue(message);
  });

  test("removes a same-generation read-only legacy copy after a non-empty CAS write", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("canonical before rewrite");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "canonical before rewrite",
    ]);
    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "stale legacy rewrite");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "canonical before rewrite",
      "stale legacy rewrite",
    ]);

    await textarea.fill("canonical after rewrite");

    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "canonical after rewrite",
    ]);
  });

  test("removes a same-generation read-only legacy copy when the draft becomes empty", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("canonical before empty");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "canonical before empty",
    ]);
    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "stale legacy empty");

    await textarea.fill("");

    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    await page.reload();
    await expect(page.locator("textarea").first()).toHaveValue("");
  });

  test("accepted send cannot resurrect a same-generation read-only legacy copy", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    let sendCount = 0;
    let releaseSend: () => void = () => {};
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await sendReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "s_legacy_copy_clear",
          turn_id: "t_legacy_copy_clear",
          stream_url: "/api/v1/chat/stream/t_legacy_copy_clear",
          status: "started",
          execution_generation: 1,
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("canonical accepted send");
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      return canonical?.message ?? null;
    }).toBe("canonical accepted send");
    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "stale legacy accepted send");

    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    releaseSend();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    await page.goto("/");
    await expect(page.locator("textarea").first()).toHaveValue("");
  });

  test("commits one v1 marker and sends typed attachment refs without storing them in the marker", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let requestBody: Record<string, unknown> = {};
    let releaseSend: () => void = () => {};
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      requestBody = route.request().postDataJSON() as Record<string, unknown>;
      await sendReleased;
      const actionId = String(
        (requestBody.client_info as Record<string, unknown>).request_id,
      );
      const suffix = actionId.slice("act_start_".length);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("marker before bytes");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toContain(
      "marker before bytes",
    );
    const initial = (await legacyOrdinaryDraftRecords(page)).find(
      (record) => record.storageKind === "canonical",
    );
    expect(initial).toBeDefined();
    await legacyOrdinaryDraftRecords(page, {
      canonicalMutation: {
        message: "marker before bytes",
        revision: initial!.revision,
        attachments: [{
          fileId: "art_terminal",
          filename: "terminal.pdf",
          size: 11,
          mime: "application/pdf",
        }],
      },
    });
    await page.reload();
    await expect(textarea).toHaveValue("marker before bytes");
    // Restored text can render before the Composer hydration fence opens.
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect.poll(async () => (
      await legacyOrdinaryDraftRecords(page)
    ).find((record) => record.storageKind === "canonical") ?? null).not.toBeNull();
    const canonical = (await legacyOrdinaryDraftRecords(page)).find(
      (record) => record.storageKind === "canonical",
    );
    expect(canonical?.pendingAction).toMatchObject({
      actionId: expect.stringMatching(/^act_start_[0-9a-f]{32}$/),
      actionKind: "start",
      attempted: true,
      sourceRevision: canonical?.revision,
      sessionId: null,
    });
    expect(Object.keys(canonical?.pendingAction ?? {}).sort()).toEqual([
      "actionId",
      "actionKind",
      "attempted",
      "sessionId",
      "sourceRevision",
    ]);
    expect(requestBody).toMatchObject({
      attachments: ["art_terminal"],
      attachment_refs: [{
        fileId: "art_terminal",
        filename: "terminal.pdf",
        size: 11,
        mime: "application/pdf",
      }],
    });
    expect(requestBody).not.toHaveProperty("client_submission_id");
    releaseSend();

    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    expect(sendCount).toBe(1);
  });

  test("keeps an attachment draft unsent when bootstrap omits the typed wire version", async ({ page }) => {
    await routeTerminalChatExecution(page, () => "terminal_v1", null);
    let sendCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.fulfill({ status: 500, body: "unexpected send" });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("wire version is not released");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toContain(
      "wire version is not released",
    );
    const initial = (await legacyOrdinaryDraftRecords(page)).find(
      (record) => record.storageKind === "canonical",
    );
    expect(initial).toBeDefined();
    await legacyOrdinaryDraftRecords(page, {
      canonicalMutation: {
        message: "wire version is not released",
        revision: initial!.revision,
        attachments: [{
          fileId: "art_terminal",
          filename: "terminal.pdf",
          size: 11,
          mime: "application/pdf",
        }],
      },
    });
    await page.reload();
    await expect(textarea).toHaveValue("wire version is not released");
    // Restored text can render before the Composer hydration fence opens.
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();

    await textarea.press("Enter");

    await expect(page.getByText(
      "可靠发送暂时不可用，草稿已保留，请稍后重试。",
    )).toBeVisible();
    await expect.poll(() => sendCount).toBe(0);
    const canonical = (await legacyOrdinaryDraftRecords(page)).find(
      (record) => record.storageKind === "canonical",
    );
    expect(canonical?.message).toBe("wire version is not released");
    expect(canonical?.pendingAction).toBeUndefined();
  });

  test("falls back to one in-memory start when the marker transaction becomes unavailable", async ({ page }) => {
    await installOneShotMarkerWriteFailure(page);
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      const suffix = body.client_info.request_id.slice("act_start_".length);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("marker storage disappeared at click");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "marker storage disappeared at click",
    ]);
    await setNextMarkerWriteFailure(page);
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    expect(sendCount).toBe(1);
  });

  test("resends a fresh in-memory unknown action without waiting for draft autosave", async ({ page }) => {
    await installOneShotMarkerWriteFailure(page);
    await routeTerminalChatExecution(page);
    const actionIds: string[] = [];
    let postCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        client_info?: { request_id?: string };
      };
      postCount += 1;
      const actionId = body.client_info?.request_id;
      if (actionId) actionIds.push(actionId);
      if (postCount === 1) {
        await route.abort("failed");
        return;
      }
      const suffix = actionId?.slice("act_start_".length) ?? "legacy_followup";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("fallback draft must not resurrect");
    await setNextMarkerWriteFailure(page);
    await textarea.press("Enter");
    await expect.poll(() => actionIds).toHaveLength(1);

    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();
    await expect.poll(() => actionIds).toHaveLength(2);
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);

    // The pending state clears only after the action callback settles.
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    // Clearing the local marker precedes the canonical Session handoff.
    // The next user edit belongs to the visible Workbench, not the departing Home.
    const nextComposer = page.locator('[data-composer-surface="workbench"] textarea');
    await expect(nextComposer).toBeEditable();
    await nextComposer.fill("new text after fallback replacement");
    await nextComposer.press("Enter");
    await expect.poll(() => postCount).toBe(3);
  });

  test("keeps an accepted Home action locked through canonical handoff until local settlement succeeds", async ({ page }) => {
    await installComposerSettlementFailure(page);
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let actionId = "";
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      actionId = body.client_info.request_id;
      const suffix = actionId.slice("act_start_".length);
      await page.evaluate(() => {
        (window as unknown as { __failComposerSettlement: boolean })
          .__failComposerSettlement = true;
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("accepted action waits for local cleanup");
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect(page).toHaveURL(new RegExp(
      `session=s_${actionId.slice("act_start_".length)}`,
    ));
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(page.getByRole("button", { name: "恢复草稿或再次发送" }))
      .toHaveCount(0);
    expect(sendCount).toBe(1);

    await page.evaluate(() => {
      (window as unknown as { __failComposerSettlement: boolean })
        .__failComposerSettlement = false;
    });
    await page.getByRole("button", { name: "重新核对结果" }).click();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    expect(sendCount).toBe(1);
  });

  test("returns an off-scope accepted Home action to its canonical Session after one settlement failure", async ({ page }) => {
    await installComposerSettlementFailure(page);
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let actionId = "";
    let releaseSend: () => void = () => {};
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message?: string;
        client_info?: { request_id?: string };
      };
      if (
        body.message !== "off-scope accepted handoff remains recoverable"
        || !body.client_info?.request_id
      ) {
        await route.continue();
        return;
      }
      sendCount += 1;
      actionId = body.client_info.request_id;
      const suffix = actionId.slice("act_start_".length);
      await sendReleased;
      await page.evaluate(() => {
        const state = window as unknown as {
          __failComposerSettlement: boolean;
          __failComposerSettlementOnce: boolean;
        };
        state.__failComposerSettlement = true;
        state.__failComposerSettlementOnce = true;
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("off-scope accepted handoff remains recoverable");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(1);

    await page.getByTitle("Test Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Alice Workspace/ }).press("Enter");
    releaseSend();
    await expect.poll(() => actionId).toMatch(/^act_start_[0-9a-f]{32}$/);
    await expect(page).not.toHaveURL(/session=/);

    await page.getByTitle("Alice Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Test Workspace/ }).press("Enter");
    await expect(page).toHaveURL(new RegExp(
      `session=s_${actionId.slice("act_start_".length)}`,
    ));
    expect(sendCount).toBe(1);
  });

  test("navigates to the accepted Session after an F5 lookup cleanup retry", async ({ page }) => {
    await installComposerSettlementFailure(page);
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let actionId = "";
    let receiptAvailable = false;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      actionId = body.client_info.request_id;
      await route.abort("failed");
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      if (!receiptAvailable) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: { error: { code: "terminal_dependency_recovering" } },
          }),
        });
        return;
      }
      const suffix = actionId.slice("act_start_".length);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          action_id: actionId,
          action_kind: "start",
          outcome: "admitted",
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          execution_generation: 1,
          terminal_protocol_version: 1,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
        }),
      });
    });

    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("accepted receipt survives local cleanup failure");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();

    receiptAvailable = true;
    await page.evaluate(() => {
      window.sessionStorage.setItem("fail-composer-settlement", "1");
    });
    await page.reload();
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(page).not.toHaveURL(/session=/);

    await page.evaluate(() => {
      window.sessionStorage.removeItem("fail-composer-settlement");
      (window as unknown as { __failComposerSettlement: boolean })
        .__failComposerSettlement = false;
    });
    await page.getByRole("button", { name: "重新核对结果" }).click();

    await expect(page).toHaveURL(new RegExp(
      `session=s_${actionId.slice("act_start_".length)}`,
    ));
    expect(sendCount).toBe(1);
  });

  test("blocks a resend after a scope roundtrip while the original POST is pending", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let releaseSend: () => void = () => {};
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      const suffix = body.client_info.request_id.slice("act_start_".length);
      await sendReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("scope roundtrip remains single attempt");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(1);

    await page.getByTitle("Test Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Alice Workspace/ }).press("Enter");
    await page.getByTitle("Alice Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Test Workspace/ }).press("Enter");

    // The predecessor can still settle this POST. The successor stays locked
    // before hydration; its durable marker remains owned by the original action.
    await expect(textarea).not.toBeEditable();
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
    const pending = (await legacyOrdinaryDraftRecords(page)).find((record) => record.message === "scope roundtrip remains single attempt");
    expect(pending?.pendingAction?.attempted).toBe(true);
    await textarea.press("Enter");
    await expect(page.getByRole("button", { name: "恢复草稿或再次发送" }))
      .toHaveCount(0);
    expect(sendCount).toBe(1);
    releaseSend();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    expect(sendCount).toBe(1);
  });

  test("does not replace a start marker while another scope start POST is pending", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let releaseBlockingStart: () => void = () => {};
    const blockingStartReleased = new Promise<void>((resolve) => {
      releaseBlockingStart = resolve;
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        message: string;
        client_info: { request_id: string };
      };
      sendCount += 1;
      if (body.message === "start resend candidate") {
        await route.abort("failed");
        return;
      }
      if (body.message !== "blocking start in another scope") {
        await route.continue();
        return;
      }
      const suffix = body.client_info.request_id.slice("act_start_".length);
      await blockingStartReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });

    await page.goto("/");
    let textarea = page.locator("textarea").first();
    await textarea.fill("start resend candidate");
    await textarea.press("Enter");
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect.poll(async () => (
      await legacyOrdinaryDraftRecords(page)
    ).find((record) => record.message === "start resend candidate")
      ?.pendingAction?.actionId ?? "").not.toBe("");
    const originalActionId = (await legacyOrdinaryDraftRecords(page)).find(
      (record) => record.message === "start resend candidate",
    )?.pendingAction?.actionId ?? "";

    await page.getByTitle("Test Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Alice Workspace/ }).press("Enter");
    textarea = page.locator("textarea").first();
    await textarea.fill("blocking start in another scope");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(2);

    await page.getByTitle("Alice Workspace").first().click();
    await page.getByRole("menuitemradio", { name: /Test Workspace/ }).press("Enter");
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();

    await expect.poll(async () => (
      await legacyOrdinaryDraftRecords(page)
    ).find((record) => record.message === "start resend candidate")
      ?.pendingAction?.actionId ?? "").toBe(originalActionId);
    expect(sendCount).toBe(2);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(page.getByText("结果仍无法确认。草稿和原发送标记均已保留。"))
      .toBeVisible();

    releaseBlockingStart();
    await expect.poll(() => sendCount).toBe(2);
  });

  test("allows only one POST when two tabs race the same v1 draft", async ({ page, context }) => {
    const secondPage = await context.newPage();
    await routeTerminalChatExecution(page);
    await routeTerminalChatExecution(secondPage);
    let sendCount = 0;
    let acceptedActionId = "";
    let lookupCount = 0;
    let releaseSend: () => void = () => {};
    const sendReleased = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });
    await context.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      acceptedActionId = body.client_info.request_id;
      const suffix = body.client_info.request_id.slice("act_start_".length);
      await sendReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await context.route("**/api/v1/chat/actions/**", async (route) => {
      lookupCount += 1;
      if (lookupCount === 1) {
        await route.fulfill({
          status: 404,
          contentType: "application/json",
          body: JSON.stringify({
            detail: { error: { code: "chat_action_not_found" } },
          }),
        });
        return;
      }
      const suffix = acceptedActionId.slice("act_start_".length);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          action_id: acceptedActionId,
          action_kind: "start",
          outcome: "admitted",
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          execution_generation: 1,
          terminal_protocol_version: 1,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
        }),
      });
    });

    await page.goto("/");
    const firstTextarea = page.locator("textarea").first();
    await firstTextarea.fill("one draft, one winner");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "one draft, one winner",
    ]);
    await secondPage.goto("/");
    const secondTextarea = secondPage.locator("textarea").first();
    await expect(secondTextarea).toHaveValue("one draft, one winner");
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
    await expect(secondPage.getByRole("button", { name: "发送", exact: true })).toBeEnabled();

    await Promise.all([
      firstTextarea.press("Enter"),
      secondTextarea.press("Enter"),
    ]);
    await expect.poll(() => sendCount).toBe(1);
    const firstRecovery = page.getByRole("region", { name: "发送状态待确认" });
    const secondRecovery = secondPage.getByRole("region", { name: "发送状态待确认" });
    await expect.poll(async () =>
      Number(await firstRecovery.isVisible()) + Number(await secondRecovery.isVisible())
    ).toBe(1);
    await expect.poll(() => lookupCount).toBe(1);
    // Keep admission pending until the losing tab has observed the shared claim.
    releaseSend();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    expect(sendCount).toBe(1);
    const loserPage = await firstRecovery.isVisible() ? page : secondPage;
    await loserPage.getByRole("button", { name: "重新核对结果" }).click();
    await expect(loserPage).toHaveURL(new RegExp(
      `session=s_${acceptedActionId.slice("act_start_".length)}`,
    ));
    expect(sendCount).toBe(1);
    expect(lookupCount).toBe(2);
  });

  test("allows only one legacy POST when two tabs race the same draft", async ({ page, context }) => {
    const secondPage = await context.newPage();
    await routeSubmissionCapability(page, () => false);
    await routeSubmissionCapability(secondPage, () => false);
    let sendCount = 0;
    await context.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "s_legacy_first_winner",
          turn_id: "t_legacy_first_winner",
          stream_url: "/api/v1/chat/stream/t_legacy_first_winner",
          status: "started",
          execution_generation: 1,
        }),
      });
    });

    await page.goto("/");
    const firstTextarea = page.locator("textarea").first();
    await firstTextarea.fill("legacy one draft, one winner");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "legacy one draft, one winner",
    ]);
    await secondPage.goto("/");
    const secondTextarea = secondPage.locator("textarea").first();
    await expect(secondTextarea).toHaveValue("legacy one draft, one winner");

    await Promise.all([
      firstTextarea.press("Enter"),
      secondTextarea.press("Enter"),
    ]);
    await expect.poll(() => sendCount).toBe(1);
  });

  test("clears a rejected v1 marker after 401 without replaying the POST", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let refreshCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "auth_required" } },
        }),
      });
    });
    await page.route("**/api/v1/auth/refresh", async (route) => {
      refreshCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, token: "refreshed", user: {} }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("401 keeps the draft but not the marker");
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect.poll(() => refreshCount).toBe(1);
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      return {
        message: canonical?.message,
        hasPendingAction: canonical?.pendingAction !== undefined,
      };
    }).toEqual({
      message: "401 keeps the draft but not the marker",
      hasPendingAction: false,
    });
    expect(sendCount).toBe(1);
    await expect(textarea).toHaveValue("401 keeps the draft but not the marker");

    await textarea.fill("editable after rejected marker");
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      return canonical?.message ?? null;
    }).toBe("editable after rejected marker");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(2);
    await expect(page.getByText(/草稿已在其他窗口更新|draft changed in another window/i))
      .toHaveCount(0);
  });

  test("refreshes auth after 401 even when rejected marker settlement is deferred", async ({ page }) => {
    await installComposerSettlementFailure(page);
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let refreshCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await page.evaluate(() => {
        (window as unknown as { __failComposerSettlement: boolean })
          .__failComposerSettlement = true;
      });
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "auth_required" } },
        }),
      });
    });
    await page.route("**/api/v1/auth/refresh", async (route) => {
      refreshCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ ok: true, token: "refreshed", user: {} }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("401 refresh is independent from local settlement");
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect.poll(() => refreshCount).toBe(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect(textarea).toHaveAttribute("readonly", "");

    await page.evaluate(() => {
      (window as unknown as { __failComposerSettlement: boolean })
        .__failComposerSettlement = false;
    });
    await page.getByRole("button", { name: "重新核对结果" }).click();

    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue(
      "401 refresh is independent from local settlement",
    );
    expect(sendCount).toBe(1);
  });

  test("reload only looks up an unresolved v1 action and never re-POSTs it", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let sendCount = 0;
    let lookupCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.abort("failed");
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      lookupCount += 1;
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("unknown response survives reload");
    await textarea.press("Enter");
    await expect.poll(() => sendCount).toBe(1);
    await expect(page.getByText(
      "这次发送结果待确认，输入暂时锁定。这里只核对原结果，不会自动再次发送。",
    )).toBeVisible();

    await page.reload();

    await expect.poll(() => lookupCount).toBeGreaterThanOrEqual(1);
    await expect(page.getByText(
      "这次发送结果待确认，输入暂时锁定。这里只核对原结果，不会自动再次发送。",
    )).toBeVisible();
    expect(sendCount).toBe(1);
    await expect(page.locator("textarea").first()).toHaveValue(
      "unknown response survives reload",
    );
  });

  test("explicit resend replaces the unresolved marker with a new action id", async ({ page }) => {
    await routeTerminalChatExecution(page);
    const actionIds: string[] = [];
    let postCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        client_info?: { request_id?: string };
      };
      postCount += 1;
      const actionId = body.client_info?.request_id;
      if (actionId) actionIds.push(actionId);
      if (postCount === 1) {
        await route.abort("failed");
        return;
      }
      const suffix = actionId?.slice("act_start_".length) ?? "legacy_followup";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
          status: "started",
          execution_generation: 1,
          terminal_protocol_version: 1,
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("explicit replacement only");
    await textarea.press("Enter");
    await expect.poll(() => actionIds).toHaveLength(1);
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();

    await expect.poll(() => actionIds).toHaveLength(2);
    expect(actionIds[1]).not.toBe(actionIds[0]);
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);

    // The pending state clears only after the action callback settles.
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    // Clearing the local marker precedes the canonical Session handoff.
    // The next user edit belongs to the visible Workbench, not the departing Home.
    const nextComposer = page.locator('[data-composer-surface="workbench"] textarea');
    await expect(nextComposer).toBeEditable();
    await nextComposer.fill("send after replacement settled");
    await nextComposer.press("Enter");
    await expect.poll(() => postCount).toBe(3);
  });

  test("keeps a replacement rejection recoverable when local settlement fails", async ({ page }) => {
    await installComposerSettlementFailure(page);
    await routeTerminalChatExecution(page);
    const actionIds: string[] = [];
    let postCount = 0;
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as {
        client_info?: { request_id?: string };
      };
      postCount += 1;
      if (body.client_info?.request_id) {
        actionIds.push(body.client_info.request_id);
      }
      if (postCount === 1) {
        await route.abort("failed");
        return;
      }
      await page.evaluate(() => {
        (window as unknown as { __failComposerSettlement: boolean })
          .__failComposerSettlement = true;
      });
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            code: "request_validation_failed",
            message: "invalid resend fixture",
            field_errors: [],
          },
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("replacement rejection stays recoverable");
    await textarea.press("Enter");
    await expect.poll(() => postCount).toBe(1);
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();

    await expect.poll(() => postCount).toBe(2);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();
    await expect.poll(async () => (
      await legacyOrdinaryDraftRecords(page)
    ).find((record) => record.storageKind === "canonical")
      ?.pendingAction?.actionId ?? "").toBe(actionIds[1]);

    await page.evaluate(() => {
      (window as unknown as { __failComposerSettlement: boolean })
        .__failComposerSettlement = false;
    });
    await page.getByRole("button", { name: "重新核对结果" }).click();

    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("replacement rejection stays recoverable");
    expect(actionIds).toHaveLength(2);
    expect(actionIds[1]).not.toBe(actionIds[0]);
    expect(postCount).toBe(2);
  });

  test("safely unlocks a draft for one explicit legacy send after rollout is turned off", async ({ page }) => {
    let writeProtocol: "terminal_v1" | "legacy_v0" = "terminal_v1";
    await routeTerminalChatExecution(page, () => writeProtocol);
    const bodies: Array<Record<string, unknown>> = [];
    let releaseLegacy: () => void = () => {};
    const legacyReleased = new Promise<void>((resolve) => {
      releaseLegacy = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      bodies.push(body);
      if (bodies.length === 1) {
        await route.abort("failed");
        return;
      }
      await legacyReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "s_legacy_after_rollback",
          turn_id: "t_legacy_after_rollback",
          stream_url: "/api/v1/chat/stream/t_legacy_after_rollback",
          status: "started",
          execution_generation: 1,
        }),
      });
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("rollback sends this draft once");
    await textarea.press("Enter");
    await expect.poll(() => bodies).toHaveLength(1);
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toBeVisible();

    writeProtocol = "legacy_v0";
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();
    await expect(textarea).not.toHaveAttribute("readonly", "");
    expect(bodies).toHaveLength(1);

    await textarea.press("Enter");
    await expect.poll(() => bodies).toHaveLength(2);
    expect(bodies[1]).not.toHaveProperty("client_info.request_id");
    await textarea.press("Enter");
    expect(bodies).toHaveLength(2);

    releaseLegacy();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([]);
    expect(bodies).toHaveLength(2);
  });

  test("preserves a fresh in-memory draft while rollout falls back to legacy", async ({ page }) => {
    await installOneShotMarkerWriteFailure(page);
    let writeProtocol: "terminal_v1" | "legacy_v0" = "terminal_v1";
    await routeTerminalChatExecution(page, () => writeProtocol);
    let postCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      postCount += 1;
      if (postCount === 1) {
        await route.abort("failed");
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "s_fresh_legacy_after_rollback",
          turn_id: "t_fresh_legacy_after_rollback",
          stream_url: "/api/v1/chat/stream/t_fresh_legacy_after_rollback",
          status: "started",
          execution_generation: 1,
        }),
      });
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({
          detail: { error: { code: "chat_action_not_found" } },
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("fresh fallback survives rollback");
    await setNextMarkerWriteFailure(page);
    await textarea.press("Enter");
    await expect.poll(() => postCount).toBe(1);

    writeProtocol = "legacy_v0";
    await page.getByRole("button", { name: "恢复草稿或再次发送" }).click();
    await expect(page.getByRole("region", { name: "发送状态待确认" })).toHaveCount(0);
    await expect(textarea).not.toHaveAttribute("readonly", "");
    await expect(textarea).toHaveValue("fresh fallback survives rollback");
    expect(postCount).toBe(1);

    await textarea.press("Enter");
    await expect.poll(() => postCount).toBe(2);
  });

  test("late action adoption removes only the marker and preserves a newer draft revision", async ({ page }) => {
    await routeTerminalChatExecution(page);
    let actionId = "";
    let sendCount = 0;
    let releaseLookup: () => void = () => {};
    const lookupReleased = new Promise<void>((resolve) => {
      releaseLookup = resolve;
    });
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      const body = route.request().postDataJSON() as {
        client_info: { request_id: string };
      };
      actionId = body.client_info.request_id;
      await route.abort("failed");
    });
    await page.route("**/api/v1/chat/actions/**", async (route) => {
      await lookupReleased;
      const suffix = actionId.slice("act_start_".length);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          action_id: actionId,
          action_kind: "start",
          outcome: "admitted",
          session_id: `s_${suffix}`,
          turn_id: `t_${suffix}`,
          execution_generation: 1,
          terminal_protocol_version: 1,
          stream_url: `/api/v1/chat/stream/t_${suffix}`,
        }),
      });
    });
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("original submitted revision");
    await textarea.press("Enter");
    await expect.poll(() => actionId).toMatch(/^act_start_[0-9a-f]{32}$/);
    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "stale legacy marker copy", {
      canonicalMutation: {
        message: "newer unsent revision",
        revision: "external-newer-revision",
      },
    });
    releaseLookup();

    await expect(page).toHaveURL(new RegExp(
      `session=s_${actionId.slice("act_start_".length)}`,
    ));

    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      return {
        message: canonical?.message,
        revision: canonical?.revision,
        hasPendingAction: canonical?.pendingAction !== undefined,
      };
    }).toEqual({
      message: "newer unsent revision",
      revision: "external-newer-revision",
      hasPendingAction: false,
    });
    expect(sendCount).toBe(1);
  });

  test("preserves foreign-generation and conflicting legacy copies", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await page.goto("/");

    const textarea = page.locator("textarea").first();
    await textarea.fill("canonical before foreign copy");
    let authSessionGenerationHash = "";
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      authSessionGenerationHash = canonical?.authSessionGenerationHash ?? "";
      return canonical?.message ?? null;
    }).toBe("canonical before foreign copy");

    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "foreign generation copy", {
      legacyAuthSessionGenerationHash: "foreign-generation-hash",
    });
    await textarea.fill("canonical after foreign copy");
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "canonical after foreign copy",
      "foreign generation copy",
    ]);
    expect(
      (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "legacy",
      )?.authSessionGenerationHash,
    ).toBe("foreign-generation-hash");

    await seedReadOnlyLegacyOrdinaryDraftCopy(page, "same-generation conflict copy", {
      canonicalMutation: {
        message: "external canonical edit",
        revision: "external-revision",
      },
      legacyAuthSessionGenerationHash: authSessionGenerationHash,
    });
    await textarea.fill("local conflicting edit");

    await expect(page.getByText(/草稿已在其他窗口更新|draft changed in another window/i))
      .toBeVisible();
    await expect.poll(() => legacyOrdinaryDraftMessages(page)).toEqual([
      "external canonical edit",
      "same-generation conflict copy",
    ]);
  });

  test("fails closed on a future pending-action shape", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    let sendCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.abort("failed");
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("future marker must not be ignored");
    let revision = "";
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      revision = canonical?.revision ?? "";
      return revision;
    }).not.toBe("");
    await legacyOrdinaryDraftRecords(page, {
      canonicalPendingAction: {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start",
        attempted: true,
        sessionId: null,
        sourceRevision: revision,
        futureField: "must-fail-closed",
      },
    });

    await page.reload();

    await expect(page.locator(".ui-card-notice")).toBeVisible();
    await expect(page.locator("textarea").first()).toHaveAttribute("readonly", "");
    await expect(page.getByRole("button", { name: /放弃旧草稿|Discard old draft/i }))
      .toHaveCount(0);
    await page.reload();
    await expect(page.locator(".ui-card-notice")).toBeVisible();
    await expect(page.locator("textarea").first()).toHaveAttribute("readonly", "");
    expect(sendCount).toBe(0);
  });

  test("fails closed on an oversized pending-action source revision", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    let networkCount = 0;
    const countUnexpectedNetwork = async (route: Route) => {
      networkCount += 1;
      await route.abort("failed");
    };
    await page.route("**/api/v1/chat/send", countUnexpectedNetwork);
    await page.route("**/api/v1/chat/actions/**", countUnexpectedNetwork);
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("oversized marker identity stays fenced");
    let revision = "";
    await expect.poll(async () => {
      revision = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      )?.revision ?? "";
      return revision;
    }).not.toBe("");
    await legacyOrdinaryDraftRecords(page, {
      canonicalPendingAction: {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start",
        attempted: true,
        sessionId: null,
        sourceRevision: "x".repeat(257),
      },
    });

    await page.reload();

    await expect(page.locator(".ui-card-notice")).toBeVisible();
    await expect(page.locator("textarea").first()).toHaveAttribute("readonly", "");
    await expect(page.getByRole("button", { name: /放弃旧草稿|Discard old draft/i }))
      .toHaveCount(0);
    expect(networkCount).toBe(0);
  });

  test("does not expose discard when an unresolved marker has corrupt attachments", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    let sendCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.abort("failed");
    });
    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("corrupt attachment stays fenced");
    let revision = "";
    await expect.poll(async () => {
      const canonical = (await legacyOrdinaryDraftRecords(page)).find(
        (record) => record.storageKind === "canonical",
      );
      revision = canonical?.revision ?? "";
      return revision;
    }).not.toBe("");
    await legacyOrdinaryDraftRecords(page, {
      canonicalMutation: {
        attachments: [null],
        message: "corrupt attachment stays fenced",
        revision,
      },
      canonicalPendingAction: {
        actionId: "act_start_00112233445546778899aabbccddeeff",
        actionKind: "start",
        attempted: true,
        sessionId: null,
        sourceRevision: revision,
      },
    });

    await page.reload();

    await expect(page.locator(".ui-card-notice")).toBeVisible();
    await expect(page.locator("textarea").first()).toHaveAttribute("readonly", "");
    await expect(page.getByRole("button", { name: /放弃旧草稿|Discard old draft/i }))
      .toHaveCount(0);
    expect(sendCount).toBe(0);
  });

  test("keeps submission write transactions out of the ordinary draft store", async ({ page }) => {
    await page.addInitScript(() => {
      const calls: RecoveryTransactionCall[] = [];
      (
        window as unknown as {
          __composerRecoveryTransactionCalls: RecoveryTransactionCall[];
        }
      ).__composerRecoveryTransactionCalls = calls;
      const originalTransaction = IDBDatabase.prototype.transaction;
      Object.defineProperty(IDBDatabase.prototype, "transaction", {
        configurable: true,
        value: function (
          this: IDBDatabase,
          storeNames: string | string[],
          mode?: IDBTransactionMode,
          options?: IDBTransactionOptions,
        ): IDBTransaction {
          calls.push({
            databaseName: this.name,
            mode: mode ?? "readonly",
            storeNames: typeof storeNames === "string"
              ? [storeNames]
              : Array.from(storeNames),
          });
          return Reflect.apply(
            originalTransaction,
            this,
            [storeNames, mode, options],
          ) as IDBTransaction;
        },
      });
    });
    await routeSubmissionCapability(page, () => true);
    let sendCount = 0;
    await page.route("**/api/v1/chat/send", async (route) => {
      sendCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "s_submission_tx_boundary",
          turn_id: "t_submission_tx_boundary",
          stream_url: "/api/v1/chat/stream/t_submission_tx_boundary",
          status: "started",
          execution_generation: 1,
        }),
      });
    });

    await page.goto("/");
    const textarea = page.locator("textarea").first();
    await textarea.fill("submission storage stays independent");
    await textarea.press("Enter");

    await expect.poll(() => sendCount).toBe(1);
    await expect.poll(async () => {
      const calls = await observedRecoveryTransactions(page);
      return calls.filter((call) => (
        call.databaseName === LEGACY_RECOVERY_DB_NAME
        && call.mode === "readwrite"
        && call.storeNames.includes("manifests")
      )).length;
    }).toBeGreaterThanOrEqual(2);

    const submissionWrites = (await observedRecoveryTransactions(page)).filter(
      (call) => (
        call.databaseName === LEGACY_RECOVERY_DB_NAME
        && call.mode === "readwrite"
        && call.storeNames.includes("manifests")
      ),
    );
    expect(submissionWrites.length).toBeGreaterThanOrEqual(2);
    for (const call of submissionWrites) {
      expect(call.storeNames).not.toContain("ordinary_drafts");
    }
  });
});

test.describe("Composer reference storage upgrade barrier", () => {
  test.skip(
    process.env.BOWHEAD_E2E_PROFILE !== "sse",
    "uses the Composer SSE fixture profile",
  );

  for (const oldVersion of [2, 3, 4] as const) {
  test(`blocks a v${oldVersion} tab, preserves all stores, then rejects old writers after upgrade`, async ({
    page,
    context,
  }) => {
    const oldTab = await context.newPage();
    await installComposerStorageModules(oldTab);
    await oldTab.goto("/__composer_storage_test");
    await oldTab.evaluate(
      async ({ name, stores, oldVersion }) => {
        const db = await new Promise<IDBDatabase>((resolve, reject) => {
          const request = indexedDB.open(name, oldVersion);
          request.onupgradeneeded = () =>
            stores.forEach((store) => request.result.createObjectStore(store));
          request.onerror = () => reject(request.error);
          request.onsuccess = () => resolve(request.result);
        });
        const transaction = db.transaction(stores, "readwrite");
        for (const store of stores)
          transaction.objectStore(store).put({ preserved: store }, "sentinel");
        await new Promise<void>((resolve, reject) => {
          transaction.oncomplete = () => resolve();
          transaction.onabort = () => reject(transaction.error);
        });
        // Hold the old connection to exercise the blocked-upgrade path.
        (window as unknown as { oldComposerDB: IDBDatabase }).oldComposerDB =
          db;
      },
      { name: LEGACY_RECOVERY_DB_NAME, stores: [...LEGACY_RECOVERY_STORES], oldVersion },
    );

    const sends: string[] = [];
    await routeSubmissionCapability(page, () => false);
    await page.route("**/api/v1/chat/send", (route) => {
      sends.push(route.request().postData() ?? "");
      return route.abort();
    });
    await page.goto("/");
    await expect(
      page.getByRole("alert").filter({ hasText: "兼容版本" }),
    ).toBeVisible();
    await expect(page.locator("textarea").first()).not.toBeEditable();
    await expect(page.getByRole("button", { name: "放弃旧草稿" })).toHaveCount(
      0,
    );
    expect(sends).toEqual([]);

    await oldTab.evaluate(async ({ name, oldVersion }) => {
      (
        window as unknown as { oldComposerDB: IDBDatabase }
      ).oldComposerDB.close();
      // This open queues behind the abandoned upgrade and proves it was aborted.
      const request = indexedDB.open(name, oldVersion);
      await new Promise<void>((resolve, reject) => {
        request.onsuccess = () => {
          request.result.close();
          resolve();
        };
        request.onerror = () => reject(request.error);
      });
    }, { name: LEGACY_RECOVERY_DB_NAME, oldVersion });
    await page.getByRole("button", { name: "重试读取" }).click();
    await expect(page.locator("textarea").first()).toBeEditable();
    expect(await legacyRecoveryStoreNames(page)).toEqual([
      ...LEGACY_RECOVERY_STORES,
    ]);
    const result = await oldTab.evaluate(async ({ name, oldVersion, currentVersion }) => {
      const oldResult = await new Promise<string>((resolve) => {
        const request = indexedDB.open(name, oldVersion);
        request.onerror = () => resolve(request.error!.name);
        request.onsuccess = () => {
          request.result.close();
          resolve("unexpected success");
        };
      });
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open(name, currentVersion);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      try {
        const stores = [...db.objectStoreNames];
        const tx = db.transaction(stores, "readonly");
        const preserved = await Promise.all(
          stores.map(
            (store) =>
              new Promise<string>((resolve, reject) => {
                const request = tx.objectStore(store).get("sentinel");
                request.onsuccess = () => resolve(request.result.preserved);
                request.onerror = () => reject(request.error);
              }),
          ),
        );
        return { oldResult, preserved };
      } finally {
        db.close();
      }
    }, { name: LEGACY_RECOVERY_DB_NAME, oldVersion, currentVersion: COMPOSER_RECOVERY_DB_VERSION });
    expect(result.oldResult).toBe("VersionError");
    expect(result.preserved.sort()).toEqual([...LEGACY_RECOVERY_STORES]);
    expect(sends).toEqual([]);
    await oldTab.close();
  });

  }

  for (const width of [412, 1440]) {
    test(`locks a newer persisted document without deleting its action at ${width}px and after F5`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      await routeSubmissionCapability(page, () => false);
      const sends: string[] = [];
      await page.route("**/api/v1/chat/send", (route) => {
        sends.push(route.request().postData() ?? "");
        return route.abort();
      });
      await page.goto("/");
      await page.locator("textarea").first().fill("keep the referenced source");
      await expect
        .poll(() => legacyOrdinaryDraftMessages(page))
        .toContain("keep the referenced source");
      await legacyOrdinaryDraftRecords(page, {
        canonicalMutation: {
          message: "keep the referenced source",
          revision: "ref_revision",
          schemaVersion: 99,
          input_document: {
            schema_version: 1,
            nodes: [{ type: "text", text: "keep the referenced source" }],
          },
        },
        canonicalPendingAction: {
          actionId: "00000000-0000-4000-8000-000000000001",
          actionKind: "start",
          attempted: true,
          sourceRevision: "ref_revision",
          sessionId: null,
        },
      });
      for (let reload = 0; reload < 2; reload++) {
        await page.reload();
        await expect(
          page.getByRole("alert").filter({ hasText: "兼容版本" }),
        ).toBeVisible();
        await expect(page.locator("textarea").first()).not.toBeEditable();
        await expect(
          page.getByRole("button", { name: "放弃旧草稿" }),
        ).toHaveCount(0);
        await expect
          .poll(() => legacyOrdinaryDraftRecords(page))
          .toEqual([
            expect.objectContaining({
              revision: "ref_revision",
              pendingAction: expect.objectContaining({
                actionId: "00000000-0000-4000-8000-000000000001",
              }),
            }),
          ]);
        expect(sends).toEqual([]);
      }
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      await page.screenshot({
        path: testInfo.outputPath("version-barrier.png"),
        fullPage: true,
      });
    });
  }

  for (const storageKind of ["canonical", "legacy", "both"] as const) {
    for (const format of ["future-version", "reference-document"] as const) {
      test(`preserves ${storageKind} ${format} through every draft mutation`, async ({
        page,
      }) => {
        await installComposerStorageModules(page);
        await page.goto("/__composer_storage_test");
        const outcomes = await page.evaluate(
          async ({ prefix, storageKind, format }) => {
            const drafts = (await import(
              `${prefix}/composer-ordinary-draft`
            )) as typeof import("../lib/composer-ordinary-draft");
            const persistence = (await import(
              `${prefix}/composer-persistence-scope`
            )) as typeof import("../lib/composer-persistence-scope");
            const database = (await import(
              `${prefix}/composer-local-persistence-db`
            )) as typeof import("../lib/composer-local-persistence-db");
            const scope = {
              userId: "u_test",
              teamId: "team_test",
              workspaceId: "ws_test",
              requestedSessionId: null,
              authSessionGeneration: "login_test",
            };
            const keys = await persistence.composerPersistenceKeys(scope);
            const canonicalKey = persistence.composerOrdinaryDraftStorageKey(
              keys.manifestKey,
              keys.authSessionGenerationHash,
            );
            const action = {
              actionId: "00000000-0000-4000-8000-000000000001",
              actionKind: "start" as const,
              attempted: true as const,
              sourceRevision: "rev_1",
              sessionId: null,
            };
            const snapshot = {
              message: "keep the full input",
              attachments: [],
            };
            const mutations = {
              read: () => drafts.readComposerOrdinaryDraft(scope),
              write: () =>
                drafts.writeComposerOrdinaryDraft(
                  scope,
                  { message: "overwritten", attachments: [] },
                  "rev_1",
                ),
              clear: () => drafts.clearComposerOrdinaryDraft(scope, "rev_1"),
              prepare: () =>
                drafts.prepareComposerPendingAction(
                  scope,
                  action,
                  "rev_1",
                  snapshot,
                ),
              replace: () =>
                drafts.replaceComposerPendingAction(
                  scope,
                  action,
                  "rev_1",
                  action.actionId,
                ),
              settle: () =>
                drafts.settleComposerPendingAction(
                  scope,
                  action.actionId,
                  "adopted",
                ),
              discard: () => drafts.discardComposerOrdinaryDraft(scope),
            };
            const outcomes = [];
            for (const [operation, mutate] of Object.entries(mutations)) {
              const db = await database.openComposerLocalPersistenceDB();
              const record = {
                ...snapshot,
                revision: "rev_1",
                updatedAt: "2026-09-08T00:00:00Z",
                authSessionGenerationHash: keys.authSessionGenerationHash,
                ...(["replace", "settle"].includes(operation)
                  ? { pendingAction: action }
                  : {}),
              };
              const newer = {
                ...record,
                ...(format === "future-version"
                  ? { schemaVersion: 99 }
                  : {
                      schemaVersion: 2,
                      input_document: {
                        schema_version: 1,
                        nodes: [{ type: "text", text: snapshot.message }],
                      },
                    }),
              };
              const tx = db.transaction(["ordinary_drafts"], "readwrite");
              const store = tx.objectStore("ordinary_drafts");
              store.clear();
              const protectedKey =
                storageKind === "canonical" ? canonicalKey : keys.manifestKey;
              store.put(newer, protectedKey);
              if (storageKind === "both") store.put(record, canonicalKey);
              await database.transactionDone(tx);
              let error = "none";
              try {
                await mutate();
              } catch (caught) {
                error = (caught as Error).name;
              }
              const readTx = db.transaction(["ordinary_drafts"], "readonly");
              const stored = await database.requestValue(
                readTx.objectStore("ordinary_drafts").get(protectedKey),
              );
              const canonical = await database.requestValue(
                readTx.objectStore("ordinary_drafts").get(canonicalKey),
              );
              await database.transactionDone(readTx);
              outcomes.push({
                operation,
                error,
                preserved: JSON.stringify(stored) === JSON.stringify(newer),
                canonicalPreserved:
                  storageKind !== "both" ||
                  JSON.stringify(canonical) === JSON.stringify(record),
              });
              db.close();
            }
            return outcomes;
          },
          { prefix: STORAGE_MODULE_PATH, storageKind, format },
        );
        for (const result of outcomes) {
          expect(result, result.operation).toMatchObject({
            error: "ComposerPersistenceVersionError",
            preserved: true,
            canonicalPreserved: true,
          });
        }
      });
    }
  }
});


test.describe("Composer reference compatibility reader", () => {
  for (const width of [412, 1440]) {
    for (const sessionId of [null, "s_e2e"]) {
      test(`restores references and explicitly removes them at ${width}px in ${sessionId ?? "home"}`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 900 });
        await routeSubmissionCapability(page, () => false);
        await installComposerStorageModules(page);
        await page.goto("/__composer_storage_test");
        const seeded = await seedReferenceDraft(page, { source: "ordinary", sessionId });
        const sends: string[] = [];
        await page.route("**/api/v1/chat/send", (route) => { sends.push(route.request().postData() ?? ""); return route.abort(); });
        await page.goto(sessionId ? `/?session=${sessionId}` : "/");
        const preview = page.getByRole("region", { name: "已保存的引用草稿" });
        for (let pass = 0; pass < 2; pass++) {
          await expect(preview).toBeVisible();
          await expect(preview.getByText("/杜邦分析", { exact: true })).toBeVisible();
          await expect(preview.getByText("盈利分析.pdf", { exact: true })).toBeVisible();
          await preview.getByText("查看引用与参数", { exact: true }).click();
          await expect(preview.locator("pre").filter({ hasText: '"period": "2026Q3"' })).toBeVisible();
          await expect(page.locator("textarea").first()).not.toBeEditable();
          expect(sends).toEqual([]);
          if (pass === 0) await page.reload();
        }
        await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
        await preview.getByRole("button", { name: "复制完整草稿", exact: true }).click();
        expect(JSON.parse(await page.evaluate(() => navigator.clipboard.readText()))).toEqual(referenceDraftInput);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        await page.screenshot({ path: testInfo.outputPath("reference-reader.png"), fullPage: true });
        await preview.getByRole("button", { name: "移除全部引用，保留正文" }).click();
        await expect(preview).toHaveCount(0);
        const textarea = page.locator("textarea").first();
        await expect(textarea).toBeEditable();
        await expect(textarea).toHaveValue(referenceTextContent);
        await page.reload();
        await expect(textarea).toHaveValue(referenceTextContent);
        expect(sends).toEqual([]);
        const stored = await page.evaluate(async ({ key, prefix }) => {
          const storage = await import(`${prefix}/composer-local-persistence-db`);
          const db = await storage.openComposerLocalPersistenceDB();
          const tx = db.transaction(["ordinary_drafts"], "readonly");
          const record = await storage.requestValue(tx.objectStore("ordinary_drafts").get(key));
          await storage.transactionDone(tx); db.close(); return record;
        }, { key: seeded.key, prefix: STORAGE_MODULE_PATH });
        expect(stored.message).toBe(referenceTextContent);
        expect(stored.attachments).toEqual(referenceDraftInput.attachments);
        expect(stored.input_document).toBeUndefined();
        expect(stored.revision).not.toBe("ref_1");
      });
    }
  }

  test("retains a pending reference action and never offers removal", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await installComposerStorageModules(page);
    await page.goto("/__composer_storage_test");
    const seeded = await seedReferenceDraft(page, { source: "ordinary", pending: true });
    await page.goto("/");
    const preview = page.getByRole("region", { name: "已保存的引用草稿" });
    await expect(preview).toBeVisible();
    await expect(preview.getByRole("button", { name: "移除全部引用，保留正文" })).toHaveCount(0);
    const result = await page.evaluate(async ({ seeded, prefix }) => {
      const drafts = await import(`${prefix}/composer-ordinary-draft`);
      const removed = await drafts.removeComposerDraftReferences(seeded.scope, "ref_1");
      const preview = await drafts.readComposerReferenceDraft(seeded.scope);
      return { removed, preview };
    }, { seeded, prefix: STORAGE_MODULE_PATH });
    expect(result.removed).toBe(false);
    expect(result.preview.pendingAction).toEqual(seeded.record.pendingAction);
    expect(result.preview.input_document).toEqual(referenceDraftInput.input_document);
  });

  test("removal preserves changed, unknown, foreign and conflicting fallback records", async ({ page }) => {
    await installComposerStorageModules(page);
    await page.goto("/__composer_storage_test");
    const seeded = await seedReferenceDraft(page, { source: "ordinary" });
    const outcomes = await page.evaluate(async ({ seeded, prefix }) => {
      const storage = await import(`${prefix}/composer-local-persistence-db`);
      const drafts = await import(`${prefix}/composer-ordinary-draft`);
      const persistence = await import(`${prefix}/composer-persistence-scope`);
      const keys = await persistence.composerPersistenceKeys(seeded.scope);
      const db = await storage.openComposerLocalPersistenceDB();
      const cases = [
        { name: "new revision", canonical: { ...seeded.record, revision: "ref_2" } },
        { name: "future field", canonical: { ...seeded.record, future_action: { unresolved: true } } },
        { name: "unknown version", canonical: { ...seeded.record, schemaVersion: 99 } },
        { name: "foreign generation", canonical: { ...seeded.record, authSessionGenerationHash: "foreign" } },
        { name: "unseen fallback", canonical: seeded.record, legacy: { ...seeded.record, revision: "legacy_ref" } },
        { name: "fallback pending", canonical: seeded.record, legacy: { ...seeded.record, schemaVersion: undefined, input_document: undefined, pendingAction: { future: true } } },
      ];
      const result = [];
      for (const entry of cases) {
        const tx = db.transaction(["ordinary_drafts"], "readwrite");
        const store = tx.objectStore("ordinary_drafts"); store.clear(); store.put(entry.canonical, seeded.key);
        if (entry.legacy) store.put(entry.legacy, keys.manifestKey);
        await storage.transactionDone(tx);
        const readRaw = async () => {
          const tx = db.transaction(["ordinary_drafts"], "readonly");
          const records = await storage.requestValue(tx.objectStore("ordinary_drafts").getAll());
          await storage.transactionDone(tx); return JSON.stringify(records);
        };
        const before = await readRaw();
        let removed = false;
        try { removed = await drafts.removeComposerDraftReferences(seeded.scope, "ref_1"); } catch { /* fenced */ }
        result.push({ name: entry.name, removed, preserved: before === await readRaw() });
      }
      db.close(); return result;
    }, { seeded, prefix: STORAGE_MODULE_PATH });
    expect(outcomes.filter((result) => result.removed || !result.preserved)).toEqual([]);
  });
});

test.describe("Composer scoped reference transfers", () => {
  for (const width of [412, 1440]) {
    for (const sessionId of [null, "s_e2e"]) {
      test(`preserves a handoff and pending ordinary source at ${width}px in ${sessionId ?? "home"} after F5`, async ({ page }, testInfo) => {
        await page.setViewportSize({ width, height: 900 });
        await routeSubmissionCapability(page, () => false);
        await installComposerStorageModules(page);
        await page.goto("/__composer_storage_test");
        const seeded = await seedReferenceDraft(page, { source: "ordinary", sessionId, pending: true });
        const handoff = await page.evaluate(async ({ prefix, scope, input }) => {
          const transfer = await import(`${prefix}/composer-reference-transfer`);
          const key = await transfer.composerReferenceHandoffKey(scope);
          const raw = JSON.stringify(input);
          sessionStorage.setItem(key, raw);
          return { key, raw };
        }, { prefix: STORAGE_MODULE_PATH, scope: seeded.scope, input: referenceDraftInput });
        const sends: string[] = [];
        await page.route("**/api/v1/chat/send", (route) => { sends.push(route.request().postData() ?? ""); return route.abort(); });
        await page.goto(sessionId ? `/?session=${sessionId}` : "/");
        const transfer = page.locator('[data-reference-transfer="handoff"]');
        for (let pass = 0; pass < 2; pass++) {
          await expect(transfer).toBeVisible();
          await expect(transfer.getByText("/杜邦分析", { exact: true })).toBeVisible();
          await expect(transfer.getByText("盈利分析.pdf", { exact: true })).toBeVisible();
          await transfer.getByText("查看引用与参数", { exact: true }).click();
          await expect(transfer.locator("pre").filter({ hasText: '"period": "2026Q3"' })).toBeVisible();
          await expect(page.locator("textarea").first()).not.toBeEditable();
          await expect(transfer.getByRole("button", { name: "移除全部引用，保留正文" })).toHaveCount(0);
          expect(await page.evaluate((key) => sessionStorage.getItem(key), handoff.key)).toBe(handoff.raw);
          expect(sends).toEqual([]);
          if (pass === 0) await page.reload();
        }
        await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
        await transfer.getByRole("button", { name: "复制完整草稿", exact: true }).click();
        expect(JSON.parse(await page.evaluate(() => navigator.clipboard.readText()))).toEqual(referenceDraftInput);
        const ordinary = await page.evaluate(async ({ prefix, key }) => {
          const storage = await import(`${prefix}/composer-local-persistence-db`);
          const db = await storage.openComposerLocalPersistenceDB();
          try { return await storage.requestValue(db.transaction(["ordinary_drafts"]).objectStore("ordinary_drafts").get(key)); }
          finally { db.close(); }
        }, { prefix: STORAGE_MODULE_PATH, key: seeded.key });
        expect(ordinary).toEqual(seeded.record);
        // Spec §9: notice fixes are 24px text buttons on a fine pointer (44 on touch).
        expect((await page.locator("[data-notice-id=\"reference-transfer\"] button").first().boundingBox())?.height).toBeGreaterThanOrEqual(24);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        await page.screenshot({ path: testInfo.outputPath("reference-handoff.png"), fullPage: true });
      });
    }
  }
  test("does not take another Session's handoff and preserves unknown versions on arrival", async ({ page }) => {
    await routeSubmissionCapability(page, () => false);
    await installComposerStorageModules(page);
    await page.goto("/__composer_storage_test");
    const handoff = await page.evaluate(async ({ prefix }) => {
      const transfer = await import(`${prefix}/composer-reference-transfer`);
      const scope = { userId: "u_e2e", teamId: "team_test", workspaceId: "ws_test", requestedSessionId: "s_e2e", authSessionGeneration: "auth-generation-e2e-1" };
      const key = await transfer.composerReferenceHandoffKey(scope);
      const raw = '{"schemaVersion":99,"message":"preserve everything","input_document":{"future":true}}';
      sessionStorage.setItem(key, raw);
      return { key, raw };
    }, { prefix: STORAGE_MODULE_PATH });
    await page.goto("/");
    await expect(page.locator("textarea").first()).toBeEditable();
    expect(await page.evaluate((key) => sessionStorage.getItem(key), handoff.key)).toBe(handoff.raw);
    await page.goto("/?session=s_e2e");
    await expect(page.getByRole("alert").filter({ hasText: "版本" })).toBeVisible();
    await expect(page.locator("textarea").first()).not.toBeEditable();
    await page.reload();
    await expect(page.locator("textarea").first()).not.toBeEditable();
    expect(await page.evaluate((key) => sessionStorage.getItem(key), handoff.key)).toBe(handoff.raw);
  });
});
