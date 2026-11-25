import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PresentationInventory } from "@/lib/presentation-edit";
import { PresentationEditor } from "./presentation-editor";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  useLocale: () => "zh-CN",
}));

const { fetchPresentationPdf } = vi.hoisted(() => ({ fetchPresentationPdf: vi.fn() }));
vi.mock("../artifact-document-preview", () => ({
  fetchPresentationPdf,
  isPresentationPreviewBusy: () => false,
}));

vi.mock("./pdf-page", () => ({
  usePdfDocument: (data: ArrayBuffer | null) => ({ pdf: data ? { numPages: 1, getPage: vi.fn() } : null, failed: false }),
  PdfPageCanvas: ({ label }: { label?: string }) => <div data-testid="page-canvas" aria-label={label} />,
  LazyPdfPageCanvas: ({ label }: { label?: string }) => <div data-testid="page-canvas" aria-label={label} />,
  readPageText: vi.fn().mockResolvedValue([]),
  openPdfDocument: vi.fn().mockResolvedValue({ pdf: { numPages: 1 }, close: () => undefined }),
}));

function deck(overrides: Partial<PresentationInventory> = {}, cellText = "产品线 B"): PresentationInventory {
  return {
    artifact_id: "art_v3",
    version_no: 3,
    edit_mode: "version",
    slide_width: 12192000,
    slide_height: 6858000,
    field_token: "￼",
    file: { filename: "季度复盘.pptx", mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation", size: 4096 },
    new_filename: null,
    edited_copies: [],
    slides: [{
      slide_id: 256,
      index: 1,
      inherited_text: true,
      elements: [
        { key: "256:2", shape_id: 2, name: "Title 1", kind: "text", box: [0.05, 0.05, 0.6, 0.1], editable: true, reason: null,
          paragraphs: [{ text: "季度复盘", mixed_style: true, fields: 0 }] },
        { key: "256:3", shape_id: 3, name: "Table 2", kind: "table", box: [0.05, 0.3, 0.5, 0.4], editable: true, reason: null,
          rows: [[{ text: "产品线", editable: true, reason: null }, { text: "", editable: false, reason: "covered_by_merge" }], [{ text: cellText, editable: true, reason: null }, { text: "18.7%", editable: true, reason: null }]] },
        { key: "256:4", shape_id: 4, name: "Chart 3", kind: "chart", box: [0.6, 0.3, 0.35, 0.4], editable: true, reason: null,
          chart: { type: "COLUMN_CLUSTERED", categories: ["1 月", "2 月"], series: [{ name: "销售额", values: [1180, 905], format: "#,##0" }] } },
        { key: "256:5", shape_id: 5, name: "LOGO", kind: "picture", box: [0.85, 0.85, 0.1, 0.1], editable: false, reason: "picture" },
      ],
    }],
    ...overrides,
  };
}

type Route = (body: unknown) => { status: number; body: unknown };

function stubServer(routes: Record<string, Route | Route[]>) {
  const calls: Array<{ path: string; body: unknown }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) as unknown : null;
    calls.push({ path, body });
    const entry = Object.entries(routes).find(([suffix]) => path.endsWith(suffix));
    const route = entry ? (Array.isArray(entry[1]) ? entry[1].shift() : entry[1]) : undefined;
    if (!route) return new Response("{}", { status: 404 });
    const result = route(body);
    return new Response(JSON.stringify(result.body), { status: result.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const { narrow } = vi.hoisted(() => ({ narrow: { value: false } }));
vi.mock("@/components/ui/sheet", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/ui/sheet")>()),
  useNarrowViewport: () => narrow.value,
}));

function renderEditor(onExit = vi.fn(), onReferenceElement?: (reference: unknown) => void) {
  render(
    <div role="dialog">
      <PresentationEditor sessionId="s_1" artifactId="art_v3" filename="季度复盘.pptx" downloadHref="/api/v1/files/art_v3"
        initialPdfData={new ArrayBuffer(8)} onExit={onExit} onReferenceElement={onReferenceElement} />
    </div>,
  );
  return onExit;
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 1000 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 600 });
  window.localStorage.setItem("bowhead.presentationEdit.guideDismissed", "1");
  fetchPresentationPdf.mockResolvedValue(new ArrayBuffer(16));
});

