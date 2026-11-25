import { describe, expect, it } from "vitest";

import { canonicalizeComposerAttachmentRef } from "./composer-attachment-refs";
import { canonicalizePresentationSelection, MAX_REFERENCED_SLIDES, presentationSlideSelection, referencedSlides, toggleSlide } from "./presentation-selection";

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
    // Version 1 slides stay readable in stored messages and drafts; new page references use version 2.
    expect(canonicalizePresentationSelection(slide)).toEqual(slide);
    expect(presentationSlideSelection("art_deck", 3, 256)).toEqual({ version: 2, artifactId: "art_deck", slides: [{ slideIndex: 3, slideId: 256 }] });
    expect(canonicalizeComposerAttachmentRef({ ...ref, presentationSelection: slide })).toEqual({ ...ref, presentationSelection: slide });
    expect(canonicalizePresentationSelection({ ...slide, slideId: 256, elementKey: "256:7" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...slide, elementKey: "256:7", elementName: "表", elementKind: "table" })).toBeNull();
    expect(canonicalizePresentationSelection({ ...slide, slideIndex: 65 })).toBeNull();
  });

  it("keeps several pages of one version in page order, at most ten (design §13.6)", () => {
    const pages = { version: 2, artifactId: "art_deck", slides: [{ slideIndex: 2 }, { slideIndex: 5, slideId: 260 }] };
    expect(canonicalizePresentationSelection(pages)).toEqual(pages);
    expect(canonicalizeComposerAttachmentRef({ ...ref, presentationSelection: pages })).toEqual({ ...ref, presentationSelection: pages });
    expect(canonicalizePresentationSelection({ ...pages, slides: [{ slideIndex: 5 }, { slideIndex: 2 }] })).toBeNull();
    expect(canonicalizePresentationSelection({ ...pages, slides: [{ slideIndex: 2 }, { slideIndex: 2 }] })).toBeNull();
    expect(canonicalizePresentationSelection({ ...pages, slides: [] })).toBeNull();
    expect(canonicalizePresentationSelection({ ...pages, slides: [{ slideIndex: 2, elementKey: "256:2" }] })).toBeNull();
    expect(canonicalizePresentationSelection({ ...pages, slides: Array.from({ length: 11 }, (_, i) => ({ slideIndex: i + 1 })) })).toBeNull();
    expect(referencedSlides(pages as never)).toEqual([2, 5]);
    expect(referencedSlides(selection as never)).toEqual([]);
  });

  it("toggles a page in the file's set, replaces an element reference, and stops at ten", () => {
    let current = toggleSlide(undefined, "art_deck", 5);
    expect(current).toEqual({ selection: { version: 2, artifactId: "art_deck", slides: [{ slideIndex: 5 }] }, outcome: "added" });
    current = toggleSlide(current.selection, "art_deck", 2);
    expect(referencedSlides(current.selection)).toEqual([2, 5]);
    current = toggleSlide(current.selection, "art_deck", 5);
    expect(current.outcome).toBe("removed");
    expect(toggleSlide(current.selection, "art_deck", 2)).toEqual({ selection: null, outcome: "removed" });
    // A version 1 slide from a saved draft joins the set; an element of the file is replaced.
    expect(referencedSlides(toggleSlide({ version: 1, artifactId: "art_deck", slideIndex: 3 }, "art_deck", 1).selection)).toEqual([1, 3]);
    expect(referencedSlides(toggleSlide(selection as never, "art_deck", 1).selection)).toEqual([1]);
    let full = toggleSlide(undefined, "art_deck", 1).selection;
    for (let page = 2; page <= MAX_REFERENCED_SLIDES; page += 1) full = toggleSlide(full, "art_deck", page).selection;
    expect(toggleSlide(full, "art_deck", 11)).toEqual({ selection: full, outcome: "limit" });
  });
});
