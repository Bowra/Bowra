import { describe, expect, it } from "vitest";
import fixture from "@/__tests__/fixtures/document-draft-canvas.json";
import {
  documentCanvasIdentity,
  documentConfirmationId,
  parseDocumentJob,
} from "./document-draft-client";

describe("document Canvas projection and command identity", () => {
  it("reads the delivery fixture without promoting a private file to a publication", () => {
    expect(parseDocumentJob(fixture.review, fixture.context.jobId)).toEqual(
      fixture.review,
    );
    expect(parseDocumentJob(fixture.published, fixture.context.jobId)).toEqual(
      fixture.published,
    );
    expect(
      fixture.review.draft.manifest.changes[0].evidence[0].source_name,
    ).toBeNull();
  });

  it.each([
    { id: "djob_other" },
    { origin_turn_id: null },
    { state: "complete" },
    { draft_digest: "untrusted" },
    { clarification_evidence_sources: [{ source_name: "history.json", source_sha256: "untrusted" }] },
    { clarification_evidence_sources: "history.json" },
    { draft: null },
    { can_confirm: "true" },
    { can_revise: "true" },
    { can_revise: true, state: "published" },
    { can_revise: true, can_confirm: false },
    { draft: { ...fixture.review.draft, files: {} } },
    { usage: { pending: false } },
  ])("rejects an invalid projection %j", (patch) => {
    expect(() =>
      parseDocumentJob({ ...fixture.review, ...patch }, fixture.context.jobId),
    ).toThrow("Invalid document delivery projection");
  });

  it("preserves the same command across reopen and authentication renewal", async () => {
    const reopened = {
      ...fixture.context,
      instanceId: "new-view",
      authSessionGeneration: "new-auth",
    };
    const first = await documentConfirmationId(
      fixture.context,
      fixture.review.draft_digest,
    );
    expect(first).toMatch(/^document-confirm:[a-f0-9]{64}$/);
    expect(
      await documentConfirmationId(reopened, fixture.review.draft_digest),
    ).toBe(first);
    expect(documentCanvasIdentity(reopened)).not.toBe(
      documentCanvasIdentity(fixture.context),
    );
  });

  it.each([true, false])("retains canonical revision availability %s", (canRevise) => {
    const value = { ...fixture.review, can_revise: canRevise };
    expect(parseDocumentJob(value, fixture.context.jobId).can_revise).toBe(canRevise);
  });

  it.each(["userId", "teamId", "workspaceId", "sessionId", "jobId"] as const)(
    "separates commands by %s",
    async (key) => {
      const first = await documentConfirmationId(
        fixture.context,
        fixture.review.draft_digest,
      );
      expect(
        await documentConfirmationId(
          { ...fixture.context, [key]: "different" },
          fixture.review.draft_digest,
        ),
      ).not.toBe(first);
    },
  );

  it("separates reviewed versions", async () => {
    expect(
      await documentConfirmationId(fixture.context, "b".repeat(64)),
    ).not.toBe(
      await documentConfirmationId(
        fixture.context,
        fixture.review.draft_digest,
      ),
    );
  });
});
