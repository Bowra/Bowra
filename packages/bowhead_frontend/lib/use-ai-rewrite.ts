/** Stream a view-only rewrite preview; only explicit acceptance changes the report. */

"use client";

import { isNetworkFailure } from "@/lib/transport-error";
import { appFetch as fetch } from "@/lib/base-path";
import { readApiError } from "@/lib/api-client";
import { useCallback, useEffect, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";
import type { Slice } from "@tiptap/pm/model";
import { closeHistory } from "@tiptap/pm/history";
import type { RewritePreset } from "@/components/canvas/ai-rewrite-menu";
import type { SourceRef } from "./canvas-pm-adapter";
import { trackInteraction as track } from "./metrics";
import { setAiRewriteDeco } from "@/lib/canvas-pm-adapter/extensions/ai-rewrite-decorations";

export interface RewriteState {
  active: boolean;
  preset: RewritePreset | null;
  /** A complete candidate awaiting explicit acceptance or rejection. */
  reviewing: boolean;
  /** Plain-text candidate, never persisted before acceptance. */
  newText: string;
  /** Unverified flag (PRD AC7) — yellow badge. */
  unverified: boolean;
  /** Failure category (UI chooses localized explanation); null = no error. */
  error: RewriteErrorKind | null;
  /** Raw failure (status/error codes, request ID), only in "复制诊断", never displayed. */
  errorCause?: unknown;
}

export type RewriteErrorKind =
  | "model_unconfigured"
  | "model_unsupported"
  | "invalid_request"
  | "forbidden"
  | "too_frequent"
  | "network"
  | "unavailable"
  | "output_limit"
  | "failed"
  | "incomplete"
  | "empty_result"
  | "content_changed";

/** Classify rewrite failures by status/error codes; raw API text and status codes stay out of user-facing sentence. */
export function rewriteErrorKind(error: unknown): RewriteErrorKind {
  const record =
    error && typeof error === "object" ? (error as { status?: unknown; code?: unknown }) : {};
  const status = typeof record.status === "number" ? record.status : undefined;
  if (record.code === "rewrite_incomplete") return "incomplete";
  if (record.code === "rewrite_empty_result") return "empty_result";
  if (record.code === "rewrite_content_changed") return "content_changed";
  if (record.code === "team_model_unconfigured") return "model_unconfigured";
  if (record.code === "rewrite_binding_missing") return "model_unsupported";
  if (record.code === "rewrite_upstream_failed") return "unavailable";
  if (record.code === "rewrite_output_exhausted") return "output_limit";
  if (status === 400 || status === 422) return "invalid_request";
  if (status === 403) return "forbidden";
  if (status === 429) return "too_frequent";
  if (status !== undefined && status >= 500) return "unavailable";
  if (status === undefined && isNetworkFailure(error)) return "network";
  return "failed";
}

const PRESET_INSTRUCTIONS: Record<RewritePreset, string> = {
  concise: "改写为更简洁版本, 砍 30-50% 字数, 保留要点",
  professional: "提升表达水平 + 业务术语, 保持准确",
  custom: "",
  grammar: "仅修正语法错误, 不改语义",
  different_angle: "用不同角度重述, 同事实",
};

/**
 * Mock SSE simulator (revives P3.0 spike mock-sse.ts pattern).
 * Replace when real backend endpoint ships (#57 followup).
 */
async function* mockRewriteStream(
  selection: string,
  preset: RewritePreset,
  customPrompt: string | undefined,
  signal: AbortSignal,
): AsyncGenerator<
  { type: "chunk"; delta: string } | { type: "done"; full: string; unverified: boolean }
> {
  // Simulate LLM output: four mock transformation styles
  const instruction =
    preset === "custom" && customPrompt ? customPrompt : PRESET_INSTRUCTIONS[preset];
  const mockOutput = mockTransform(selection, preset);
  const chunkSize = 3;
  for (let i = 0; i < mockOutput.length; i += chunkSize) {
    if (signal.aborted) return;
    await new Promise<void>((r, rej) => {
      const timer = setTimeout(r, 50);
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(timer);
          rej(new DOMException("Aborted", "AbortError"));
        },
        { once: true },
      );
    }).catch(() => {});
    if (signal.aborted) return;
    yield { type: "chunk", delta: mockOutput.slice(i, i + chunkSize) };
  }
  // unverified: check LLM output for [ref:S] or [num:..:src:S]
  const hasRef = /\[ref:[^\]]+\]|\[num:[^:]+:[^:]+:src:[^\]]+\]/.test(mockOutput);
  yield { type: "done", full: mockOutput, unverified: !hasRef };
  // unused warn fix
  void instruction;
}

