import { afterEach, describe, expect, it, vi } from "vitest";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import {
  composerReferenceHandoffKey,
  readComposerReferenceHandoff,
  type ComposerReferenceSnapshot,
} from "./composer-reference-transfer";
import { composerDraftScopeSignature } from "./composer-persistence-scope";
import { ComposerPersistenceVersionError } from "./composer-local-persistence-db";
import {
  clearAllComposerTransientDrafts,
  clearComposerTransientDraft,
  readComposerReferenceTransientDraft,
  readComposerTransientDraft,
  retainComposerReferenceTransientDraft,
  retainComposerTransientDraft,
} from "./composer-transient-draft";
import { takeComposerPrefill, COMPOSER_PREFILL_KEY } from "./composer-handoff";

const fixture = fixtures.valid.find((item) => item.name === "exact_agent_and_parameterized_skill")!;
const input = (): ComposerReferenceSnapshot => ({
  schemaVersion: 2,
  message: fixture.display_text!,
  input_document: structuredClone(
    fixture.normalized,
  ) as ComposerReferenceSnapshot["input_document"],
  attachments: [
    {
      fileId: "file_1",
      filename: "analysis.csv",
      mime: "text/csv",
      size: 42,
      referenceKind: "result",
      bundleArtifactIds: ["art_1"],
    },
  ],
});
const scope = {
  userId: "u1",
  teamId: "team1",
  workspaceId: "ws1",
  requestedSessionId: null as string | null,
  authSessionGeneration: "login1",
};
afterEach(() => {
  sessionStorage.clear();
  clearAllComposerTransientDrafts();
  vi.restoreAllMocks();
});

describe("scoped reference handoff reader", () => {
  it("restores the complete envelope repeatedly without taking the one-shot text channel", async () => {
    const snapshot = input();
    const key = await composerReferenceHandoffKey(scope);
    const raw = JSON.stringify(snapshot);
    sessionStorage.setItem(key, raw);
    sessionStorage.setItem(COMPOSER_PREFILL_KEY, "/literal @text");
    expect(takeComposerPrefill()).toBe("/literal @text");
    const restored = await readComposerReferenceHandoff(scope);
    expect(restored).toEqual(snapshot);
    restored!.attachments[0].bundleArtifactIds!.push("mutated");
    expect(await readComposerReferenceHandoff(scope)).toEqual(snapshot);
    expect(sessionStorage.getItem(key)).toBe(raw);
  });
  it.each([
    "userId",
    "teamId",
    "workspaceId",
    "requestedSessionId",
    "authSessionGeneration",
  ] as const)("does not cross %s", async (field) => {
    const key = await composerReferenceHandoffKey(scope);
    sessionStorage.setItem(key, JSON.stringify(input()));
    expect(await readComposerReferenceHandoff({ ...scope, [field]: "other" })).toBeNull();
    expect(sessionStorage.getItem(key)).not.toBeNull();
  });
  it.each([
    "{broken",
    JSON.stringify({ ...input(), schemaVersion: 99 }),
    JSON.stringify({ ...input(), message: "lost labels" }),
    JSON.stringify({ ...input(), pendingAction: { attempted: true } }),
    JSON.stringify({ ...input(), attachments: [{ fileId: "missing fields" }] }),
    JSON.stringify({ ...input(), input_document: { schema_version: 99, nodes: [] } }),
    "x".repeat(1_000_001),
  ])("preserves unreadable or unsupported handoff bytes", async (raw) => {
    const key = await composerReferenceHandoffKey(scope);
    sessionStorage.setItem(key, raw);
    await expect(readComposerReferenceHandoff(scope)).rejects.toBeInstanceOf(
      ComposerPersistenceVersionError,
    );
    expect(sessionStorage.getItem(key)).toBe(raw);
  });
});

describe("reference transient snapshots", () => {
  it("retains detached nodes, parameters, typed attachments and CAS revision", () => {
    const signature = composerDraftScopeSignature(scope);
    const snapshot = { ...input(), expectedRevision: "rev1" };
    const original = structuredClone(snapshot);
    retainComposerReferenceTransientDraft(signature, snapshot);
    snapshot.attachments[0].bundleArtifactIds!.push("caller-change");
    snapshot.input_document.nodes.length = 0;
    const restored = readComposerReferenceTransientDraft(signature)!;
    expect(restored).toEqual(original);
    restored.input_document.nodes.length = 0;
    expect(readComposerReferenceTransientDraft(signature)).toEqual(original);
    expect(
      readComposerReferenceTransientDraft(
        composerDraftScopeSignature({ ...scope, workspaceId: "other" }),
      ),
    ).toBeNull();
    expect(() => readComposerTransientDraft(signature)).toThrow(ComposerPersistenceVersionError);
    expect(() =>
      retainComposerTransientDraft(signature, {
        message: "overwrite",
        attachments: [],
        expectedRevision: "rev1",
      }),
    ).toThrow(ComposerPersistenceVersionError);
    clearComposerTransientDraft(signature, {
      message: original.message,
      attachments: original.attachments,
      expectedRevision: "rev1",
    });
    expect(readComposerReferenceTransientDraft(signature)).toEqual(original);
  });
  it("keeps legacy transient text and attachments detached too", () => {
    const snapshot = {
      message: "/literal @text",
      attachments: input().attachments,
      expectedRevision: null,
    };
    retainComposerTransientDraft("legacy", snapshot);
    snapshot.message = "changed";
    expect(readComposerTransientDraft("legacy")!.message).toBe("/literal @text");
    const restored = readComposerTransientDraft("legacy")!;
    restored.attachments.length = 0;
    expect(readComposerTransientDraft("legacy")!.attachments).toHaveLength(1);
    clearComposerTransientDraft("legacy", readComposerTransientDraft("legacy")!);
    expect(readComposerTransientDraft("legacy")).toBeNull();
  });
  it("does not replace a preserved snapshot with malformed reference input", () => {
    const snapshot = { ...input(), expectedRevision: "rev1" };
    retainComposerReferenceTransientDraft("scope", snapshot);
    expect(() =>
      retainComposerReferenceTransientDraft("scope", {
        ...snapshot,
        expectedRevision: undefined,
      } as unknown as typeof snapshot),
    ).toThrow(ComposerPersistenceVersionError);
    expect(readComposerReferenceTransientDraft("scope")).toEqual(snapshot);
  });
  it("preserves the previous text snapshot when copying a replacement fails", () => {
    const snapshot = { message: "preserved", attachments: [], expectedRevision: "rev1" };
    retainComposerTransientDraft("scope", snapshot);
    const invalid = { ...snapshot, runtimeOnly: () => undefined };
    expect(() => retainComposerTransientDraft("scope", invalid)).toThrow();
    expect(readComposerTransientDraft("scope")).toEqual(snapshot);
  });
});
