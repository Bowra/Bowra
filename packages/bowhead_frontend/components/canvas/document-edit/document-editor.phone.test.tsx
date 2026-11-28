import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DocumentInventory } from "@/lib/document-edit";
import type { DocumentLayout } from "@/lib/document-layout";
import { DocumentEditor } from "./document-editor";
import { MIN_TOUCH, touchRects } from "./document-pages";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  useLocale: () => "zh-CN",
}));
vi.mock("@/lib/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/components/ui/sheet", async (original) => ({ ...(await original<typeof import("@/components/ui/sheet")>()), useNarrowViewport: () => true }));
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
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 390 });
  window.localStorage.setItem("bowhead.documentEdit.guideDismissed", "1");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.localStorage.clear();
});


describe("touch areas on phone pages (canvas WP1)", () => {
  it("grows each paragraph to 44 px and splits overlaps at the midpoint, so the nearest paragraph wins", () => {
    const [a, b, c] = touchRects([
      { left: 40, top: 100, width: 200, height: 12 },
      { left: 40, top: 118, width: 200, height: 12 },
      { left: 300, top: 100, width: 20, height: 12 },
    ], { width: 358, height: 506 });
    expect(a.height).toBeLessThan(MIN_TOUCH);
    expect(a.top + a.height).toBeCloseTo(115);
    expect(b.top).toBeCloseTo(115);
    expect(b.top + b.height).toBeCloseTo(118 + 12 + 16);
    // Beside the first paragraph, a narrow one keeps 44 px and the boundary between them is the middle of the gap.
    expect(c.width).toBeGreaterThanOrEqual(30);
    expect(a.left + a.width).toBeLessThanOrEqual(c.left);
  });
});

describe("DocumentEditor on phones (canvas WP1–WP5)", () => {
  it("edits a tapped paragraph in its sheet and saves from 修改记录", async () => {
    const calls = stubServer({
      "/document-edit/inventory": [() => ({ status: 200, body: inventory() })],
      "/document-edits": [() => ({ status: 201, body: { artifact_id: "art_doc_2", version_no: 4, filename: "复盘.docx", new_file: false, changes: [] } })],
    });
    renderEditor();
    expect(await screen.findByTestId("document-editor")).toHaveAttribute("data-layout", "phone");
    expect(screen.getByRole("button", { name: /phone_edit_page:\{"page":1\}.*phone_editable_count:\{"count":3\}/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /销售额 2,905 万元/ }));
    const sheet = await screen.findByRole("dialog", { name: /paragraph_place:\{"number":1\}/ });
    const field = within(sheet).getByRole("textbox");
    fireEvent.change(field, { target: { value: "销售额 3,105 万元。" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "done" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /paragraph_place/ })).not.toBeInTheDocument());
    // Under the page: what changed; at the bottom: the summary that opens 修改记录.
    expect(screen.getByRole("list", { name: /phone_page_changes:\{"page":1\}/ })).toHaveTextContent("销售额 3,105 万元。");
    fireEvent.click(screen.getByRole("button", { name: /phone_summary_label/ }));
    const changes = await screen.findByRole("dialog", { name: /panel_changes/ });
    fireEvent.click(within(changes).getByRole("button", { name: "save_version" }));
    await waitFor(() => expect(calls.some((call) => call.path.endsWith("/document-edits"))).toBe(true));
    expect(calls.find((call) => call.path.endsWith("/document-edits"))?.body).toMatchObject({ edits: [{ key: "document:b1", text: "销售额 3,105 万元。" }] });
    expect(await screen.findByRole("button", { name: "edit_again" })).toBeInTheDocument();
  });

  it("offers 修改第 N 页 and 引用这一页 under each page", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    const onTogglePage = vi.fn().mockReturnValue("added");
    renderEditor({ onTogglePage, referencedPages: [2] });
    fireEvent.click(await screen.findByRole("button", { name: 'reference_page_label:{"page":1}' }));
    expect(onTogglePage).toHaveBeenCalledWith(expect.objectContaining({ fileId: "art_doc_1" }), 1);
    expect(screen.getByRole("button", { name: 'referenced_page_label:{"page":2}' })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: /phone_edit_page:\{"page":1\}/ }));
    const pageSheet = await screen.findByRole("dialog", { name: 'phone_page_sheet_title:{"page":1}' });
    fireEvent.click(within(pageSheet).getByRole("button", { name: /paragraph_place:\{"number":2\}/ }));
    expect(await screen.findByRole("dialog", { name: /paragraph_place:\{"number":2\}/ })).toBeInTheDocument();
  });

  it("explains read-only items and references their page or paragraph (canvas WP4a)", async () => {
    stubServer({ "/document-edit/inventory": [() => ({ status: 200, body: inventory() })] });
    const onTogglePage = vi.fn().mockReturnValue("added");
    const onReferenceElement = vi.fn();
    renderEditor({ onTogglePage, onReferenceElement });
    const header = (await screen.findAllByRole("button", { name: /kind_header.*季度复盘报告/ }))[0];
    fireEvent.click(header);
    const sheet = await screen.findByRole("dialog", { name: /季度复盘报告/ });
    expect(within(sheet).getByText(/phone_readonly_body/)).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "reference_page" }));
    expect(onTogglePage).toHaveBeenCalledWith(expect.objectContaining({ fileId: "art_doc_1" }), 1);
    fireEvent.click(screen.getByRole("button", { name: /带修订的段落/ }));
    const tracked = await screen.findByRole("dialog", { name: /paragraph_place:\{"number":3\}/ });
    expect(within(tracked).queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(within(tracked).getByRole("button", { name: "reference_paragraph" }));
    expect(onReferenceElement).toHaveBeenCalledWith(expect.objectContaining({
      documentSelection: expect.objectContaining({ version: 1, paragraphKey: "document:b3" }),
    }), { stay: true });
  });
});