function mockTransform(selection: string, preset: RewritePreset): string {
  switch (preset) {
    case "concise":
      return `[改写: 简洁] ${selection.slice(0, Math.max(20, Math.floor(selection.length * 0.5)))}…`;
    case "professional":
      return `[改写: 专业] 据数据观察, ${selection}`;
    case "grammar":
      return `[改写: 语法] ${selection}`;
    case "different_angle":
      return `[改写: 换角度] 反过来看, ${selection}`;
    case "custom":
      return `[改写: 自定义] ${selection}`;
    default:
      return selection;
  }
}

export interface UseAIRewriteOpts {
  editor: Editor | null;
  /** P1-B: rewrite is executable only in real editable/saveable context. */
  enabled?: boolean;
  /** P3.4 mock SSE; providing canvasId defaults to real backend (#57). */
  useMock?: boolean;
  /** P4.5b: required for real backend — canvas ID for POST /sections/_/rewrite. */
  canvasId?: string | null;
  /** Rewrite-done callback (P3.4.5 displays diff bar). */
  onDone?: (result: { newText: string; unverified: boolean }) => void;
}

/**
 * Real backend SSE stream — POST /api/v1/canvas/{canvasId}/sections/_/rewrite.
 * sid="_" is placeholder (backend only validates canvas ownership; sid unused currently).
 *
 * Yield chunk / done events; throw errors.
 */
async function* backendRewriteStream(
  canvasId: string,
  selection: string,
  preset: RewritePreset,
  customPrompt: string | undefined,
  signal: AbortSignal,
): AsyncGenerator<
  { type: "chunk"; delta: string } | { type: "done"; full: string; unverified: boolean }
