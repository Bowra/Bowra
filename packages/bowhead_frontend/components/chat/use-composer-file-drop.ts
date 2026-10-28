"use client";

import { useEffect, useRef, useState, type RefObject } from "react";

/**
 * Drop files into the chat column (Workbench Spec section 4).
 *
 * - Accept only file drags (`dataTransfer.types` contains Files), ignoring text or report-block drags.
 * - During dragenter / dragover, judge count, whether attachments are currently allowed, and explicitly unsupported categories;
 *   rejection overlay states the reason immediately with `dropEffect = "none"`. Drag MIME varies by browser (text/x-markdown,
 *   text/tsv, application/x-jsonlines…), with filename/size unavailable; reject only video, audio, and non-whitelisted images,
 *   allowing everything else. On release, each file goes through `addFile` → `validateUpload`, the same upload-button validation;
 *   invalid files become individual error attachment rows without affecting others in the batch.
 * - Release equals attachment-button upload: send files individually to `onFiles`, never a separate API.
 * - Politely announce overlay appearance and drop results to screen readers.
 */

export const DROP_HOST_CLASS = "bh-file-drop-host";

export type ComposerDropVerdict =
  | { kind: "accept"; count: number }
  | { kind: "reject"; reason: "type" | "count" | "blocked" };

function sameVerdict(a: ComposerDropVerdict | null, b: ComposerDropVerdict): boolean {
  if (!a || a.kind !== b.kind) return false;
  return a.kind === "accept" ? a.count === (b as typeof a).count : a.reason === (b as typeof a).reason;
}

const DRAGGABLE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/jpg", "image/pjpeg", "image/webp"]);

/**
 * Whether drag-time browser MIME is explicitly unsupported: reject only video, audio, and non-whitelisted images (upload allows png / jpg / webp).
 * Allow all text/*, application/*, empty strings, etc.; validate each extension/size after release.
 */
export function isDraggableUploadType(type: string): boolean {
  const t = type.trim().toLowerCase();
  if (t.startsWith("video/") || t.startsWith("audio/")) return false;
  if (t.startsWith("image/")) return DRAGGABLE_IMAGE_TYPES.has(t);
  return true;
}

interface DataTransferLike {
  types: readonly string[] | DOMStringList;
  items?: DataTransferItemList | ArrayLike<{ kind: string; type: string }> | null;
}

export function hasDraggedFiles(dt: DataTransferLike | null | undefined): boolean {
  if (!dt) return false;
  return Array.from(dt.types as ArrayLike<string>).includes("Files");
}

/** Pure decision whether a drop is allowed from information available during dragging. */
export function judgeComposerDrop(
  dt: DataTransferLike,
  opts: { blocked: boolean; currentCount: number; maxFiles: number },
): ComposerDropVerdict {
  if (opts.blocked) return { kind: "reject", reason: "blocked" };
  const items = dt.items ? Array.from(dt.items as ArrayLike<{ kind: string; type: string }>) : [];
  const files = items.filter((item) => item.kind === "file");
  // Safari may omit items on dragenter: when count is unknown, allow by type and let the upload path validate after release.
  const count = files.length;
  if (count > 0 && opts.currentCount + count > opts.maxFiles) return { kind: "reject", reason: "count" };
  // Allow a batch with any addable file: unsupported files become error rows after release; reject only if the entire batch is unsupported.
  if (files.length > 0 && files.every((item) => !isDraggableUploadType(item.type))) return { kind: "reject", reason: "type" };
  return { kind: "accept", count };
}

export function useComposerFileDrop({
  enabled,
  targetRef,
  fallbackRef,
  blocked,
  currentCount,
  maxFiles,
  onFiles,
}: {
  enabled: boolean;
  /** Overlay/listener host: workbench provides the chat column; otherwise use the composer itself. */
  targetRef?: RefObject<HTMLElement | null>;
  fallbackRef: RefObject<HTMLElement | null>;
  blocked: boolean;
  currentCount: number;
  maxFiles: number;
  onFiles: (files: File[]) => void;
}): { verdict: ComposerDropVerdict | null; host: HTMLElement | null; dropped: ComposerDropVerdict | null } {
  const [verdict, setVerdict] = useState<ComposerDropVerdict | null>(null);
  const [dropped, setDropped] = useState<ComposerDropVerdict | null>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const latest = useRef({ blocked, currentCount, maxFiles, onFiles });
  latest.current = { blocked, currentCount, maxFiles, onFiles };

  useEffect(() => {
    if (!enabled) {
      setHost(null);
      setVerdict(null);
      return;
    }
    const el = targetRef?.current ?? fallbackRef.current;
    if (!el) return;
    setHost(el);
    let depth = 0;
    // Position overlay against the receiving area: add its positioning class only during dragging, leaving normal DOM/styles unchanged.
    const show = (next: ComposerDropVerdict) => {
      el.classList.add(DROP_HOST_CLASS);
      setVerdict((prev) => (sameVerdict(prev, next) ? prev : next));
    };
    const hide = () => {
      el.classList.remove(DROP_HOST_CLASS);
      setVerdict(null);
    };
    const judge = (dt: DataTransfer) => judgeComposerDrop(dt, latest.current);
    const onEnter = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      event.preventDefault();
      depth += 1;
      setDropped(null);
      show(judge(event.dataTransfer!));
    };
    const onOver = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      event.preventDefault();
      const next = judge(event.dataTransfer!);
      event.dataTransfer!.dropEffect = next.kind === "accept" ? "copy" : "none";
      show(next);
    };
    const onLeave = (event: DragEvent) => {
      // Do not check types: some engines omit Files on dragleave. Without file dragging depth is already 0, so hiding has no side effect.
      depth = Math.max(0, depth - 1);
      // Also hide when leaving the chat column (relatedTarget outside it), guarding mismatched dragleave counts in some engines.
      const related = event.relatedTarget as Node | null;
      if (depth === 0 || !related || !el.contains(related)) {
        depth = 0;
        hide();
      }
    };
    const onDrop = (event: DragEvent) => {
      if (!hasDraggedFiles(event.dataTransfer)) return;
      event.preventDefault();
      depth = 0;
      const next = judge(event.dataTransfer!);
      hide();
      setDropped(next);
      if (next.kind !== "accept") return;
      const files = Array.from(event.dataTransfer!.files ?? []);
      if (files.length > 0) latest.current.onFiles(files);
    };
    el.addEventListener("dragenter", onEnter);
    el.addEventListener("dragover", onOver);
    el.addEventListener("dragleave", onLeave);
    el.addEventListener("drop", onDrop);
    return () => {
      el.removeEventListener("dragenter", onEnter);
      el.removeEventListener("dragover", onOver);
      el.removeEventListener("dragleave", onLeave);
      el.removeEventListener("drop", onDrop);
      hide();
    };
  }, [enabled, targetRef, fallbackRef]);

  return { verdict, host, dropped };
}
