import { describe, expect, it } from "vitest";
import fixture from "@/__tests__/fixtures/document-word-canvas.json";
import { parseDocumentJob } from "./document-draft-client";
import { documentCanvasTitle, documentDraftSourceName } from "./document-drafts";

describe("actual synthetic Word delivery projections", () => {
  it.each([fixture.source, fixture.source_pdf])("reads source-only and paired private review/publication", (value) => {
    for (const raw of [value.review, value.published]) {
      const job = parseDocumentJob(raw, value.context.jobId);
      expect(documentDraftSourceName(job)).toBe("candidate.docx");
      expect(documentCanvasTitle(job, "Document")).toBe("report - Updated.docx");
      expect(job.draft?.manifest.comparison).toBe("source_preserving_word_text");
      expect(job.draft?.requires_full_review).toBe(true);
    }
  });

  it.each([
    { descriptor: { ...fixture.source.review.draft.descriptor, source_name: "candidate.pptx" } },
    { descriptor: { ...fixture.source.review.draft.descriptor, published_outputs: ["pdf"] } },
    { descriptor: { ...fixture.source.review.draft.descriptor, source_name: "../../report.docx" } },
    { manifest: { ...fixture.source.review.draft.manifest, schema_version: 2 } },
    { manifest: { ...fixture.source.review.draft.manifest, comparison: "observed_object_facts" } },
    { manifest: { ...fixture.source.review.draft.manifest, candidate_sha256: "0".repeat(64) } },
    { manifest: { ...fixture.source.review.draft.manifest, changes: [{ ...fixture.source.review.draft.manifest.changes[0], slide: 1 }] } },
    { manifest: { ...fixture.source.review.draft.manifest, changes: [{ ...fixture.source.review.draft.manifest.changes[0], location: { ...fixture.source.review.draft.manifest.changes[0].location, sections: [0] } }] } },
    { preview: { ...fixture.source.review.draft.preview, text_and_geometry: "passed" } },
    { preview: { ...fixture.source.review.draft.preview, native_word_certified: true } },
    { requires_full_review: false },
  ])("rejects a mismatched format, source identity or review claim", (patch) => {
    const value = { ...fixture.source.review, draft: { ...fixture.source.review.draft, ...patch } };
    expect(() => parseDocumentJob(value, value.id)).toThrow("Invalid document delivery projection");
  });
});