> {
  const res = await fetch(`/api/v1/canvas/${encodeURIComponent(canvasId)}/sections/_/rewrite`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      selection_text: selection,
      preset: preset === "custom" ? null : preset,
      custom_prompt: customPrompt || null,
    }),
    signal,
  });
  if (!res.ok) {
    // Status/error codes (team_model_unconfigured / rewrite_binding_missing) and request ID attach to
    // ApiError; UI gives localized explanation by error code, omitting raw API message.
    throw await readApiError(res);
  }
  if (!res.body) throw new Error("rewrite: no response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  const abortRead = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", abortRead, { once: true });
  try {
    while (true) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const { done, value } = await reader.read();
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      if (done)
        throw Object.assign(new Error("Rewrite stream ended without completion"), {
          code: "rewrite_incomplete",
        });
      buf = (buf + decoder.decode(value, { stream: true })).replaceAll("\r\n", "\n");
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let event = "";
        const lines: string[] = [];
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
        }
        if (!lines.length) continue;
        const obj = JSON.parse(lines.join("\n")) as Record<string, unknown>;
        if (event === "chunk" && typeof obj.delta === "string") {
          yield { type: "chunk", delta: obj.delta };
        } else if (event === "done") {
          const full =
            typeof obj.final_text === "string"
              ? obj.final_text
              : typeof obj.final === "string"
                ? obj.final
                : "";
          if (!full.trim())
            throw Object.assign(new Error("Rewrite returned no text"), {
              code: "rewrite_empty_result",
            });
          yield {
            type: "done",
            full,
            unverified: Array.isArray(obj.unverified_numbers) && obj.unverified_numbers.length > 0,
          };
          return;
        } else if (event === "error") {
          const code = typeof obj.code === "string" ? obj.code : undefined;
          throw Object.assign(
            new Error(typeof obj.error === "string" ? obj.error : "Rewrite failed"),
            {
              code,
              diagnostics: code
                ? {
                    schemaVersion: 1,
                    disclosure: "authenticated",
                    code,
                    observedAt: new Date().toISOString(),
                  }
                : undefined,
            },
          );
        }
      }
    }
  } finally {
    signal.removeEventListener("abort", abortRead);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

const IDLE: RewriteState = {
  active: false,
  reviewing: false,
  preset: null,
  newText: "",
  unverified: false,
  error: null,
};
type RewriteRequest = {
  preset: RewritePreset;
  customPrompt?: string;
  range: { from: number; to: number };
  text: string;
  slice?: Slice;
  lost: boolean;
};

/** Reject selections containing non-prose blocks or crossing report sections. */
function isProseRange(editor: Editor, from: number, to: number): boolean {
  const doc = editor.state.doc;
  if (from < 0 || to > doc.content.size || from >= to) return false;
  let safe = true;
  const sections = new Set<string>();
  doc.nodesBetween?.(from, to, (node) => {
    if (node.type.name.startsWith("section_")) {
      sections.add(node.attrs.sectionId);
      if (!["section_narrative", "section_deck", "section_callout"].includes(node.type.name))
        safe = false;
    }
    if (
      (node.isAtom && !node.isText && node.type.name !== "hardBreak") ||
      ["codeBlock", "table"].includes(node.type.name)
    )
      safe = false;
  });
  return safe && sections.size <= 1;
}

function requestStillMatches(editor: Editor, request: RewriteRequest): boolean {
  const { from, to } = request.range;
  if (request.lost || !isProseRange(editor, from, to)) return false;
  return (
    editor.state.doc.textBetween(from, to, "\n") === request.text &&
    (!request.slice || request.slice.eq(editor.state.doc.slice(from, to)))
  );
}

export function useAIRewrite(opts: UseAIRewriteOpts) {
  const { editor, canvasId, onDone, enabled = true, useMock = false } = opts;
  const [state, setState] = useState<RewriteState>(IDLE);
  const stateRef = useRef(state);
  stateRef.current = state;
  const operationRef = useRef<AbortController | null>(null);
  const lastRequestRef = useRef<RewriteRequest | null>(null);

  const reset = useCallback(() => {
    const operation = operationRef.current;
    operationRef.current = null;
    operation?.abort();
    if (editor && !editor.isDestroyed) setAiRewriteDeco(editor.view, null);
    stateRef.current = IDLE;
    setState(IDLE);
  }, [editor]);

  // A request belongs to one editor/report and edit context. Late completions cannot cross it.
  useEffect(() => {
    reset();
    lastRequestRef.current = null;
    return () => {
      operationRef.current?.abort();
      operationRef.current = null;
      if (editor && !editor.isDestroyed) setAiRewriteDeco(editor.view, null);
    };
  }, [editor, canvasId, enabled, reset]);

  useEffect(() => {
    if (!editor?.on) return;
    const onTransaction = ({
      transaction,
    }: {
      transaction: {
        docChanged: boolean;
        mapping: { mapResult: (pos: number, assoc?: number) => { pos: number; deleted: boolean } };
      };
    }) => {
      const request = lastRequestRef.current;
      if (!request || !transaction.docChanged) return;
      const from = transaction.mapping.mapResult(request.range.from, 1);
      const to = transaction.mapping.mapResult(request.range.to, -1);
      request.range = { from: from.pos, to: to.pos };
      if (from.deleted && to.deleted) request.lost = true;
    };
    editor.on("transaction", onTransaction);
    return () => {
      editor.off("transaction", onTransaction);
    };
  }, [editor]);

  const start = useCallback(
    async (preset: RewritePreset, customPrompt?: string, range?: { from: number; to: number }) => {
      if (
        !enabled ||
        !editor ||
        editor.isDestroyed ||
        (!canvasId && !useMock) ||
        operationRef.current
      )
        return;
      const { from, to } = range ?? editor.state.selection;
      if (from === to) return;
      if (!isProseRange(editor, from, to)) {
        setState({ ...IDLE, error: "invalid_request" });
        return;
      }
      const text = editor.state.doc.textBetween(from, to, "\n");
      if (!text.trim()) return;
      if (text.length > 12000 || (customPrompt?.length ?? 0) > 1000) {
        setState({ ...IDLE, error: "invalid_request" });
        return;
      }
      const request: RewriteRequest = {
        preset,
        customPrompt,
        range: { from, to },
        text,
        slice: editor.state.doc.slice?.(from, to),
        lost: false,
      };
      lastRequestRef.current = request;
      const operation = new AbortController();
      operationRef.current = operation;
      const initial = { ...IDLE, active: true, preset };
      stateRef.current = initial;
      setState(initial);
      setAiRewriteDeco(editor.view, { old: { from, to }, new: null, preview: "" });
      const mode = useMock ? "mock" : "backend";
      track("bowhead_canvas_ai_rewrite_total", { action: "start", preset, mode });
      const stream = useMock
        ? mockRewriteStream(text, preset, customPrompt, operation.signal)
        : backendRewriteStream(canvasId!, text, preset, customPrompt, operation.signal);
      let acc = "";
      try {
        for await (const event of stream) {
          if (operationRef.current !== operation || operation.signal.aborted || editor.isDestroyed)
            return;
          if (!requestStillMatches(editor, request))
            throw Object.assign(new Error("Rewrite target changed"), {
              code: "rewrite_content_changed",
            });
          if (event.type === "chunk") {
            acc += event.delta;
            setAiRewriteDeco(editor.view, { old: request.range, new: null, preview: acc });
          } else {
            if (!event.full.trim())
              throw Object.assign(new Error("Rewrite returned no text"), {
                code: "rewrite_empty_result",
              });
            const next = {
              ...IDLE,
              preset,
              reviewing: true,
              newText: event.full,
              unverified: event.unverified,
            };
            stateRef.current = next;
            setState(next);
            setAiRewriteDeco(editor.view, { old: request.range, new: null, preview: event.full });
            track("bowhead_canvas_ai_rewrite_total", { action: "done", preset, mode });
            onDone?.({ newText: event.full, unverified: event.unverified });
            return;
          }
        }
        if (operationRef.current === operation)
          throw Object.assign(new Error("Rewrite stream ended without completion"), {
            code: "rewrite_incomplete",
          });
      } catch (error) {
        if (operationRef.current !== operation || editor.isDestroyed) return;
        operationRef.current = null;
        setAiRewriteDeco(editor.view, null);
        const next = { ...IDLE, error: rewriteErrorKind(error), errorCause: error };
        stateRef.current = next;
        setState(next);
        track("bowhead_canvas_ai_rewrite_total", { action: "fail", preset, mode });
      }
    },
    [editor, canvasId, enabled, useMock, onDone],
  );

  const cancel = useCallback(() => {
    track("bowhead_canvas_ai_rewrite_total", {
      action: "cancel",
      preset: stateRef.current.preset ?? "unknown",
    });
    reset();
  }, [reset]);

  const accept = useCallback(() => {
    const current = stateRef.current;
    const request = lastRequestRef.current;
    if (!enabled || !editor || editor.isDestroyed || !current.reviewing || !request) return;
    if (!requestStillMatches(editor, request)) {
      reset();
      setState({ ...IDLE, error: "content_changed" });
      return;
    }
    // One literal-text transaction is one undo step. Preview decorations never enter history.
    const { from, to } = request.range;
    const tr = closeHistory(editor.state.tr);
    const text = current.newText.replace(/\r\n?/g, "\n");
    if (text.includes("\n")) {
      const marks = tr.doc.resolve(from).marksAcross(tr.doc.resolve(to)) ?? [];
      const nodes = text
        .split("\n")
        .flatMap((line, index) => [
          ...(index ? [editor.schema.nodes.hardBreak.create(null, null, marks)] : []),
          ...(line ? [editor.schema.text(line, marks)] : []),
        ]);
      tr.replaceWith(from, to, nodes);
    } else {
      tr.insertText(text, from, to);
    }
    tr.setMeta("preventUpdate", false);
    editor.view.dispatch(tr);
    editor.view.dispatch(closeHistory(editor.state.tr));
    track("bowhead_canvas_ai_rewrite_total", {
      action: "apply",
      preset: current.preset ?? "unknown",
    });
    reset();
  }, [editor, enabled, reset]);

  const reject = useCallback(() => {
    track("bowhead_canvas_ai_rewrite_total", {
      action: "reject",
      preset: stateRef.current.preset ?? "unknown",
    });
    reset();
  }, [reset]);

  const retry = useCallback(() => {
    const request = lastRequestRef.current;
    if (operationRef.current) return;
    reset();
    if (!editor || !request || !requestStillMatches(editor, request)) return;
    void start(request.preset, request.customPrompt, request.range);
  }, [editor, reset, start]);

  return { state, start, cancel, accept, reject, clearError: reset, retry };
}

export type { SourceRef };
