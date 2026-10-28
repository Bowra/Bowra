import { expect, it } from "vitest";
import { captureDocumentRevisionSubmission, type SubmissionRecoveryDraft } from "./composer-submission-recovery";
import { parseDocumentRevisionTarget } from "./composer-document-revision";

const scope = { userId: "u_revision", teamId: "team_revision", workspaceId: "ws_revision", requestedSessionId: "s_review", authSessionGeneration: "auth" };
const target = { kind: "document_revision" as const, version: 1 as const, session_id: "s_review", job_id: "djob_original", draft_digest: "a".repeat(64) };
const input: SubmissionRecoveryDraft = { message: "  Exact edit  ", attachments: [], wireSnapshot: { composerSurface: "workbench", userProfile: null, preferredProvider: null, preferredModel: null, planningMode: "off", documentRevisionTarget: target, sourceDraftRevision: "saved-original" } };

it("captures the exact text, target and source revision without retaining mutable caller objects", () => {
  const draft = structuredClone(input);
  const captured = captureDocumentRevisionSubmission(scope, draft);
  draft.wireSnapshot!.documentRevisionTarget!.job_id = "djob_mutated";
  expect(captured).toMatchObject({ schemaVersion: 3, message: "  Exact edit  ", sourceDraftRevision: "saved-original", wireSnapshot: { documentRevisionTarget: target } });
});

it.each([
  { ...target, extra: true }, { ...target, version: "1" }, { ...target, draft_digest: "A".repeat(64) },
  { ...target, session_id: "other" }, { ...target, job_id: "s_wrong_kind" },
])("rejects malformed target authority: %j", (value) => {
  expect(() => parseDocumentRevisionTarget(value)).toThrow();
});

it.each([
  { schemaVersion: 2 }, { sourceDraftRevision: "different" }, { submissionKind: "steer" as const },
  { replaySourceTurnId: "t_old" }, { expectedParentTurnId: "t_old" }, { expectedExecutionGeneration: 1 },
  { message: " " }, { attachments: [{}] as SubmissionRecoveryDraft["attachments"] },
])("rejects unsupported revision source semantics: %j", (patch) => {
  expect(() => captureDocumentRevisionSubmission(scope, { ...input, ...patch })).toThrow();
});

it.each([
  { planningMode: "planning" as const }, { sourceDraftRevision: "" }, { documentRevisionTarget: undefined },
  { draftChatBinding: { draftId: "draft_other", version: 1, dirty: false } },
  { documentRevisionTarget: { ...target, session_id: "s_other" } },
])("rejects mixed or incomplete wire controls: %j", (patch) => {
  expect(() => captureDocumentRevisionSubmission(scope, { ...input, wireSnapshot: { ...input.wireSnapshot!, ...patch } })).toThrow();
});
