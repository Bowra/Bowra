import { describe, expect, it } from "vitest";

import type { RecoveredReferenceSubmission } from "./composer-submission-recovery";
import {
  referencePreviewAfterSendSettled,
  referencePreviewForPendingSend,
  referencePreviewForRecoveredSubmission,
  referencePreviewVisible,
} from "./reference-submission-preview";

const SCOPE = "scope-home";

function draft(clientSubmissionId: string): RecoveredReferenceSubmission {
  return {
    clientSubmissionId,
    contentChecksum: "checksum",
    createdAt: "2026-11-16T08:00:00.000Z",
    message: "/Analysis framework 测试一下",
    attachments: [],
    schemaVersion: 2,
    input_document: { schema_version: 1, nodes: [{ type: "text", text: "测试一下" }] },
  } as RecoveredReferenceSubmission;
}

describe("reference submission preview", () => {
  it("hides the local copy while a normal send is in flight", () => {
    const pending = referencePreviewForPendingSend(null, SCOPE, draft("cs_1"));
    expect(pending.inFlight).toBe(true);
    expect(referencePreviewVisible(pending, SCOPE)).toBe(false);
  });

  it("shows the copy once a send settles without clearing it", () => {
    // A failed, interrupted or unconfirmed send leaves its copy behind.
    const settled = referencePreviewAfterSendSettled(referencePreviewForPendingSend(null, SCOPE, draft("cs_1")));
    expect(settled?.inFlight).toBe(false);
    expect(referencePreviewVisible(settled, SCOPE)).toBe(true);
  });

  it("keeps an accepted, cleared send hidden after it settles", () => {
    expect(referencePreviewAfterSendSettled(null)).toBeNull();
  });

  it("shows a copy recovered from browser storage at once", () => {
    const recovered = referencePreviewForRecoveredSubmission(SCOPE, draft("cs_1"));
    expect(referencePreviewVisible(recovered, SCOPE)).toBe(true);
    // Settling another attempt does not hide it.
    expect(referencePreviewAfterSendSettled(recovered)).toBe(recovered);
  });

  it("keeps a recovery copy visible while the same submission is resent", () => {
    const recovered = referencePreviewForRecoveredSubmission(SCOPE, draft("cs_1"));
    const resent = referencePreviewForPendingSend(recovered, SCOPE, draft("cs_1"));
    expect(referencePreviewVisible(resent, SCOPE)).toBe(true);
  });

  it("hides a new submission even when an older recovery copy was shown", () => {
    const recovered = referencePreviewForRecoveredSubmission(SCOPE, draft("cs_1"));
    expect(referencePreviewVisible(referencePreviewForPendingSend(recovered, SCOPE, draft("cs_2")), SCOPE)).toBe(false);
    expect(referencePreviewVisible(referencePreviewForPendingSend(recovered, "scope-other", draft("cs_1")), "scope-other")).toBe(false);
  });

  it("never shows a copy in another composer scope", () => {
    const recovered = referencePreviewForRecoveredSubmission(SCOPE, draft("cs_1"));
    expect(referencePreviewVisible(recovered, "scope-session")).toBe(false);
    expect(referencePreviewVisible(null, SCOPE)).toBe(false);
  });
});
