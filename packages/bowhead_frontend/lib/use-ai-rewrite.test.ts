import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import type { Editor } from "@tiptap/react";

import { rewriteErrorKind, useAIRewrite } from "./use-ai-rewrite";

// Metrics use a 250ms batching timer and the same fetch; without mocking, under high machine load
// the timer fires mid-test, mixing telemetry into rewrite-request counts (lib/track does not exist).
vi.mock("./metrics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./metrics")>()),
  trackInteraction: vi.fn(),
}));

function makeEditor() {
  let content = "Alpha selected omega";
  const selection = { from: 6, to: 14 };
  const editor = {
    state: {
      selection,
      doc: {
        get content() {
          return { size: content.length };
        },
        textBetween(from: number, to: number) {
          return content.slice(from, to);
        },
      },
    },
    commands: {
      setTextSelection(posOrRange: number | { from: number; to: number }) {
        if (typeof posOrRange === "number") {
          editor.state.selection.from = posOrRange;
          editor.state.selection.to = posOrRange;
        } else {
          editor.state.selection.from = posOrRange.from;
          editor.state.selection.to = posOrRange.to;
        }
        return true;
      },
      insertContent(text: string) {
        const from = editor.state.selection.from;
        const to = editor.state.selection.to;
        content = `${content.slice(0, from)}${text}${content.slice(to)}`;
        editor.state.selection.from = from + text.length;
        editor.state.selection.to = from + text.length;
        return true;
      },
      deleteRange(range: { from: number; to: number }) {
        content = `${content.slice(0, range.from)}${content.slice(range.to)}`;
        editor.state.selection.from = range.from;
        editor.state.selection.to = range.from;
        return true;
      },
    },
    getContent() {
      return content;
    },
  };
  return editor;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useAIRewrite", () => {
  it.each([
    ["rewrite_upstream_failed", "unavailable"],
    ["rewrite_output_exhausted", "output_limit"],
  ])("preserves original text when selection moves before %s", async (code, kind) => {
    const editor = makeEditor();
    const encoder = new TextEncoder();
    let releaseError: () => void = () => {
      throw new Error("rewrite error release was not registered");
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode('event: chunk\ndata: {"delta":"NEW"}\n\n'));
          releaseError = () => {
            controller.enqueue(encoder.encode(`event: error\ndata: ${JSON.stringify({ error: "Rewrite failed", code })}\n\n`));
            controller.close();
          };
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      },
    )));

    const { result } = renderHook(() =>
      useAIRewrite({ editor: editor as unknown as Editor, canvasId: "cv_test" }),
    );
    const startPromise = result.current.start("concise");

    await waitFor(() => {
      expect(result.current.state.active).toBe(true);
      expect(editor.getContent()).toBe("Alpha selected omega");
    });
    editor.commands.setTextSelection(editor.getContent().length);
    releaseError();

    await waitFor(() => {
      // UI explains by error type in localized copy; exact API messages remain only in errorCause for diagnostics.
      expect(result.current.state.error).toBe(kind);
      expect(result.current.state.errorCause).toMatchObject({ code });
    });
    await startPromise;

    expect(editor.getContent()).toBe("Alpha selected omega");
  });

  it("retry 用上一次的预设、指令与原文范围重发（结果条「重试」）", async () => {
    const editor = makeEditor();
    const encoder = new TextEncoder();
    const bodies: Array<Record<string, unknown>> = [];
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      bodies.push(JSON.parse(String(init.body)));
      call += 1;
      if (call === 1) return new Response(JSON.stringify({ detail: { message: "上游暂不可用" } }), { status: 503 });
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('event: chunk\ndata: {"delta":"NEW"}\n\n'));
            controller.enqueue(encoder.encode('event: done\ndata: {"final_text":"NEW"}\n\n'));
            controller.close();
          },
        }),
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    }));

    const { result } = renderHook(() =>
      useAIRewrite({ editor: editor as unknown as Editor, canvasId: "cv_test" }),
    );
    await result.current.start("custom", "改得更正式", { from: 6, to: 14 });
    await waitFor(() => expect(result.current.state.error).toBe("unavailable"));
    expect(result.current.state.errorCause).toMatchObject({ status: 503 });
    // User moved the cursor after failure; retry still uses the original text range
    editor.commands.setTextSelection(0);
    result.current.retry();
    await waitFor(() => expect(result.current.state.reviewing).toBe(true));
    expect(result.current.state.error).toBeNull();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[1]).toMatchObject({ selection_text: "selected", custom_prompt: "改得更正式" });
  });

  async function failOnce(editor: ReturnType<typeof makeEditor>, extra: Record<string, unknown> = {}) {
    // Count only rewrite requests (telemetry also uses fetch)
    const fetchMock = vi.fn(async (url: string) => new Response("x", { status: String(url).includes("/rewrite") ? 500 : 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const rewriteCalls = () => fetchMock.mock.calls.filter(([url]) => String(url).includes("/rewrite")).length;
    const stableEditor = { ...editor, ...extra } as unknown as Editor;
    const { result } = renderHook(() => useAIRewrite({ editor: stableEditor, canvasId: "cv_test" }));
    await result.current.start("concise", undefined, { from: 6, to: 14 });
    await waitFor(() => expect(result.current.state.error).not.toBeNull());
    return { result, rewriteCalls, callsBefore: rewriteCalls() };
  }

  it("retry：原文范围里的文字被改了（范围仍在文档内、非空）→ 只收起错误，不再请求", async () => {
    const editor = makeEditor();
    const { result, rewriteCalls, callsBefore } = await failOnce(editor);
    // Same-position/same-length replacement bypasses start's own empty-text check; only original-text comparison before retry blocks it
    editor.commands.setTextSelection({ from: 6, to: 14 });
    editor.commands.insertContent("SELECTED");
    expect(editor.getContent()).toBe("Alpha SELECTED omega");
    result.current.retry();
    await waitFor(() => expect(result.current.state.error).toBeNull());
    expect(rewriteCalls()).toBe(callsBefore);
  });

  it("retry：失败后在范围前插入文字、且拿不到编辑器事务映射时 → 不发请求（不改写错位的片段）", async () => {
    const editor = makeEditor();
    const { result, rewriteCalls, callsBefore } = await failOnce(editor);
    editor.commands.setTextSelection(0);
    editor.commands.insertContent("PREFIX ");
    result.current.retry();
    await waitFor(() => expect(result.current.state.error).toBeNull());
    expect(rewriteCalls()).toBe(callsBefore);
  });

  it("retry：编辑器事务把范围映射到插入后的新位置，按原文重发", async () => {
    const editor = makeEditor();
    const listeners: Array<(p: unknown) => void> = [];
    const on = (_: string, cb: (p: unknown) => void) => { listeners.push(cb); };
    const off = () => {};
    const bodies: Array<Record<string, unknown>> = [];
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      if (!String(url).includes("/rewrite")) return new Response("{}", { status: 200 });
      call += 1;
      bodies.push(JSON.parse(String(init.body)));
      return new Response("x", { status: 500 });
    }));
    const editorWithEvents = { ...editor, on, off } as unknown as Editor;
    const { result } = renderHook(() => useAIRewrite({ editor: editorWithEvents, canvasId: "cv_test" }));
    await result.current.start("concise", undefined, { from: 6, to: 14 });
    await waitFor(() => expect(result.current.state.error).not.toBeNull());
    // Insert seven characters at doc start and emit the corresponding transaction mapping
    editor.commands.setTextSelection(0);
    editor.commands.insertContent("PREFIX ");
    for (const cb of listeners) {
      cb({ transaction: { docChanged: true, mapping: { mapResult: (pos: number) => ({ pos: pos + 7, deleted: false }) } } });
    }
    const before = call;
    result.current.retry();
    await waitFor(() => expect(call).toBe(before + 1));
    expect(bodies.at(-1)).toMatchObject({ selection_text: "selected" });
  });
});

describe("rewriteErrorKind", () => {
  it.each([
    [{ status: 409, code: "team_model_unconfigured" }, "model_unconfigured"],
    [{ status: 503, code: "rewrite_binding_missing" }, "model_unsupported"],
    [{ code: "rewrite_upstream_failed" }, "unavailable"],
    [{ code: "rewrite_output_exhausted" }, "output_limit"],
    [{ status: 400 }, "invalid_request"],
    [{ status: 403 }, "forbidden"],
    [{ status: 429 }, "too_frequent"],
    [{ status: 502 }, "unavailable"],
    [new TypeError("Failed to fetch"), "network"],
    [new Error("rewrite: no response body"), "failed"],
  ] as const)("%o → %s", (error, kind) => {
    expect(rewriteErrorKind(error)).toBe(kind);
  });
});
