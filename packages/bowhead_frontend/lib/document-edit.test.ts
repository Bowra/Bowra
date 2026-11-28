import { afterEach, describe, expect, it, vi } from "vitest";

import {
  changeNumbers,
  DocumentEditError,
  editProblem,
  fetchDocumentInventory,
  matchDocumentEdits,
  parseDocumentInventory,
  removeDocumentEdit,
  saveDocumentEdits,
  upsertDocumentEdit,
  type DocumentInventory,
} from "./document-edit";

const TOKEN = "￼";

function inventory(overrides: Partial<DocumentInventory> = {}): DocumentInventory {
  return parseDocumentInventory({
    artifact_id: "art_doc_1",
    version_no: 1,
    edit_mode: "version",
    page: { width: 11906, height: 16838 },
    field_token: TOKEN,
    document: { refusal: null },
    paragraphs: [
      { key: "document:b0", kind: "body", table: null, text: "一、总体情况", heading_level: 1, editable: true, reason: null },
      { key: "document:b1", kind: "body", table: null, text: "一季度销售额 2,905 万元。", heading_level: null, editable: true, reason: null },
      { key: "document:b2.r1.c0.b0", kind: "cell", table: { key: "document:b2", row: 1, col: 0, row_span: 1, col_span: 1 }, text: "华东", heading_level: null, editable: true, reason: null },
      { key: "document:b3", kind: "body", table: null, text: `见第 ${TOKEN} 页`, heading_level: null, editable: false, reason: "field_span" },
    ],
    read_only_parts: { header_footer: 2, note: 1, comment: 0 },
    read_only_paragraphs: [{ key: "header1:b0", part: "header", text: "季度复盘", reason: "header_footer" }, { key: "x", part: "comment", text: "skip" }],
    file: { filename: "复盘.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size: 2048 },
    new_filename: null,
    edited_copies: [],
    ...overrides,
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("document edit client (docx inline edit design §7)", () => {
  it("reads the inventory with its tables, reasons and read-only parts, and fills safe defaults", () => {
    const value = inventory();
    expect(value.paragraphs[2].table).toEqual({ key: "document:b2", row: 1, col: 0, row_span: 1, col_span: 1 });
    expect(value.paragraphs[3]).toMatchObject({ editable: false, reason: "field_span" });
    expect(value.read_only_parts).toEqual({ header_footer: 2, note: 1, comment: 0 });
    expect(value.read_only_paragraphs).toEqual([{ key: "header1:b0", part: "header", text: "季度复盘", reason: "header_footer" }]);
    const bare = parseDocumentInventory({ artifact_id: "a", version_no: 2, edit_mode: "new_file", paragraphs: [{ key: "k", text: "x" }] });
    expect(bare).toMatchObject({ page: { width: 12240, height: 15840 }, document: { refusal: null }, field_token: TOKEN, edited_copies: [] });
    expect(bare.paragraphs[0]).toMatchObject({ kind: "body", editable: false, table: null });
    expect(() => parseDocumentInventory({ artifact_id: "a" })).toThrow(DocumentEditError);
  });

  it("keeps one pending edit per paragraph, drops it when the text is back, and numbers changes in order", () => {
    let edits = upsertDocumentEdit([], { key: "document:b1", expect: "旧", text: "新" });
    edits = upsertDocumentEdit(edits, { key: "document:b0", expect: "标题", text: "新标题" });
    edits = upsertDocumentEdit(edits, { key: "document:b1", expect: "旧", text: "更新" });
    expect(edits).toEqual([{ key: "document:b1", expect: "旧", text: "更新" }, { key: "document:b0", expect: "标题", text: "新标题" }]);
    expect([...changeNumbers(edits)]).toEqual([["document:b1", 1], ["document:b0", 2]]);
    expect(upsertDocumentEdit(edits, { key: "document:b1", expect: "旧", text: "旧" })).toHaveLength(1);
    expect(removeDocumentEdit(edits, "document:b0")).toHaveLength(1);
  });

  it("finds what the save would refuse before sending it", () => {
    expect(editProblem({ key: "k", expect: `见第 ${TOKEN} 页`, text: "见第页" }, TOKEN)).toBe("field_changed");
    expect(editProblem({ key: "k", expect: "a", text: "a\rb" }, TOKEN)).toBe("new_paragraph");
    expect(editProblem({ key: "k", expect: "a", text: "a\nb\tc" }, TOKEN)).toBeNull();
  });

  it("re-applies onto a newer version only where the paragraph did not change", () => {
    const latest = inventory({ paragraphs: [
      { key: "document:b0", kind: "body", table: null, text: "一、总体情况", heading_level: 1, editable: true, reason: null },
      { key: "document:b1", kind: "body", table: null, text: "一季度销售额 3,105 万元。", heading_level: null, editable: true, reason: null },
    ] });
    const rows = matchDocumentEdits([
      { key: "document:b0", expect: "一、总体情况", text: "一、总体" },
      { key: "document:b1", expect: "一季度销售额 2,905 万元。", text: "一季度销售额 2,950 万元。" },
      { key: "document:b9", expect: "x", text: "y" },
    ], latest);
    expect(rows.map((row) => row.applicable)).toEqual([true, false, false]);
    expect(rows[1].rebased).toEqual({ key: "document:b1", expect: "一季度销售额 3,105 万元。", text: "一季度销售额 2,950 万元。" });
    expect(rows[2].rebased).toBeNull();
    const tracked = matchDocumentEdits([{ key: "document:b0", expect: "一、总体情况", text: "x" }], inventory({ document: { refusal: "track_changes_on" } }));
    expect(tracked[0]).toMatchObject({ applicable: false, rebased: null });
  });

  it("calls the routes and reads refusals by edit, by document and by newer version", async () => {
    const replies = [
      new Response(JSON.stringify(inventory()), { status: 200 }),
      new Response(JSON.stringify({ detail: { error: { code: "edit_refused", edits: [{ index: 0, key: "document:b1", code: "not_editable", reason: "tracked_change" }] } } }), { status: 422 }),
      new Response(JSON.stringify({ detail: { error: { code: "document_not_editable", reason: "track_changes_on" } } }), { status: 422 }),
      new Response(JSON.stringify({ detail: { error: { code: "version_target_not_latest", latest: { artifact_id: "art_doc_2", version_no: 2, produced_by: "agent" } } } }), { status: 409, headers: { "x-request-id": "0".repeat(32) } }),
      new Response(JSON.stringify({ artifact_id: "art_doc_2", version_no: 2, filename: "复盘.docx", new_file: false, changes: [{ index: 0, key: "document:b1", before: "a", after: "b" }] }), { status: 201 }),
    ];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => replies.shift() as Response);
    vi.stubGlobal("fetch", fetchMock);
    await fetchDocumentInventory({ sessionId: "s 1", artifactId: "art_doc_1" });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/sessions/s%201/results/document-edit/inventory");
    const edits = [{ key: "document:b1", expect: "a", text: "b" }];
    const refused = await saveDocumentEdits({ sessionId: "s", artifactId: "art_doc_1", edits }).catch((error: unknown) => error as DocumentEditError);
    expect(refused).toMatchObject({ code: "edit_refused", refused: [{ index: 0, key: "document:b1", code: "not_editable", reason: "tracked_change" }] });
    const document = await saveDocumentEdits({ sessionId: "s", artifactId: "art_doc_1", edits }).catch((error: unknown) => error as DocumentEditError);
    expect(document).toMatchObject({ code: "document_not_editable", reason: "track_changes_on" });
    const conflict = await saveDocumentEdits({ sessionId: "s", artifactId: "art_doc_1", edits }).catch((error: unknown) => error as DocumentEditError);
    expect(conflict).toMatchObject({ code: "version_target_not_latest", latest: { artifact_id: "art_doc_2", version_no: 2 } });
    expect((conflict as DocumentEditError).diagnostics).toMatchObject({ status: 409, requestId: "0".repeat(32) });
    const saved = await saveDocumentEdits({ sessionId: "s", artifactId: "art_doc_1", edits });
    expect(saved).toEqual({ artifact_id: "art_doc_2", version_no: 2, filename: "复盘.docx", new_file: false, changes: [{ index: 0, key: "document:b1", before: "a", after: "b" }] });
  });
});
