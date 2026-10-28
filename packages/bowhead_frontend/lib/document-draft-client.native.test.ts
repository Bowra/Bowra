import { readFileSync } from "node:fs";
import { expect, it } from "vitest";
import { parseDocumentJob } from "./document-draft-client";

const nativeReviewPath = process.env.BOWHEAD_DOCUMENT_NATIVE_REVIEW_JSON;

it.skipIf(!nativeReviewPath)("reads the original HTTP projection from native same-Turn document execution", () => {
  const source = JSON.parse(readFileSync(nativeReviewPath!, "utf8"));
  const review = parseDocumentJob(source, source.id);
  expect(review.state).toBe("review");
  expect(review.can_confirm).toBe(true);
  expect(review.draft_digest).toMatch(/^[a-f0-9]{64}$/);
  expect(review).toEqual(source);
});
