/**
 * InlineEditable text helper — shared component for single-field inline editing within atomic NodeViews.
 *
 * Example:
 *   <InlineEditableText
 *     value={payload.text}
 *     isEditMode={isEditMode}
 *     onChange={(v) => updateAttributes({ payload: { ...payload, text: v } })}
 *     as="div"
 *     className="v2-report-eyebrow"
 *   />
 *
 * Behavior:
 *   - isEditMode=false: ordinary text, no interaction
 *   - isEditMode=true: contentEditable, 1px light-blue outline on hover, onChange on blur
 *   - content unchanged → do not call onChange (avoid unnecessary transactions)
 *
 * When PATCH is added in P3.2.6, onChange triggers a transaction through editor.commands.updateAttributes
 * → editor.onUpdate → debouncedSync → PATCH endpoint.
 */

"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import {
  clearPendingInlineEdit,
  setPendingInlineEdit,
} from "../inline-edit-registry";

export interface InlineEditableTextProps {
  value: string;
  isEditMode: boolean;
  onChange: (newValue: string) => void;
  /** Rendered tag, default span (specify div / h1 etc. when needed). */
  as?: keyof React.JSX.IntrinsicElements;
  className?: string;
  style?: React.CSSProperties;
  /** Allow multiple lines (Enter inserts a newline). Default false (single-line text). */
  multiline?: boolean;
  /** Placeholder (shown when empty, only if isEditMode=true). */
  placeholder?: string;
  /** Exposes the underlying editable element for layout measurement. */
  elementRef?: (element: HTMLElement | null) => void;
}

const BLOCK_TAGS = new Set([
  "ADDRESS",
  "ARTICLE",
  "ASIDE",
  "BLOCKQUOTE",
  "DIV",
  "FIGCAPTION",
  "FIGURE",
  "FOOTER",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "HEADER",
  "LI",
  "MAIN",
  "P",
  "SECTION",
]);

function normalizeEditableText(text: string): string {
  return text.replace(/\r\n?/g, "\n").replace(/\n$/, "");
}

function readMultilineEditableText(element: HTMLElement): string {
  if (typeof element.innerText === "string") {
    return normalizeEditableText(element.innerText);
  }

  let text = "";
  const appendLineBreak = () => {
    if (text && !text.endsWith("\n")) text += "\n";
  };
  const walk = (node: Node) => {
    if (node.nodeType === 3) {
      text += node.nodeValue ?? "";
      return;
    }
    if (node.nodeType !== 1) return;
    const child = node as HTMLElement;
    if (child.tagName === "BR") {
      text += "\n";
      return;
    }
    const isBlock = child !== element && BLOCK_TAGS.has(child.tagName);
    if (isBlock) appendLineBreak();
    child.childNodes.forEach(walk);
    if (isBlock) appendLineBreak();
  };
  element.childNodes.forEach(walk);
  return normalizeEditableText(text);
}

export function InlineEditableText({
  value,
  isEditMode,
  onChange,
  as = "span",
  className,
  style,
  multiline = false,
  placeholder,
  elementRef,
}: InlineEditableTextProps) {
  const t = useTranslations("inline_edit");
  const ref = useRef<HTMLElement | null>(null);
  const setRef = useCallback((element: HTMLElement | null) => {
    ref.current = element;
    elementRef?.(element);
  }, [elementRef]);

  // Batch B (report-edit-audit P1-2): account for the blind spot in blur-only commits. While typing, PM doc does not know
  // about changes (updateAttributes on each keystroke rerenders NodeView and moves the cursor), so register pending edits
  // → the status bar truthfully shows "未保存", and all commit before flushNow. A ref holds the latest value/onChange,
  // keeping the registry commit closure valid across rerenders.
  const pendingKey = useId();
  const latestRef = useRef({ value, onChange, multiline });
  latestRef.current = { value, onChange, multiline };
  const readCurrent = useCallback((): string => {
    const el = ref.current;
    if (!el) return latestRef.current.value;
    return latestRef.current.multiline
      ? readMultilineEditableText(el)
      : (el.textContent ?? "");
  }, []);
  const commitNow = useCallback(() => {
    const current = readCurrent();
    if (current !== latestRef.current.value) {
      latestRef.current.onChange(current);
    }
    clearPendingInlineEdit(pendingKey);
  }, [pendingKey, readCurrent]);
  const handleInput = () => {
    if (!isEditMode) return;
    if (readCurrent() !== latestRef.current.value) {
      setPendingInlineEdit(pendingKey, commitNow);
    } else {
      clearPendingInlineEdit(pendingKey);
    }
  };
  // Unmount / exit edit mode → clear pending without committing: NodeView often unmounts during setContent reconstruction,
  // when dispatching updateAttributes would affect the rebuilt doc.
  useEffect(() => {
    if (!isEditMode) clearPendingInlineEdit(pendingKey);
    return () => clearPendingInlineEdit(pendingKey);
  }, [isEditMode, pendingKey]);

  // Sync external value changes (e.g. SSE patch) to the DOM
  useEffect(() => {
    if (ref.current && ref.current.textContent !== value) {
      ref.current.textContent = value;
    }
  }, [value]);

  // design r2 (report-edit-affordance): a local teal glow for 300ms after commit provides nearby feedback,
  // independent of the distant status bar. Optimistic glow (no network wait; status bar + toast already cover save failure).
  const [justCommitted, setJustCommitted] = useState(false);
  const doneTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (doneTimerRef.current) clearTimeout(doneTimerRef.current);
  }, []);

  const handleBlur = (e: React.FocusEvent<HTMLElement>) => {
    if (!isEditMode) return;
    const newValue = multiline
      ? readMultilineEditableText(e.currentTarget)
      : e.currentTarget.textContent ?? "";
    if (newValue !== value) {
      onChange(newValue);
      setJustCommitted(true);
      if (doneTimerRef.current) clearTimeout(doneTimerRef.current);
      doneTimerRef.current = setTimeout(() => setJustCommitted(false), 300);
    }
    clearPendingInlineEdit(pendingKey);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    // Single-line mode: Enter blurs (prevent newline insertion)
    if (!multiline && e.key === "Enter") {
      e.preventDefault();
      (e.currentTarget as HTMLElement).blur();
    }
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const Tag = as as any;
  // design r2 (report-edit-affordance): all four states are class-driven (rc-ed family, --c-* tokens),
  // replacing legacy inline blue outline JS. hover=light accent fill + dashed underline + trailing pencil + "点击修改" tooltip,
  // edit(:focus)=accent double ring; done=teal glow after commit.
  return (
    <Tag
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      ref={setRef as any}
      className={[
        className,
        isEditMode ? "rc-ed" : null,
        justCommitted ? "rc-ed-done" : null,
      ].filter(Boolean).join(" ") || undefined}
      contentEditable={isEditMode}
      suppressContentEditableWarning
      onInput={handleInput}
      onBlur={handleBlur}
      onKeyDown={handleKeyDown}
      data-placeholder={placeholder}
      data-tip={isEditMode ? t("click_to_edit") : undefined}
      style={style}
    >
      {value}
    </Tag>
  );
}
