import type { Page } from "@playwright/test";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import { STORAGE_MODULE_PATH } from "./_composer-storage-modules";

const source = fixtures.valid.find(
  (entry) => entry.name === "exact_agent_and_parameterized_skill",
)!;
export const referenceDraftInput = {
  schemaVersion: 2,
  message: source.display_text!,
  input_document: source.normalized,
  attachments: [
    {
      fileId: "art_saved",
      filename: "盈利分析.pdf",
      mime: "application/pdf",
      size: 123,
    },
  ],
};
export const referenceTextContent = source.text_content!;

/** Seed a future writer's bytes; all reads/removals run the actual storage modules. */
export async function seedReferenceDraft(
  page: Page,
  options: {
    source: "ordinary" | "submission";
    sessionId?: string | null;
    pending?: boolean;
  },
) {
  return page.evaluate(
    async ({ prefix, input, options }) => {
      const storage = (await import(
        `${prefix}/composer-local-persistence-db`
      )) as typeof import("../lib/composer-local-persistence-db");
      const persistence = (await import(
        `${prefix}/composer-persistence-scope`
      )) as typeof import("../lib/composer-persistence-scope");
      const scope = {
        userId: "u_e2e",
        teamId: "team_test",
        workspaceId: "ws_test",
        requestedSessionId: options.sessionId ?? null,
        authSessionGeneration: "auth-generation-e2e-1",
      };
      const keys = await persistence.composerPersistenceKeys(scope);
      if (options.source === "ordinary") {
        const db = await storage.openComposerLocalPersistenceDB();
        const key = persistence.composerOrdinaryDraftStorageKey(
          keys.manifestKey,
          keys.authSessionGenerationHash,
        );
        const record = {
          ...input,
          revision: "ref_1",
          updatedAt: "2026-09-09T00:00:00Z",
          authSessionGenerationHash: keys.authSessionGenerationHash,
          ...(options.pending
            ? {
                pendingAction: {
                  actionId: "act_start_00000000000040008000000000000001",
                  actionKind: "start",
                  attempted: true,
                  sourceRevision: "ref_1",
                  sessionId: scope.requestedSessionId,
                },
              }
            : {}),
        };
        const tx = db.transaction(["ordinary_drafts"], "readwrite");
        tx.objectStore("ordinary_drafts").put(record, key);
        await storage.transactionDone(tx);
        db.close();
        return { scope, key, record };
      }
      const recovery = (await import(
        `${prefix}/composer-submission-recovery`
      )) as typeof import("../lib/composer-submission-recovery");
      const hash = (await import(
        `${prefix}/sha256-fallback`
      )) as typeof import("../lib/sha256-fallback");
      await recovery.preparePendingSubmission(scope, {
        message: input.message,
        attachments: input.attachments,
      });
      const document = new TextEncoder().encode(
        JSON.stringify({
          ...input,
          submissionKind: "start",
          wireSnapshot: {
            schemaVersion: 2,
            input_document: input.input_document,
            composerSurface: scope.requestedSessionId ? "workbench" : "home",
            userProfile: null,
            preferredProvider: null,
            preferredModel: null,
            planningMode: null,
          },
        }),
      );
      const checksum = await hash.sha256Hex(document);
      const db = await storage.openComposerLocalPersistenceDB();
      const tx = db.transaction(
        [
          "manifests",
          "content_chunk_payload",
          "content_chunk_meta",
          "draft_blobs",
        ],
        "readwrite",
      );
      const manifests = tx.objectStore("manifests");
      const manifest = await storage.requestValue(
        manifests.get(keys.manifestKey),
      );
      const record = { ...manifest, contentChecksum: checksum, chunkCount: 1 };
      manifests.put(record, keys.manifestKey);
      const chunkKey = `${manifest.contentId}:000000`;
      tx.objectStore("content_chunk_payload").put(document.buffer, chunkKey);
      tx.objectStore("content_chunk_meta").put(
        {
          contentId: manifest.contentId,
          index: 0,
          byteLength: document.byteLength,
        },
        chunkKey,
      );
      tx.objectStore("draft_blobs").put(
        {
          manifestKey: keys.manifestKey,
          contentId: manifest.contentId,
          chunkCount: 1,
          contentChecksum: checksum,
          encodedBytes: document.byteLength,
        },
        keys.manifestKey,
      );
      await storage.transactionDone(tx);
      db.close();
      return { scope, key: keys.manifestKey, record };
    },
    { prefix: STORAGE_MODULE_PATH, input: referenceDraftInput, options },
  );
}
