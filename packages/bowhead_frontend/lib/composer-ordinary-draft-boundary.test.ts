import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  canonicalizeComposerAttachmentRef,
  canonicalizeComposerAttachmentRefs,
} from "./composer-ordinary-draft";

const readSource = (relativePath: string) => readFileSync(
  join(__dirname, relativePath),
  "utf8",
);

function namedImportOwners(source: string, symbol: string): string[] {
  const owners: string[] = [];
  const imports = /import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+["']([^"']+)["']/g;
  for (const match of source.matchAll(imports)) {
    const names = (match[1] ?? "")
      .split(",")
      .map((name) => name.trim().replace(/^type\s+/, "").split(/\s+as\s+/)[0]);
    if (names.includes(symbol)) owners.push(match[2] ?? "");
  }
  return owners;
}

describe("Composer ordinary draft module boundary", () => {
  const submission = readSource("composer-submission-recovery.ts");
  const ordinary = readSource("composer-ordinary-draft.ts");
  const localDatabase = readSource("composer-local-persistence-db.ts");
  const chatAction = readSource("composer-chat-action.ts");
  const composer = readSource("../components/chat/v2-composer.tsx");
  const workbench = readSource("../components/chat-workbench.tsx");
  const page = readSource("../app/(app)/page.tsx");

  it("keeps ordinary CRUD and public types out of the submission owner", () => {
    const functions = [
      "readComposerOrdinaryDraft",
      "writeComposerOrdinaryDraft",
      "clearComposerOrdinaryDraft",
      "discardComposerOrdinaryDraft",
    ];
    for (const name of functions) {
      expect(submission).not.toMatch(
        new RegExp(`export\\s+(?:async\\s+)?function\\s+${name}\\b`),
      );
      expect(submission).not.toMatch(
        new RegExp(`export\\s*(?:type\\s*)?\\{[^}]*\\b${name}\\b`),
      );
    }
    for (const name of [
      "ComposerDraftScope",
      "ComposerOrdinaryDraft",
      "ComposerOrdinaryDraftWriteOutcome",
    ]) {
      expect(submission).not.toMatch(
        new RegExp(`export\\s+(?:interface|type)\\s+${name}\\b`),
      );
      expect(submission).not.toMatch(
        new RegExp(`export\\s*(?:type\\s*)?\\{[^}]*\\b${name}\\b`),
      );
    }
    expect(submission).not.toContain("./composer-ordinary-draft");
    expect(submission).not.toContain("composerOrdinaryDraftStorageKey");
  });

  it("keeps the ordinary owner one-way while the frozen local schema retains its store", () => {
    expect(ordinary).not.toContain("./composer-submission-recovery");
    expect(namedImportOwners(ordinary, "openComposerLocalPersistenceDB"))
      .toEqual(["./composer-local-persistence-db"]);
    expect(namedImportOwners(ordinary, "COMPOSER_ORDINARY_DRAFT_STORE"))
      .toEqual(["./composer-local-persistence-db"]);

    expect(submission).not.toContain('"ordinary_drafts"');
    expect(localDatabase).toContain('"bowhead-composer-recovery-v1"');
    expect(localDatabase).toContain("COMPOSER_LOCAL_DB_VERSION = 5");
    expect(localDatabase).toContain('"ordinary_drafts"');
    expect(ordinary).not.toContain('"ordinary_drafts"');
    expect(ordinary).toContain("db.transaction([COMPOSER_ORDINARY_DRAFT_STORE]");
  });

  it("routes both Composer consumers directly to the ordinary draft owner", () => {
    for (const symbol of [
      "readComposerOrdinaryDraft",
      "writeComposerOrdinaryDraft",
      "clearComposerOrdinaryDraft",
      "discardComposerOrdinaryDraft",
      "ComposerDraftScope",
      "ComposerOrdinaryDraft",
    ]) {
      expect(namedImportOwners(composer, symbol)).toEqual([
        "@/lib/composer-ordinary-draft",
      ]);
    }
    expect(namedImportOwners(workbench, "ComposerDraftScope")).toEqual([
      "@/lib/composer-ordinary-draft",
    ]);
    for (const consumer of [composer, page]) {
      expect(namedImportOwners(consumer, "ComposerRecoveryUnavailableError"))
        .toEqual(["@/lib/composer-local-persistence-db"]);
    }
  });

  it("keeps the v1 marker in the ordinary owner without adding a store or recovery writer", () => {
    for (const symbol of [
      "prepareComposerPendingAction",
      "replaceComposerPendingAction",
      "settleComposerPendingAction",
    ]) {
      expect(ordinary).toMatch(
        new RegExp(`export\\s+(?:async\\s+)?function\\s+${symbol}\\b`),
      );
      expect(submission).not.toContain(symbol);
    }
    expect(chatAction).not.toContain("indexedDB");
    expect(chatAction).not.toContain("openComposerLocalPersistenceDB");
    expect(localDatabase).toContain("COMPOSER_LOCAL_DB_VERSION = 5");
    expect(localDatabase).not.toContain("pending_actions");
  });
});

describe("Composer attachment persistence boundary", () => {
  it("canonicalizes browser upload metadata before handoff or persistence", () => {
    const attachment = canonicalizeComposerAttachmentRef({
      fileId: "art_1",
      filename: "x".repeat(240),
      size: 0,
      mime: "",
    });

    expect(attachment).toEqual({
      fileId: "art_1",
      filename: "x".repeat(240),
      size: 0,
      mime: "application/octet-stream",
    });
  });

  it("rejects malformed handoff arrays as one atomic payload", () => {
    expect(canonicalizeComposerAttachmentRefs([{
      fileId: "art_1",
      filename: "valid.pdf",
      size: 12,
      mime: "application/pdf",
    }, {
      fileId: "art_2",
      filename: "invalid.pdf",
      size: -1,
      mime: "application/pdf",
    }])).toBeNull();
  });
});
