import { expect, it } from "vitest";
import { documentCanvasTitle, documentSourceExtension, type DocumentJob } from "./document-drafts";

const job = (stem: string | null, files: string[] = ["candidate.pptx", "candidate.pdf"]) => ({
  id: "djob_1", state: "review", origin_turn_id: "t_1", request_text: "", can_confirm: true,
  draft: stem === null ? undefined : {
    output_stem: stem,
    files: Object.fromEntries(files.map((name) => [name, { artifact_id: `art_${name}`, sha256: "a" }])),
    manifest: { changes: [], retained_unsupported_text: [] },
    preview: { pages: [], declared_font_fallbacks: [] },
  },
}) as unknown as DocumentJob;

it("names a document draft after its source file, extension included", () => {
  expect(documentCanvasTitle(job("Q2 运营复盘"), "模板演示文稿")).toBe("Q2 运营复盘.pptx");
  expect(documentCanvasTitle(job("Q2 运营复盘.pptx"), "模板演示文稿")).toBe("Q2 运营复盘.pptx");
  expect(documentCanvasTitle(job("Report", ["candidate.docx", "candidate.pdf"]), "Draft")).toBe("Report.docx");
  expect(documentCanvasTitle(job(null), "模板演示文稿")).toBe("模板演示文稿");
  expect(documentSourceExtension(job("Only PDF", ["candidate.pdf"]))).toBeNull();
});
