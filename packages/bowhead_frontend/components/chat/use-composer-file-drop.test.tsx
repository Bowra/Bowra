import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DROP_HOST_CLASS,
  hasDraggedFiles,
  isDraggableUploadType,
  judgeComposerDrop,
  useComposerFileDrop,
} from "./use-composer-file-drop";

const files = (...types: string[]) => ({ types: ["Files"], items: types.map((type) => ({ kind: "file", type })) });

describe("judgeComposerDrop（拖动中判断能不能放）", () => {
  it("表格、PDF、图片、类型不明（空串 / octet-stream）可以放", () => {
    expect(judgeComposerDrop(files("text/csv", "application/pdf", "image/png", "", "application/octet-stream"), { blocked: false, currentCount: 0, maxFiles: 5 }))
      .toEqual({ kind: "accept", count: 5 });
  });

  it("整批都是明确不支持的大类（视频、音频、白名单外的图片）时写原因", () => {
    expect(judgeComposerDrop(files("video/mp4", "audio/mpeg", "image/gif"), { blocked: false, currentCount: 0, maxFiles: 5 }))
      .toEqual({ kind: "reject", reason: "type" });
    expect(isDraggableUploadType("image/gif")).toBe(false);
    expect(isDraggableUploadType("image/webp")).toBe(true);
  });

  // Browser / OS MIME variants during dragging: do not reject files accepted by attachment upload via the extension fallback
  it.each([
    ["Markdown", ["text/x-markdown", "text/markdown"]],
    ["CSV 的几种写法", ["text/csv", "application/csv", "text/comma-separated-values", "application/vnd.ms-excel"]],
    ["JSON Lines", ["application/x-jsonlines", "application/jsonl", "application/x-ndjson"]],
    ["TSV", ["text/tsv", "text/tab-separated-values"]],
    ["JSON", ["text/json", "application/json"]],
    ["类型不明", ["", "application/octet-stream", "application/x-unknown"]],
  ])("%s 拖动时放行，松开后再按扩展名校验", (_label, types) => {
    for (const type of types) expect(isDraggableUploadType(type), type).toBe(true);
    expect(judgeComposerDrop(files(...types), { blocked: false, currentCount: 0, maxFiles: 5 }))
      .toEqual({ kind: "accept", count: types.length });
  });

  it("混合批次：夹着一个不支持的也整批放行（不支持的那个松开后成为错误附件条）", () => {
    expect(judgeComposerDrop(files("text/csv", "video/mp4", "text/x-markdown"), { blocked: false, currentCount: 0, maxFiles: 5 }))
      .toEqual({ kind: "accept", count: 3 });
  });

  it("超过单条消息的附件数写原因（算上已有附件）", () => {
    expect(judgeComposerDrop(files("text/csv", "text/csv"), { blocked: false, currentCount: 4, maxFiles: 5 }))
      .toEqual({ kind: "reject", reason: "count" });
    expect(judgeComposerDrop(files("text/csv"), { blocked: false, currentCount: 4, maxFiles: 5 }).kind).toBe("accept");
  });

  it("此刻不能添加附件时写原因", () => {
    expect(judgeComposerDrop(files("text/csv"), { blocked: true, currentCount: 0, maxFiles: 5 }))
      .toEqual({ kind: "reject", reason: "blocked" });
  });

  it("只认文件拖入", () => {
    expect(hasDraggedFiles({ types: ["text/plain"] })).toBe(false);
    expect(hasDraggedFiles({ types: ["Files"] })).toBe(true);
    expect(hasDraggedFiles(null)).toBe(false);
  });
});

function dragEvent(type: string, fileList: File[] = []) {
  const event = new Event(type, { bubbles: true, cancelable: true }) as DragEvent;
  Object.defineProperty(event, "dataTransfer", {
    value: {
      types: ["Files"],
      items: fileList.map((f) => ({ kind: "file", type: f.type })),
      files: fileList,
      dropEffect: "none",
    },
  });
  return event;
}

describe("useComposerFileDrop", () => {
  afterEach(() => { document.body.innerHTML = ""; });

  function setup(enabled: boolean, onFiles = vi.fn()) {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const hostRef = { current: host };
    const fallbackRef = { current: null };
    const hook = renderHook(() => useComposerFileDrop({
      enabled,
      targetRef: hostRef,
      fallbackRef,
      blocked: false,
      currentCount: 0,
      maxFiles: 5,
      onFiles,
    }));
    return { host, hook, onFiles };
  }

  it("shows the drop verdict while dragging, marks the drop zone only during the drag and hands dropped files to the upload", () => {
    const { host, hook, onFiles } = setup(true);
    const file = new File(["a"], "a.csv", { type: "text/csv" });
    let enter!: DragEvent;
    act(() => { enter = dragEvent("dragenter", [file]); host.dispatchEvent(enter); });
    expect(enter.defaultPrevented).toBe(true);
    expect(hook.result.current.verdict).toEqual({ kind: "accept", count: 1 });
    expect(hook.result.current.host).toBe(host);
    expect(host.classList.contains(DROP_HOST_CLASS)).toBe(true);
    act(() => { host.dispatchEvent(dragEvent("drop", [file])); });
    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(hook.result.current.verdict).toBeNull();
    expect(hook.result.current.dropped).toEqual({ kind: "accept", count: 1 });
    expect(host.classList.contains(DROP_HOST_CLASS)).toBe(false);
  });

  it("does not upload files that cannot be dropped", () => {
    const { host, hook, onFiles } = setup(true);
    const exe = new File(["a"], "a.mp4", { type: "video/mp4" });
    act(() => { host.dispatchEvent(dragEvent("dragenter", [exe])); });
    expect(hook.result.current.verdict).toEqual({ kind: "reject", reason: "type" });
    act(() => { host.dispatchEvent(dragEvent("drop", [exe])); });
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("when disabled: no listeners, no interception, no class", () => {
    const { host, hook, onFiles } = setup(false);
    const file = new File(["a"], "a.csv", { type: "text/csv" });
    let enter!: DragEvent;
    act(() => { enter = dragEvent("dragenter", [file]); host.dispatchEvent(enter); });
    expect(enter.defaultPrevented).toBe(false);
    expect(hook.result.current.verdict).toBeNull();
    expect(hook.result.current.host).toBeNull();
    expect(host.classList.contains(DROP_HOST_CLASS)).toBe(false);
    act(() => { host.dispatchEvent(dragEvent("drop", [file])); });
    expect(onFiles).not.toHaveBeenCalled();
  });
});
