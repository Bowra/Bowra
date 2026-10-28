import { Editor } from "@tiptap/core";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import {
  CanvasDocument,
  canvasToPMDoc,
  getAllCanvasExtensions,
  pmDocToSections,
} from "./canvas-pm-adapter";
import { patchCanvasSection } from "./canvas-pm-adapter/canvas-patch";
import type { ReportSection } from "./types";
import { useDebouncedSync } from "./use-debounced-sync";
import { AiRewriteDecorationsExtension } from "./canvas-pm-adapter/extensions/ai-rewrite-decorations";
import { useAIRewrite } from "./use-ai-rewrite";

vi.mock("./metrics", () => ({ trackInteraction: vi.fn() }));
vi.mock("./canvas-pm-adapter/canvas-patch", () => ({
  patchCanvasSection: vi.fn(async () => ({ ok: true, rev: 2 })),
}));
vi.mock("./api", () => ({ reorderCanvas: vi.fn() }));
const editors: Editor[] = [];
afterEach(() => {
  cleanup();
  editors.splice(0).forEach((editor) => editor.destroy());
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function setup(frames: string) {
  const editor = new Editor({
    extensions: [StarterKit, AiRewriteDecorationsExtension],
    content: "<p>Alpha selected omega</p>",
  });
  editors.push(editor);
  editor.commands.setTextSelection({ from: 7, to: 15 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(frames, { headers: { "Content-Type": "text/event-stream" } })),
  );
  const hook = renderHook(() => useAIRewrite({ editor, canvasId: "cv_edit_audit" }));
  return { editor, ...hook };
}
function frame(event: string, data: unknown) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

it("keeps a completed candidate out of the document until acceptance", async () => {
  const { editor, result } = setup(
    frame("chunk", { delta: "Expanded detail" }) + frame("done", { final_text: "Expanded detail" }),
  );
  const original = editor.getJSON();
  await act(async () => {
    await result.current.start("custom", "Expand the explanation without adding facts");
  });
  expect(editor.getJSON()).toEqual(original);
  act(() => result.current.accept());
  expect(editor.getText()).toBe("Alpha Expanded detail omega");
  act(() => editor.commands.undo());
  expect(editor.getJSON()).toEqual(original);
});

it("uses final text rather than partial chunks and treats markup as literal text", async () => {
  const { editor, result } = setup(
    frame("chunk", { delta: "partial" }) + frame("done", { final_text: "<b>Final</b> & detail" }),
  );
  await act(async () => {
    await result.current.start("professional");
  });
  act(() => result.current.accept());
  expect(editor.getText()).toBe("Alpha <b>Final</b> & detail omega");
});

it.each([
  [frame("chunk", { delta: "unfinished" }), "incomplete"],
  [frame("done", { final_text: "   " }), "empty_result"],
  [frame("chunk", { delta: "unfinished" }) + frame("done", {}), "empty_result"],
])("retains original text when the terminal result is invalid: %s", async (frames, error) => {
  const { editor, result } = setup(frames);
  await act(async () => {
    await result.current.start("concise");
  });
  expect(result.current.state.active).toBe(false);
  expect(result.current.state.error).toBe(error);
  expect(editor.getText()).toBe("Alpha selected omega");
  act(() => result.current.accept());
  expect(editor.getText()).toBe("Alpha selected omega");
});

it("ignores repeated starts while a request is waiting", async () => {
  const { result } = setup("");
  let resolveResponse!: (response: Response) => void;
  const fetchMock = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        resolveResponse = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.start("concise");
    void result.current.start("grammar", undefined, { from: 7, to: 15 });
  });
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  await act(async () => {
    resolveResponse(new Response(frame("done", { final_text: "Short" })));
    await pending;
  });
});

it("maps acceptance around other edits and separates their undo history", async () => {
  const { editor, result } = setup(frame("done", { final_text: "Updated" }));
  await act(async () => {
    await result.current.start("grammar");
  });
  act(() => editor.view.dispatch(editor.state.tr.insertText("Prefix ", 1)));
  act(() => result.current.accept());
  expect(editor.getText()).toBe("Prefix Alpha Updated omega");
  act(() => editor.commands.undo());
  expect(editor.getText()).toBe("Prefix Alpha selected omega");
  act(() => editor.commands.redo());
  expect(editor.getText()).toBe("Prefix Alpha Updated omega");
});

it("rejects a stale candidate without overwriting manual edits", async () => {
  const { editor, result } = setup(frame("done", { final_text: "Updated" }));
  await act(async () => {
    await result.current.start("grammar");
  });
  act(() => editor.view.dispatch(editor.state.tr.insertText("manual", 7, 15)));
  act(() => result.current.accept());
  expect(result.current.state.error).toBe("content_changed");
  expect(editor.getText()).toBe("Alpha manual omega");
});

