import { expect, it } from "vitest";
import { captureContextMaintenanceSubmission, type SubmissionRecoveryDraft } from "./composer-submission-recovery";
import { assertComposerTextDraftFormat } from "./composer-draft-format";
import { parseContextMaintenanceAction } from "./context-maintenance";
import { snapshotArchivedTurn } from "./use-turn-runner";
import type { TurnRunnerState } from "./types";

const scope = { userId: "u_context", teamId: "team_context", workspaceId: "ws_context", requestedSessionId: "s_context", authSessionGeneration: "auth" };
const action = { contract: "context-maintenance.v1" as const, expected_generation: `mcg_${"a".repeat(32)}` };
const draft: SubmissionRecoveryDraft = { message: "", attachments: [], wireSnapshot: {
  composerSurface: "workbench", userProfile: null, preferredProvider: null, preferredModel: null, planningMode: "off", contextMaintenance: action,
} };

it("retains an immutable maintenance operation without manufacturing a question", () => {
  const source = structuredClone(draft);
  const captured = captureContextMaintenanceSubmission(scope, source);
  source.wireSnapshot!.contextMaintenance!.expected_generation = `mcg_${"b".repeat(32)}`;
  expect(captured).toMatchObject({ schemaVersion: 4, message: "", attachments: [], wireSnapshot: { contextMaintenance: action } });
  expect(() => assertComposerTextDraftFormat(captured)).toThrow();
  expect(() => assertComposerTextDraftFormat(captured.wireSnapshot!)).toThrow();
});

it.each([{ message: "continue" }, { submissionKind: "replay" as const }, { expectedParentTurnId: "t_old" }, { replaySourceTurnId: "t_old" }])(
  "refuses business input or continuation mixed with maintenance: %j", (patch) => {
    expect(() => captureContextMaintenanceSubmission(scope, { ...draft, ...patch })).toThrow();
  },
);

it.each([{ ...action, extra: true }, { ...action, contract: "context-maintenance.v2" }, { ...action, expected_generation: "t_old" }])(
  "does not accept a different action contract: %j", (value) => expect(parseContextMaintenanceAction(value)).toBeNull(),
);

it("archives a maintenance status even when it contains no question, answer or Tool", () => {
  const archived = snapshotArchivedTurn({ turnId: "t_maintenance", operationKind: "context_maintenance", user: "", answer: "", userAttachments: [], traceSteps: [], terminalStatus: "completed" } as unknown as TurnRunnerState);
  expect(archived).toMatchObject({ operationKind: "context_maintenance", user: "", answer: "", turnId: "t_maintenance" });
});

it("accepts explicit absence of a generation for an old conversation but rejects an omitted fence", () => {
  expect(parseContextMaintenanceAction({ ...action, expected_generation: null })).toEqual({ ...action, expected_generation: null });
  expect(parseContextMaintenanceAction({ contract: action.contract })).toBeNull();
});