afterEach(() => {
  narrow.value = false;
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("PresentationEditor", () => {
  it("edits a paragraph from the slide and saves it as the next version", async () => {
    const calls = stubServer({
      "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })],
      "/presentation-edits": () => ({ status: 201, body: { artifact_id: "art_v4", version_no: 4, filename: "季度复盘.pptx", new_file: false } }),
    });
    renderEditor();

    const hit = await screen.findByRole("button", { name: /kind_textlabel_separator季度复盘/ });
    fireEvent.click(hit);
    // A click selects, also when the text is edited in the inspector; Enter starts editing
    // without adding a new line to the field.
    await waitFor(() => expect(hit).toHaveFocus());
    const field = await screen.findByRole("textbox");
    fireEvent.keyDown(hit, { key: "Enter" });
    await waitFor(() => expect(field).toHaveFocus());
    expect(field).toHaveValue("季度复盘");
    expect(screen.queryByText(/pending_count/)).not.toBeInTheDocument();
    expect(screen.getByText("mixed_style_hint")).toBeInTheDocument();

    fireEvent.change(field, { target: { value: "年度复盘" } });
    expect(screen.getByText('pending_count:{"count":1}')).toBeInTheDocument();
    // The element's name carries the new text and its change number (design §13.3).
    expect(screen.getByRole("button", { name: /kind_textlabel_separator年度复盘 · unsaved_numbered:\{"number":1\}/ })).toHaveAttribute("data-pending", "true");

    fireEvent.click(screen.getByRole("button", { name: "save_version" }));
    await screen.findByText('saved_summary:{"count":1,"version":4,"previous":3}');
    expect(calls.find((call) => call.path.endsWith("/presentation-edits"))?.body).toEqual({
      artifact_id: "art_v3",
      edits: [{ kind: "text", key: "256:2", paragraph: 0, text: "年度复盘", expect: "季度复盘" }],
    });
    expect(fetchPresentationPdf).toHaveBeenCalledWith("/api/v1/files/art_v4/representations/pptx-pdf", expect.any(AbortSignal));
    expect(screen.queryByRole("button", { name: "save_version" })).not.toBeInTheDocument();
  });

  it("on a newer version, re-applies only the edits whose target did not change", async () => {
    const latest = { artifact_id: "art_v4", version_no: 4, created_at: "2026-11-24T06:32:00Z", produced_by: "agent", request_excerpt: "把总结页改成三条要点" };
    stubServer({
      "/presentation-edit/inventory": [
        () => ({ status: 200, body: deck() }),
        () => ({ status: 200, body: deck({ artifact_id: "art_v4", version_no: 4 }, "产品线 B（含新品）") }),
      ],
      "/presentation-edits": () => ({ status: 409, body: { detail: { error: { code: "version_target_not_latest", latest } } } }),
    });
    renderEditor();

    fireEvent.click(await screen.findByRole("button", { name: /kind_textlabel_separator季度复盘/ }));
    fireEvent.change(await screen.findByRole("textbox"), { target: { value: "年度复盘" } });
    fireEvent.click(screen.getByRole("button", { name: "back_to_list" }));
    fireEvent.click(screen.getByRole("button", { name: /kind_tablelabel_separator/ }));
    fireEvent.change(await screen.findByRole("textbox", { name: 'cell_label:{"row":2,"col":1} · 产品线' }), { target: { value: "产品线 B（华东）" } });
    fireEvent.click(screen.getByRole("button", { name: "save_version" }));

    expect(await screen.findByText(/^conflict_agent:/)).toHaveTextContent("把总结页改成三条要点");
    await screen.findByText("conflict_applicable");
    expect(screen.getByText("conflict_manual")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "save_version" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "discard_mine" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: 'reapply:{"version":4,"count":1}' }));
    expect(await screen.findByText('reapplied:{"version":4,"count":1}')).toBeInTheDocument();
    expect(screen.getByText('pending_count:{"count":1}')).toBeInTheDocument();
    expect(fetchPresentationPdf).toHaveBeenCalledWith("/api/v1/files/art_v4/representations/pptx-pdf", expect.any(AbortSignal));
  });

  it("keeps a chart value that is not a number out of the save", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    renderEditor();

    // A click selects the chart; a second click opens its data beside it (design §13.3).
    const chart = await screen.findByRole("button", { name: /kind_chartlabel_separatorChart 3/ });
    fireEvent.click(chart);
    fireEvent.click(chart);
    const popover = await screen.findByRole("dialog", { name: 'chart_popover_label:{"name":"Chart 3"}' });
    const cell = within(popover).getByRole("textbox", { name: 'chart_value_label:{"category":"1 月","series":"销售额"}' });
    expect(cell).toHaveValue("1,180");
    // Enter moves down a column, as in a table.
    cell.focus();
    fireEvent.keyDown(cell, { key: "Enter" });
    expect(within(popover).getByRole("textbox", { name: 'chart_value_label:{"category":"2 月","series":"销售额"}' })).toHaveFocus();
    cell.focus();
    fireEvent.change(cell, { target: { value: "一千" } });
    // The error is announced once the field is left, not while typing.
    expect(within(popover).queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.blur(cell);
    expect(within(popover).getByRole("alert")).toHaveTextContent("not_a_number");
    // The save waits, and the footer names the value, with a way to it (design §13.7).
    expect(screen.getByText('invalid_chart_value:{"page":1,"name":"Chart 3","category":"1 月"}')).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "save_version" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "go_edit" })).toBeInTheDocument();
    // The chart is marked as a change, numbered, and says why.
    expect(screen.getByRole("button", { name: /kind_chartlabel_separatorChart 3 · unsaved_numbered:\{"number":1\} · chart_invalid_label/ })).toHaveAttribute("data-pending", "true");
  });

  it("keeps focus in the editor when PageDown turns the page, so the next PageUp still works", async () => {
    const first = deck().slides[0];
    const second = { ...first, slide_id: 257, index: 2, elements: [{ ...first.elements[0], key: "257:2", paragraphs: [{ text: "区域明细", mixed_style: false, fields: 0 }] }] };
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck({ slides: [first, second] }) })] });
    renderEditor();

    const hit = await screen.findByRole("button", { name: /kind_textlabel_separator季度复盘/ });
    fireEvent.click(hit);
    await waitFor(() => expect(hit).toHaveFocus());
    fireEvent.keyDown(hit, { key: "PageDown" });
    // The hit left with page 1; focus moves to the page (or its rail thumbnail) instead of the body.
    await waitFor(() => expect(screen.getByRole("group", { name: 'stage_label:{"page":2,"count":2}' })).toHaveFocus());
    fireEvent.keyDown(document.activeElement as Element, { key: "PageUp" });
    await waitFor(() => expect(screen.getByRole("group", { name: 'stage_label:{"page":1,"count":2}' })).toHaveFocus());
  });

  it("toggles 引用这一页 for the shown slide and stays in the editor", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    const onToggleSlide = vi.fn().mockReturnValue("added");
    const onExit = vi.fn();
    const view = render(
      <PresentationEditor sessionId="s_e" artifactId="art_v3" filename="季度复盘.pptx" downloadHref="/api/v1/files/art_v3" onExit={onExit}
        onToggleSlide={onToggleSlide} referencedSlides={{}} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: 'reference_page_label:{"page":1}' }));
    expect(onToggleSlide).toHaveBeenCalledWith(expect.objectContaining({ fileId: "art_v3", referenceKind: "result" }), 1);
    expect(onExit).not.toHaveBeenCalled();
    // The Composer holds the page: the button reads 「已引用」, pressed.
    view.rerender(
      <PresentationEditor sessionId="s_e" artifactId="art_v3" filename="季度复盘.pptx" downloadHref="/api/v1/files/art_v3" onExit={onExit}
        onToggleSlide={onToggleSlide} referencedSlides={{ art_v3: [1] }} />,
    );
    const pressed = screen.getByRole("button", { name: 'referenced_page_label:{"page":1}' });
    expect(pressed).toHaveAttribute("aria-pressed", "true");
    expect(pressed).toHaveTextContent("referenced_page");
  });

  it("focuses the page on entry, undoes the last change with ⌘Z and brings it back with ⇧⌘Z", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    renderEditor();
    // Entering edit mode puts focus on the page, so the keys work at once.
    await waitFor(() => expect(screen.getByRole("group", { name: 'stage_label:{"page":1,"count":1}' })).toHaveFocus());

    const hit = await screen.findByRole("button", { name: /kind_textlabel_separator季度复盘/ });
    fireEvent.click(hit);
    fireEvent.keyDown(hit, { key: "Enter" });
    const field = await screen.findByRole("textbox");
    fireEvent.change(field, { target: { value: "年度复盘" } });
    expect(screen.getByText('pending_count:{"count":1}')).toBeInTheDocument();
    const editor = screen.getByTestId("presentation-editor");
    fireEvent.keyDown(editor, { key: "z", metaKey: true });
    await waitFor(() => expect(screen.queryByText('pending_count:{"count":1}')).not.toBeInTheDocument());
    expect(screen.getByText('undo_announce:{"page":1,"name":"季度复盘"}')).toBeInTheDocument();
    fireEvent.keyDown(editor, { key: "z", metaKey: true, shiftKey: true });
    await waitFor(() => expect(screen.getByText('pending_count:{"count":1}')).toBeInTheDocument());
    expect(screen.getByText('redo_announce:{"page":1,"name":"季度复盘"}')).toBeInTheDocument();
  });

  it("explains a picture and offers the ways out", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    renderEditor();

    fireEvent.click(await screen.findByRole("button", { name: /kind_picturelabel_separatorLOGO · unsupported_badge/ }));
    expect(await screen.findByText("unsupported_title_picture")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "download" })).toBeInTheDocument();
    expect(screen.getByText("unsupported_list_notes")).toBeInTheDocument();
  });

  it("Esc clears the selection first, then asks before leaving with unsaved edits", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    const onExit = renderEditor();

    fireEvent.click(await screen.findByRole("button", { name: /kind_textlabel_separator季度复盘/ }));
    const field = await screen.findByRole("textbox");
    fireEvent.change(field, { target: { value: "年度复盘" } });
    fireEvent.keyDown(field, { key: "Escape" });
    await screen.findByText('page_list_title:{"page":1}');
    expect(screen.getByText("inherited_text_note")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId("presentation-editor"), { key: "Escape" });
    const guard = await screen.findByRole("alertdialog").catch(() => screen.findByRole("dialog", { name: /guard_title/ }));
    fireEvent.click(within(guard).getByRole("button", { name: "guard_keep" }));
    await waitFor(() => expect(screen.queryByText("guard_title")).not.toBeInTheDocument());
    expect(onExit).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByTestId("presentation-editor"), { key: "z", metaKey: true });
    expect(screen.queryByText('pending_count:{"count":1}')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "exit" }));
    await waitFor(() => expect(onExit).toHaveBeenCalledWith({ artifactId: "art_v3", close: false, page: 1 }));
  });

  it("offers the file already made from an upload before saving another copy", async () => {
    stubServer({
      "/presentation-edit/inventory": [() => ({ status: 200, body: deck({
        artifact_id: "art_up", version_no: 1, edit_mode: "new_file", new_filename: "季度复盘（已修改）.pptx",
        edited_copies: [{ artifact_id: "art_copy_v2", filename: "季度复盘（已修改）.pptx", version_no: 2 }],
      }) })],
    });
    const onExit = renderEditor();

    expect(await screen.findByText('guide_upload:{"filename":"季度复盘（已修改）.pptx"}')).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "open_edited_copy" }));
    await waitFor(() => expect(onExit).toHaveBeenCalledWith({ artifactId: "art_copy_v2", pdfData: undefined, close: false, edit: true }));
  });

  it("references a picture with its slide in chat after leaving the editor", async () => {
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: deck() })] });
    const onReferenceElement = vi.fn();
    // The viewer runs `then` once it shows the file, so the reference lands after the view settles.
    const onExit = renderEditor(vi.fn((exit: { then?: () => void } | null) => exit?.then?.()), onReferenceElement);

    fireEvent.click(await screen.findByRole("button", { name: /kind_picturelabel_separatorLOGO · unsupported_badge/ }));
    expect(screen.getByText('reference_element_note:{"page":1,"name":"LOGO"}')).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "reference_element" }));
    await waitFor(() => expect(onReferenceElement).toHaveBeenCalledWith({
      fileId: "art_v3", filename: "季度复盘.pptx", size: 4096, referenceKind: "result",
      mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      presentationSelection: { version: 1, artifactId: "art_v3", slideId: 256, slideIndex: 1, elementKey: "256:5", elementName: "LOGO", elementKind: "picture" },
    }));
    expect(onExit).toHaveBeenCalledWith({ artifactId: "art_v3", close: true, page: 1, then: expect.any(Function) });
    expect(onExit.mock.invocationCallOrder[0]).toBeLessThan(onReferenceElement.mock.invocationCallOrder[0]);
  });

  it("on a phone lists a slide's elements in a sheet, edits in a sheet, and keeps wide tables read-only", async () => {
    narrow.value = true;
    const wide = deck();
    wide.slides[0].elements[1].rows = [[0, 1, 2, 3].map((col) => ({ text: `c${col}`, editable: true, reason: null }))];
    stubServer({ "/presentation-edit/inventory": [() => ({ status: 200, body: wide })] });
    renderEditor();

    fireEvent.click(await screen.findByRole("button", { name: 'phone_edit_page:{"page":1,"count":3}' }));
    const list = await screen.findByRole("dialog", { name: 'page_list_title:{"page":1}' });
    fireEvent.click(within(list).getByRole("button", { name: /季度复盘/ }));
    const sheet = await screen.findByRole("dialog", { name: "季度复盘" });
    fireEvent.change(within(sheet).getByRole("textbox"), { target: { value: "年度复盘" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "done" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "季度复盘" })).not.toBeInTheDocument());
    expect(screen.getByText('phone_page_pending:{"count":1}')).toBeInTheDocument();
    expect(screen.getByText('pending_count:{"count":1}')).toBeInTheDocument();

    // A table wider than 3 columns is edited one row at a time on a phone.
    fireEvent.click(screen.getByRole("button", { name: /kind_tablelabel_separatorc0/ }));
    const table = await screen.findByRole("dialog", { name: /c0/ });
    expect(within(table).getByText('row_form_hint:{"cols":4}')).toBeInTheDocument();
    expect(within(table).queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("presentation-editor").querySelector("[data-covered]")).not.toBeNull();
    fireEvent.click(within(table).getByRole("button", { name: 'row_label:{"row":1} · c0' }));
    fireEvent.change(within(table).getByRole("textbox", { name: "c2" }), { target: { value: "c2 new" } });
    expect(screen.getByText('pending_count:{"count":2}')).toBeInTheDocument();
  });
});