it("reverts a preview without creating an undoable document mutation", async () => {
  const { editor, result } = setup(frame("done", { final_text: "Updated" }));
  await act(async () => {
    await result.current.start("grammar");
  });
  expect(editor.view.dom.querySelector(".ai-rewrite-new")?.textContent).toBe("Updated");
  act(() => result.current.reject());
  expect(editor.view.dom.querySelector(".ai-rewrite-new")).toBeNull();
  expect(editor.commands.undo()).toBe(false);
  expect(editor.getText()).toBe("Alpha selected omega");
});

it.each(["cancel", "context", "unmount"])("ignores late responses after %s", async (action) => {
  const editor = new Editor({
    extensions: [StarterKit, AiRewriteDecorationsExtension],
    content: "<p>Alpha selected omega</p>",
  });
  editors.push(editor);
  let release!: (response: Response) => void;
  let signal!: AbortSignal;
  vi.stubGlobal(
    "fetch",
    vi.fn((_url, init) => {
      signal = init.signal;
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    }),
  );
  const { result, rerender, unmount } = renderHook(
    ({ canvasId }) => useAIRewrite({ editor, canvasId }),
    { initialProps: { canvasId: "cv_first" } },
  );
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.start("concise", undefined, { from: 7, to: 15 });
  });
  act(() => {
    if (action === "cancel") result.current.cancel();
    if (action === "context") rerender({ canvasId: "cv_second" });
    if (action === "unmount") unmount();
  });
  expect(signal.aborted).toBe(true);
  await act(async () => {
    release(new Response(frame("done", { final_text: "Stale" })));
    await pending;
  });
  expect(editor.getText()).toBe("Alpha selected omega");
  expect(editor.view.dom.querySelector(".ai-rewrite-new")).toBeNull();
  if (action !== "unmount")
    expect(result.current.state).toMatchObject({ active: false, reviewing: false, error: null });
});

it("cancels an open stream promptly and never accepts partial content", async () => {
  const { editor, result } = setup("");
  const cancelled = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(new TextEncoder().encode(frame("chunk", { delta: "Partial" })));
            },
            cancel: cancelled,
          }),
        ),
    ),
  );
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.start("concise");
  });
  await waitFor(() =>
    expect(editor.view.dom.querySelector(".ai-rewrite-new")?.textContent).toBe("Partial"),
  );
  await act(async () => {
    result.current.cancel();
    await pending;
  });
  expect(cancelled).toHaveBeenCalled();
  expect(result.current.state).toMatchObject({ active: false, reviewing: false, error: null });
  expect(editor.getText()).toBe("Alpha selected omega");
});

const sections: ReportSection[] = [
  { id: "sec_edit", kind: "narrative", markdown: "Revenue grew.\n\nCosts fell.", rev: 1 },
  { id: "sec_keep", kind: "narrative", markdown: "Keep this section.", rev: 1 },
];
function canvasEditor() {
  const editor = new Editor({
    extensions: [
      CanvasDocument,
      StarterKit.configure({ document: false }),
      ...getAllCanvasExtensions().map((extension) => extension.extend({ addNodeView: () => null })),
      AiRewriteDecorationsExtension,
    ],
    content: canvasToPMDoc(sections) as never,
  });
  editors.push(editor);
  return editor;
}

it("saves only accepted whole-block expansion and preserves its section wrapper", async () => {
  const editor = canvasEditor();
  const original = editor.getJSON();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(frame("done", { final_text: "Revenue grew.\nCosts fell as described." })),
    ),
  );
  const { result } = renderHook(() => {
    const rewrite = useAIRewrite({ editor, canvasId: "cv_save" });
    const sync = useDebouncedSync({
      editor,
      baseline: sections,
      canvasId: "cv_save",
      debounceMs: 10,
      suspended: rewrite.state.active || rewrite.state.reviewing,
    });
    return { rewrite, sync };
  });
  const first = editor.state.doc.firstChild!;
  await act(async () => {
    await result.current.rewrite.start("custom", "Expand using the supplied facts", {
      from: 1,
      to: first.nodeSize - 1,
    });
  });
  expect(result.current.rewrite.state.reviewing).toBe(true);
  expect(editor.getJSON()).toEqual(original);
  let flush;
  await act(async () => {
    flush = await result.current.sync.flushNow();
  });
  expect(flush).toMatchObject({ ok: false, reason: "rewrite_in_progress" });
  expect(patchCanvasSection).not.toHaveBeenCalled();
  act(() => result.current.rewrite.accept());
  await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledTimes(1));
  expect(vi.mocked(patchCanvasSection).mock.calls[0]).toEqual([
    "cv_save",
    "sec_edit",
    { markdown: "Revenue grew.\\\nCosts fell as described." },
    1,
  ]);
  expect(editor.getJSON().content?.map((node) => node.attrs?.sectionId)).toEqual([
    "sec_edit",
    "sec_keep",
  ]);
  expect(editor.getJSON().content?.[1]).toEqual(original.content?.[1]);
  act(() => editor.commands.undo());
  expect(editor.getJSON()).toEqual(original);
});

