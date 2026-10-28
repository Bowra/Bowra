/**
 * Chat Cards Spec §3: a document task's question carries `purpose: "document_draft"` in its
 * `clarification_required` payload. The hydrated card (F5 / history) must keep it, so the live and
 * replayed card agree; ordinary questions and unknown values carry no purpose.
 */

import { describe, expect, it } from "vitest";

import { resolveHydratedClarificationPending } from "./use-turn-runner";

const BASE = { clarification_id: "clr_doc_purpose", questions: [{ question: "用哪个模板？", options: ["A", "B"], type: "single_select" }] };

describe("hydrated clarification purpose", () => {
  it("keeps the document draft purpose", () => {
    expect(resolveHydratedClarificationPending([{ ...BASE, purpose: "document_draft" }])?.purpose).toBe("document_draft");
  });

  it("leaves ordinary and unknown purposes out", () => {
    expect(resolveHydratedClarificationPending([{ ...BASE, clarification_id: "clr_plain" }])).not.toHaveProperty("purpose");
    expect(resolveHydratedClarificationPending([{ ...BASE, clarification_id: "clr_other", purpose: "other" }])).not.toHaveProperty("purpose");
  });
});
