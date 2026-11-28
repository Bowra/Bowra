import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DocumentInventory } from "@/lib/document-edit";
import type { DocumentLayout } from "@/lib/document-layout";
import { DocumentEditor } from "./document-editor";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  useLocale: () => "zh-CN",
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));
vi.mock("../presentation-edit/pdf-page", () => ({
  LazyPdfPageCanvas: ({ label }: { label?: string }) => <div data-testid="page-canvas" aria-label={label} />,
}));

const { layoutRef } = vi.hoisted(() => ({ layoutRef: { current: null as DocumentLayout | null } }));
vi.mock("./use-document-layout", () => ({
  useDocumentPages: (id: string | null) => ({ pdf: id ? { numPages: 2 } : null, data: id ? new ArrayBuffer(4) : null, state: id ? "ready" : "loading", reload: vi.fn() }),
  useDocumentLayout: (pdf: unknown) => (pdf ? layoutRef.current : null),
}));

const line = (page: number, top: number, x1 = 400) => ({ page, x0: 72, x1, top, bottom: top + 12, fontSize: 10.5 });

function inventory(overrides: Partial<DocumentInventory> = {}): DocumentInventory {
  return {
    artifact_id: "art_doc_1", version_no: 3, edit_mode: "version", page: { width: 11906, height: 16838 }, field_token: "￼",
    document: { refusal: null },
    paragraphs: [
      { key: "document:b0", kind: "body", table: null, text: "一、总体情况", heading_level: 1, editable: true, reason: null },
      { key: "document:b1", kind: "body", table: null, text: "销售额 2,905 万元。", heading_level: null, editable: true, reason: null },
      { key: "document:b2", kind: "body", table: null, text: "推迟到二季度签订。", heading_level: null, editable: true, reason: null },
      { key: "document:b3", kind: "body", table: null, text: "带修订的段落", heading_level: null, editable: false, reason: "tracked_change" },
      { key: "document:b4", kind: "body", table: null, text: "页面上找不到的段落", heading_level: null, editable: true, reason: null },
    ],
    read_only_parts: { header_footer: 1, note: 0, comment: 0 },
    read_only_paragraphs: [{ key: "header1:b0", part: "header", text: "季度复盘报告", reason: "header_footer" }],
    file: { filename: "复盘.docx", mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size: 2048 },
    new_filename: null, edited_copies: [], drops_on_save: [],
    ...overrides,
  };
}

function layout(): DocumentLayout {
  return {
    pages: [{ width: 595, height: 842 }, { width: 595, height: 842 }],
    paragraphs: new Map([
      ["document:b0", { status: "exact", lines: [line(0, 100, 200)], room: 300 }],
      ["document:b1", { status: "exact", lines: [line(0, 130)], room: 100 }],
      ["document:b2", { status: "exact", lines: [line(0, 160, 520)], room: 0 }],
      ["document:b3", { status: "exact", lines: [line(1, 100)], room: 50 }],
      ["document:b4", { status: "missing", lines: [], room: null }],
    ]),
    missing: ["document:b4"],
    readOnly: new Map([["header1:b0", [line(0, 40, 300), line(1, 40, 300)]]]),
  };
}

function stubServer(routes: Record<string, Array<(body: unknown) => { status: number; body: unknown }>>) {
  const calls: Array<{ path: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) as unknown : null;
    calls.push({ path, body });
    const entry = Object.entries(routes).find(([suffix]) => path.endsWith(suffix));
    const route = entry?.[1].shift();
    if (!route) return new Response("{}", { status: 404 });
    const result = route(body);
    return new Response(JSON.stringify(result.body), { status: result.status });
  }));
  return calls;
}

function renderEditor(props: Partial<Parameters<typeof DocumentEditor>[0]> = {}) {
  const onExit = vi.fn();
  render(
    <div role="dialog">
      <DocumentEditor sessionId="s_1" artifactId="art_doc_1" filename="复盘.docx" downloadHref="/api/v1/files/art_doc_1" onExit={onExit} {...props} />
    </div>,
  );
  return onExit;
}