it("refuses cross-section and embedded chart selections before sending a request", async () => {
  const editor = canvasEditor();
  const original = editor.getJSON();
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { result } = renderHook(() => useAIRewrite({ editor, canvasId: "cv_scope" }));
  await act(async () => {
    await result.current.start("concise", undefined, {
      from: 2,
      to: editor.state.doc.content.size - 2,
    });
  });
  expect(result.current.state.error).toBe("invalid_request");
  expect(fetchMock).not.toHaveBeenCalled();
  expect(editor.getJSON()).toEqual(original);
  const figure = editor.schema.nodes.figure_ref.create({ figureId: "fig_keep" });
  act(() =>
    editor.view.dispatch(editor.state.tr.insert(editor.state.doc.firstChild!.nodeSize - 1, figure)),
  );
  const withFigure = editor.getJSON();
  await act(async () => {
    await result.current.start("concise", undefined, {
      from: 1,
      to: editor.state.doc.firstChild!.nodeSize - 1,
    });
  });
  expect(result.current.state.error).toBe("invalid_request");
  expect(fetchMock).not.toHaveBeenCalled();
  expect(editor.getJSON()).toEqual(withFigure);
});

it("parses split UTF-8 and CRLF frames without changing literal output", async () => {
  const { editor, result } = setup("");
  const final = "Revenue — résumé\n第二段 <b>literal</b>";
  const bytes = new TextEncoder().encode(
    frame("chunk", { delta: "Partial" }).replaceAll("\n", "\r\n") +
      frame("done", { final_text: final }).replaceAll("\n", "\r\n"),
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          new ReadableStream({
            start(controller) {
              for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
              controller.close();
            },
          }),
        ),
    ),
  );
  await act(async () => {
    await result.current.start("professional");
  });
  expect(result.current.state.newText).toBe(final);
  expect(editor.view.dom.querySelector(".ai-rewrite-new")?.querySelector("b")).toBeNull();
  act(() => result.current.accept());
  expect(editor.getText()).toBe(`Alpha ${final} omega`);
  expect(editor.view.dom.querySelectorAll("br")).toHaveLength(1);
});

it("keeps expanded line breaks visible after accepting and reloading a report", async () => {
  const editor = canvasEditor();
  const final = "Revenue grew.\nCosts fell.\n\nNo additional facts.";
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(frame("done", { final_text: final }))),
  );
  const { result } = renderHook(() => useAIRewrite({ editor, canvasId: "cv_expansion" }));
  const original = editor.getJSON();
  await act(async () => {
    await result.current.start("custom", "Expand the explanation", {
      from: 1,
      to: editor.state.doc.firstChild!.nodeSize - 1,
    });
  });
  act(() => result.current.accept());
  expect(editor.view.dom.querySelectorAll("br").length).toBeGreaterThanOrEqual(3);
  const patches = pmDocToSections(editor.getJSON(), sections);
  const saved = structuredClone(sections);
  for (const { sectionId, patch } of patches) {
    const section = saved.find((item) => item.id === sectionId);
    if (
      section?.kind === "narrative" &&
      "markdown" in patch &&
      typeof patch.markdown === "string"
    ) {
      section.markdown = patch.markdown;
    }
  }
  const reloaded = canvasToPMDoc(saved);
  expect(reloaded.content?.[0]).toEqual(editor.getJSON().content?.[0]);
  act(() => editor.commands.undo());
  expect(editor.getJSON()).toEqual(original);
});

it("resumes saving independent manual edits after a candidate is reverted", async () => {
  const editor = canvasEditor();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(frame("done", { final_text: "Candidate" }))),
  );
  const { result } = renderHook(() => {
    const rewrite = useAIRewrite({ editor, canvasId: "cv_manual" });
    const sync = useDebouncedSync({
      editor,
      baseline: sections,
      canvasId: "cv_manual",
      debounceMs: 10,
      suspended: rewrite.state.active || rewrite.state.reviewing,
    });
    return { rewrite, sync };
  });
  await act(async () => {
    await result.current.rewrite.start("professional", undefined, { from: 2, to: 9 });
  });
  act(() =>
    editor.view.dispatch(
      editor.state.tr.insertText("Manual ", editor.state.doc.firstChild!.nodeSize + 2),
    ),
  );
  expect(result.current.sync.dirty).toEqual(["sec_keep"]);
  expect(patchCanvasSection).not.toHaveBeenCalled();
  act(() => result.current.rewrite.reject());
  await waitFor(() => expect(patchCanvasSection).toHaveBeenCalledTimes(1));
  expect(vi.mocked(patchCanvasSection).mock.calls[0]).toEqual([
    "cv_manual",
    "sec_keep",
    { markdown: "Manual Keep this section." },
    1,
  ]);
});
