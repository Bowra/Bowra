import { describe, expect, it } from "vitest";

import { canonicalizeComposerAttachmentRef } from "./composer-attachment-refs";
import { canonicalizePresentationSelection, presentationSlideSelection } from "./presentation-selection";

const selection = {
  version: 1, artifactId: "art_deck", slideId: 256, slideIndex: 3, elementKey: "256:7", elementName: "季度销售额", elementKind: "table",
};
const ref = { fileId: "art_deck", filename: "deck.pptx", mime: "application/pdf", size: 10, referenceKind: "result", presentationSelection: selection };

describe("presentation element references", () => {
  it("keeps the closed wire through drafts, Send and history", () => {
    expect(canonicalizePresentationSelection(selection)).toEqual(selection);
    expect(canonicalizeComposerAttachmentRef(ref)).toEqual(ref);
    expect(canonicalizeComposerAttachmentRef(JSON.parse(JSON.stringify(ref)))).toEqual(ref);
  });

  it("refuses an open, oversized or mismatched selection", () => {
    expect(canonicalizePresentationSelection({ ...selection, extra: true })).toBeNull();
    expect(canonicalizePresentationSelection({ ...selection, elementKey: "257:7" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...selection, elementKind: "video" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...selection, artifactId: "a".repeat(128), elementName: "😀".repeat(200) })).toBeNull();
    expect(canonicalizeComposerAttachmentRef({ ...ref, referenceKind: "upload" })).toBeNull();
    expect(canonicalizeComposerAttachmentRef({ ...ref, fileId: "art_other" })).toBeNull();
  });

  it("names a whole slide without the element fields, and refuses a partial element", () => {
    const slide = { version: 1, artifactId: "art_deck", slideIndex: 3 };
    expect(canonicalizePresentationSelection(slide)).toEqual(slide);
    expect(presentationSlideSelection("art_deck", 3, 256)).toEqual({ ...slide, slideId: 256 });
    expect(canonicalizeComposerAttachmentRef({ ...ref, presentationSelection: slide })).toEqual({ ...ref, presentationSelection: slide });
    expect(canonicalizePresentationSelection({ ...slide, slideId: 256, elementKey: "256:7" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...slide, elementKey: "256:7", elementName: "表", elementKind: "table" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...slide, slideIndex: 65 })).toBeNull();
  });
});