beforeEach(() => {
  layoutRef.current = layout();
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 1400 });
  window.localStorage.setItem("bowhead.documentEdit.guideDismissed", "1");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("DocumentEditor (docx inline edit design §8; canvas W1–W10)", () => {
  it("edits a paragraph where it is drawn and saves it as the next version from the top bar", async () => {
    const calls = stubServer({
      "/document-edit/inventory": [() => ({ status: 200, body: inventory() })],
      "/document-edits": [() => ({ status: 201, body: { artifact_id: "art_doc_2", version_no: 4, filename: "复盘.docx", new_file: false, changes: [] } })],
    });
    renderEditor();
    const hit = await screen.findByRole("button", { name: /kind_body.*销售额 2,905 万元/ });
    fireEvent.click(hit);
    // The selection keeps the paragraph's field focused, hidden, so typing lands at once (canvas W1).
    const field = await screen.findByRole("textbox", { name: /kind_body/ });
    expect(field).toHaveAttribute("data-armed", "true");
    await waitFor(() => expect(field).toHaveFocus());
    fireEvent.keyDown(field, { key: "Enter" });
    expect(field).not.toHaveAttribute("data-armed");
    fireEvent.change(field, { target: { value: "销售额 3,105 万元。" } });
    fireEvent.keyDown(field, { key: "Enter" });
    expect(field).toHaveAttribute("data-armed", "true");
    expect(screen.getByText(/pending_count:\{"count":1\}/)).toBeInTheDocument();
    // A short change fits the last line: it is painted on the page.
    expect(document.querySelector("[data-painted='paragraph']")).toHaveTextContent("销售额 3,105 万元。");

    fireEvent.click(screen.getByRole("button", { name: "save_version" }));
    const again = await screen.findByRole("button", { name: "edit_again" });
    await waitFor(() => expect(again).toHaveFocus());
    expect(calls.find((call) => call.path.endsWith("/document-edits"))?.body).toEqual({
      artifact_id: "art_doc_1",
      edits: [{ key: "document:b1", expect: "销售额 2,905 万元。", text: "销售额 3,105 万元。" }],
    });
  });

  it("keeps an edit that adds a line pending, with its new text in a card in the margin (canvas W2)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    renderEditor();
    const hit = await screen.findByRole("button", { name: /推迟到二季度签订/ });
    fireEvent.click(hit);
    const field = await screen.findByRole("textbox", { name: /kind_body/ });
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.change(field, { target: { value: "推迟到二季度签订，预计 5 月补回约 120 万元，全年目标不变。" } });
    fireEvent.keyDown(field, { key: "Escape" });
    const card = screen.getByRole("note", { name: 'pending_badge:{"number":1}' });
    expect(card).toHaveTextContent("预计 5 月补回约 120 万元");
    expect(card).toHaveTextContent("pending_note");
    expect(document.querySelector("[data-painted='paragraph']")).toBeNull();
    expect(screen.getByRole("group", { name: /selection_group/ })).toHaveTextContent('pending_badge_line:{"number":1}');
  });

  it("says why content cannot be edited here: a tracked change, and a header found on every page (canvas W7)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    renderEditor();
    fireEvent.click(await screen.findByRole("button", { name: /带修订的段落/ }));
    const group = screen.getByRole("group", { name: /selection_group/ });
    expect(group).toHaveTextContent("reason_tracked_change");
    expect(within(group).getByRole("button", { name: "download_file" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    const headers = document.querySelectorAll("[data-hit-key='header1:b0']");
    expect([...headers].map((hit) => hit.closest("[data-page]")?.getAttribute("data-page"))).toEqual(["1", "2"]);
    expect(headers[0]).toHaveAttribute("data-locked", "true");
  });

  it("edits a paragraph that was not found on the pages in 本页内容 (canvas W6)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    renderEditor();
    fireEvent.click(await screen.findByRole("button", { name: "missing_open" }));
    const panel = screen.getByRole("complementary", { name: /panel_items/ });
    const field = within(panel).getByRole("textbox", { name: /页面上找不到的段落|paragraph_place/ });
    fireEvent.change(field, { target: { value: "在面板里改好的段落" } });
    expect(screen.getByText(/pending_count:\{"count":1\}/)).toBeInTheDocument();
  });

  it("explains a document that refuses edits and offers download first (canvas W8)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory({ document: { refusal: "track_changes_on" } }) })] });
    const onReference = vi.fn();
    const onExit = renderEditor({ onReference });
    const dialog = await screen.findByRole("alertdialog", { name: "blocked_title_track_changes_on" });
    const buttons = within(dialog).getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("download_file");
    fireEvent.click(within(dialog).getByRole("button", { name: "reference_file" }));
    expect(onExit).toHaveBeenCalledWith(expect.objectContaining({ artifactId: "art_doc_1", close: true, then: onReference }));
    expect(screen.queryByRole("button", { name: "save_version" })).toBeNull();
  });

  it("references the selected paragraph, or the page in view, in chat and goes on editing (design §7.4)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    const onReferenceElement = vi.fn();
    const onTogglePage = vi.fn().mockReturnValue("added");
    renderEditor({ onReferenceElement, onTogglePage, referencedPages: [] });
    fireEvent.click(await screen.findByRole("button", { name: 'reference_page_label:{"page":1}' }));
    expect(onTogglePage).toHaveBeenCalledWith(expect.objectContaining({ fileId: "art_doc_1", referenceKind: "result" }), 1);
    fireEvent.click(screen.getByRole("button", { name: /销售额 2,905 万元/ }));
    fireEvent.click(await screen.findByRole("button", { name: "reference_paragraph" }));
    expect(onReferenceElement).toHaveBeenCalledWith(expect.objectContaining({
      fileId: "art_doc_1",
      documentSelection: { version: 1, artifactId: "art_doc_1", paragraphKey: "document:b1", paragraphName: expect.stringContaining("paragraph_place") },
    }), { stay: true });
  });

  it("opens 修改记录 by itself when a newer version refuses the save, and re-applies there", async () => {
    const latest = { artifact_id: "art_doc_2", version_no: 4, created_at: "2026-11-27T06:32:00Z", produced_by: "agent", request_excerpt: "改总结" };
    stubServer({
      "/document-edit/inventory": [() => ({ status: 200, body: inventory() }), () => ({ status: 200, body: inventory({ artifact_id: "art_doc_2", version_no: 4 }) })],
      "/document-edits": [() => ({ status: 409, body: { detail: { error: { code: "version_target_not_latest", latest } } } })],
    });
    renderEditor();
    fireEvent.click(await screen.findByRole("button", { name: /销售额 2,905 万元/ }));
    const field = await screen.findByRole("textbox", { name: /kind_body/ });
    fireEvent.keyDown(field, { key: "Enter" });
    fireEvent.change(field, { target: { value: "销售额 3,105 万元。" } });
    fireEvent.keyDown(field, { key: "Enter" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "save_version" })); });
    const panel = await screen.findByRole("complementary", { name: /panel_changes/ });
    expect(within(panel).getByText(/^conflict_agent:/)).toHaveTextContent("改总结");
    await within(panel).findByText("conflict_applicable");
    fireEvent.click(screen.getByRole("button", { name: 'reapply:{"version":4,"count":1}' }));
    expect(await screen.findByText('reapplied:{"version":4,"count":1}')).toBeInTheDocument();
  });
});
