import { afterEach, expect, it, vi } from "vitest";
import {
  clearDocumentDismissal,
  documentDismissalIdentity,
  persistDocumentDismissals,
  readDocumentDismissals,
} from "./document-canvas-dismissal";
import type { DocumentCanvasScope } from "./document-canvas-navigation";
import type { DocumentDraftOpen } from "./document-canvas-events";

const scope: DocumentCanvasScope = {
  userId: "usr_a",
  teamId: "team_a",
  workspaceId: "ws_a",
  sessionId: "s_a",
  authSessionGeneration: "auth_a",
};
const pointer: DocumentDraftOpen = {
  version: 1,
  turn_id: "t_a",
  session_id: "s_a",
  user_id: "usr_a",
  team_id: "team_a",
  workspace_id: "ws_a",
  job_id: "djob_a",
  draft_digest: "a".repeat(64),
};
afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

it("bounds tab-local identifiers and evicts the oldest close choice", () => {
  const identities = new Set(
    Array.from({ length: 130 }, (_, index) =>
      documentDismissalIdentity(scope, { ...pointer, job_id: `djob_${index}` }),
    ),
  );
  persistDocumentDismissals(identities);
  const read = readDocumentDismissals();
  expect(read.size).toBe(128);
  expect(
    read.has(
      documentDismissalIdentity(scope, { ...pointer, job_id: "djob_0" }),
    ),
  ).toBe(false);
  expect(
    read.has(
      documentDismissalIdentity(scope, { ...pointer, job_id: "djob_129" }),
    ),
  ).toBe(true);
});

it("clears an explicitly opened job only in its exact authority scope", () => {
  const current = documentDismissalIdentity(scope, pointer);
  const foreign = documentDismissalIdentity(
    { ...scope, userId: "usr_other" },
    pointer,
  );
  const otherJob = documentDismissalIdentity(scope, {
    ...pointer,
    job_id: "djob_other",
  });
  const identities = new Set([current, foreign, otherJob]);
  clearDocumentDismissal(identities, scope, pointer.job_id);
  expect(readDocumentDismissals()).toEqual(new Set([foreign, otherJob]));
});

it("ignores corrupted or blocked browser storage", () => {
  window.sessionStorage.setItem(
    "bowhead.canvas.document.dismissals.v1",
    "malformed",
  );
  expect(readDocumentDismissals().size).toBe(0);
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("Storage unavailable");
  });
  expect(readDocumentDismissals().size).toBe(0);
});
