import { describe, expect, it } from "vitest";

import { canonicalizeComposerAttachmentRef } from "./composer-attachment-refs";
import {
  canonicalizeDocumentSelection,
  documentParagraphSelection,
  MAX_REFERENCED_PAGES,
  referencedDocumentPages,
  toggleDocumentPage,
} from "./document-selection";

describe("Word references in chat (docx inline edit design §7.4)", () => {
  it("accepts a paragraph or 1–10 ascending pages and nothing else", () => {
    expect(documentParagraphSelection("art_doc", "document:b2.r1.c0.b0", "一、总体情况 · 表格第 2 行第 1 列")).toEqual({
      version: 1, artifactId: "art_doc", paragraphKey: "document:b2.r1.c0.b0", paragraphName: "一、总体情况 · 表格第 2 行第 1 列",
    });
    expect(canonicalizeDocumentSelection({ version: 1, artifactId: "art_doc", paragraphKey: "header1:b0" })).toBeNull();
    expect(canonicalizeDocumentSelection({ version: 1, artifactId: "art_doc", paragraphKey: "document:b1", extra: 1 })).toBeNull();
    expect(canonicalizeDocumentSelection({ version: 2, artifactId: "art_doc", pages: [{ page: 2 }, { page: 5 }] })).toEqual({
      version: 2, artifactId: "art_doc", pages: [{ page: 2 }, { page: 5 }],
    });
    expect(canonicalizeDocumentSelection({ version: 2, artifactId: "art_doc", pages: [{ page: 5 }, { page: 2 }] })).toBeNull();
    expect(canonicalizeDocumentSelection({ version: 2, artifactId: "art_doc", pages: [] })).toBeNull();
    expect(canonicalizeDocumentSelection({ version: 2, artifactId: "art_doc", pages: [{ page: 301 }] })).toBeNull();
  });

  it("toggles pages in one set per file, and a page replaces a paragraph", () => {
    let state = toggleDocumentPage(undefined, "art_doc", 3);
    expect(state).toEqual({ selection: { version: 2, artifactId: "art_doc", pages: [{ page: 3 }] }, outcome: "added" });
    state = toggleDocumentPage(state.selection, "art_doc", 1);
    expect(referencedDocumentPages(state.selection)).toEqual([1, 3]);
    state = toggleDocumentPage(state.selection, "art_doc", 3);
    expect(state.outcome).toBe("removed");
    expect(toggleDocumentPage(state.selection, "art_doc", 1).selection).toBeNull();
    const paragraph = documentParagraphSelection("art_doc", "document:b1");
    expect(toggleDocumentPage(paragraph, "art_doc", 4).selection).toEqual({ version: 2, artifactId: "art_doc", pages: [{ page: 4 }] });
    const full = { version: 2 as const, artifactId: "art_doc", pages: Array.from({ length: MAX_REFERENCED_PAGES }, (_, index) => ({ page: index + 1 })) };
    expect(toggleDocumentPage(full, "art_doc", 20).outcome).toBe("limit");
  });

  it("travels on a result reference only, alone, and for its own file", () => {
    const base = { fileId: "art_doc", filename: "复盘.docx", size: 10, mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", referenceKind: "result" };
    const documentSelection = { version: 2, artifactId: "art_doc", pages: [{ page: 2 }] };
    expect(canonicalizeComposerAttachmentRef({ ...base, documentSelection })?.documentSelection).toEqual(documentSelection);
    expect(canonicalizeComposerAttachmentRef({ ...base, referenceKind: "upload", documentSelection })).toBeNull();
    expect(canonicalizeComposerAttachmentRef({ ...base, documentSelection: { ...documentSelection, artifactId: "art_other" } })).toBeNull();
    expect(canonicalizeComposerAttachmentRef({
      ...base, documentSelection, presentationSelection: { version: 2, artifactId: "art_doc", slides: [{ slideIndex: 1 }] },
    })).toBeNull();
  });
});
