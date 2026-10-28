import { afterEach, describe, expect, it, vi } from "vitest";
import { assertComposerTextDraftFormat } from "./composer-draft-format";
import { ComposerPersistenceVersionError } from "./composer-local-persistence-db";
import { preparePendingSubmission } from "./composer-submission-recovery";
import {
  prepareComposerPendingAction,
  writeComposerOrdinaryDraft,
} from "./composer-ordinary-draft";
import {
  clearAllComposerTransientDrafts,
  readComposerTransientDraft,
  retainComposerTransientDraft,
} from "./composer-transient-draft";

const scope = {
  userId: "u_1",
  teamId: "team_1",
  workspaceId: "ws_1",
  requestedSessionId: null,
  authSessionGeneration: "login_1",
};

afterEach(() => {
  vi.unstubAllGlobals();
  clearAllComposerTransientDrafts();
});

describe("Composer text-only format boundary", () => {
  it("retains supported legacy text without interpreting slash or at-sign text", () => {
    const draft = { schemaVersion: 1, message: "@助手 /方法 stays text", attachments: [] };
    expect(() => assertComposerTextDraftFormat(draft)).not.toThrow();
    expect(() =>
      assertComposerTextDraftFormat({ message: "legacy", attachments: [] }),
    ).not.toThrow();
    retainComposerTransientDraft("scope", { ...draft, expectedRevision: null });
    expect(readComposerTransientDraft("scope")?.message).toBe(draft.message);
  });

  it.each([
    { schemaVersion: 99 },
    { schemaVersion: 2, input_document: { schema_version: 1, nodes: [] } },
    { input_document: { schema_version: 1, nodes: [] } },
  ])("rejects a newer input before any persistence owner can drop fields: %j", async (format) => {
    const open = vi.fn();
    vi.stubGlobal("indexedDB", { open });
    const draft = { message: "keep reference", attachments: [], ...format };
    await expect(writeComposerOrdinaryDraft(scope, draft, null)).rejects.toBeInstanceOf(
      ComposerPersistenceVersionError,
    );
    await expect(
      prepareComposerPendingAction(
        scope,
        {
          actionId: "00000000-0000-4000-8000-000000000001",
          actionKind: "start",
          attempted: true,
          sourceRevision: "rev_1",
          sessionId: null,
        },
        "rev_1",
        draft,
      ),
    ).rejects.toBeInstanceOf(ComposerPersistenceVersionError);
    await expect(preparePendingSubmission(scope, draft)).rejects.toBeInstanceOf(
      ComposerPersistenceVersionError,
    );
    expect(() =>
      retainComposerTransientDraft("scope", { ...draft, expectedRevision: null }),
    ).toThrow(ComposerPersistenceVersionError);
    expect(readComposerTransientDraft("scope")).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });

  it("rejects an unsupported wire snapshot before writing its recovery fence", async () => {
    const open = vi.fn();
    vi.stubGlobal("indexedDB", { open });
    const wireSnapshot = {
      composerSurface: "home" as const,
      userProfile: null,
      preferredProvider: null,
      preferredModel: null,
      planningMode: null,
      input_document: { schema_version: 1, nodes: [] },
    };
    await expect(
      preparePendingSubmission(scope, { message: "preserve", attachments: [], wireSnapshot }),
    ).rejects.toBeInstanceOf(ComposerPersistenceVersionError);
    expect(open).not.toHaveBeenCalled();
  });
});
