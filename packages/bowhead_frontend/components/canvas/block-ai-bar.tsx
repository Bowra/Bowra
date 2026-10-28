/**
 * BlockAIBar: block-level AI rewrite input bar (design D2Input, WP3).
 *
 * Trigger: hover narrative/deck/callout → action cluster "✦ Edit with AI"
 * (SectionChrome → CanvasContext.onEditBlockWithAI → ReportView opens this bar).
 *
 * Behavior: free-form instruction input fixed at report-area center (feedback-0713 #8, same position as AIRewriteDiffBar,
 *   replacing floating-ui block anchoring, where flip + inconsistent dual anchors shifted dialogs).
 *   Submit → whole-block text range → useAIRewrite.start("custom",
 *   instruction, range) → existing narrative rewrite SSE (custom_prompt) →
 *   streaming → AIRewriteDiffBar accepts/reverts. This bar only collects instructions;
 *   rewrite execution/results reuse the existing pipeline.
 *
 * Chart blocks bypass this path (no backend, sprint-status WP3 decision A); SectionChrome renders
 * no Edit with AI button for charts.
 */

"use client";

import { useEffect, useRef, useState } from "react";
import { FloatingPortal } from "@floating-ui/react";
import type { Editor } from "@tiptap/react";
import { useTranslations } from "next-intl";
import { Spark, Send } from "@/components/icons";
import { useReportCenterStyle } from "./use-report-center";
import { aiRewriteDraftKey, readAiRewriteDraft, writeAiRewriteDraft } from "@/lib/ai-rewrite-drafts";
import { CharIcon } from "@/components/char-icon";

/** Find section node text range in PM doc (whole-block rewrite). Return null if missing. */
export function findSectionTextRange(
  editor: Editor,
  sectionId: string,
): { from: number; to: number } | null {
  let found: { from: number; to: number } | null = null;
  editor.state.doc.descendants((node, pos) => {
    if (found) return false;
    const sid = (node.attrs as { sectionId?: string } | null)?.sectionId;
    if (sid === sectionId && node.type.name.startsWith("section_")) {
      found = { from: pos + 1, to: pos + node.nodeSize - 1 };
      return false;
    }
    return true;
  });
  return found;
}

interface Props {
  editor: Editor | null;
  /** sectionId of currently open AI bar; null means closed. */
  sectionId: string | null;
  onClose: () => void;
  floatingRef?: (node: HTMLElement | null) => void;
  /** Submit instruction: ReportView calls aiRewrite.start("custom", instruction, range). */
  onSubmit: (instruction: string, range: { from: number; to: number }) => void;
  /** Preserve drafts by report + section across reopening (canvas id, falling back to session id). */
  draftScope?: string | null;
}

export function BlockAIBar({ editor, sectionId, onClose, floatingRef, onSubmit, draftScope = null }: Props) {
  const t = useTranslations("ai_edit");
  const draftKey = aiRewriteDraftKey(draftScope, sectionId);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  // feedback-0713 #8: fixed at report-area center (same positioning as AIRewriteDiffBar)
  const centerStyle = useReportCenterStyle(editor, !!sectionId);

  // Automatically focus input on opening
  useEffect(() => {
    if (!sectionId || typeof document === "undefined") return;
    const id = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(id);
  }, [sectionId]);

  // On sectionId changes, clear previous input and restore this section's unsent draft
  useEffect(() => {
    setValue(readAiRewriteDraft(draftKey));
  }, [sectionId, draftKey]);

  if (!sectionId || !editor) return null;

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    const range = findSectionTextRange(editor, sectionId);
    if (!range) {
      onClose();
      return;
    }
    onSubmit(text, range);
    setValue("");
    writeAiRewriteDraft(draftKey, "");
    onClose();
  };

  return (
    <FloatingPortal>
      <div
        ref={(node) => {
          floatingRef?.(node);
        }}
        style={{ ...centerStyle, zIndex: 1000, width: "min(560px, calc(100vw - 32px))" }}
      >
        <div className="rc-ai-bar" role="dialog" aria-label={t("aria")}>
          <Spark width={15} height={15} className="spark" />
          <input
            ref={inputRef}
            className="rc-ai-text"
            type="text"
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              writeAiRewriteDraft(draftKey, e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={t("placeholder")}
          />
          <span className="rc-ai-kbd">↵</span>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("close")}
            className="legacy-rc-ai-close"
          >
            <CharIcon ch="×" />
          </button>
          <button
            type="button"
            className="rc-ai-send"
            onClick={submit}
            disabled={!value.trim()}
            aria-label={t("send")}
          >
            <Send width={13} height={13} />
          </button>
        </div>
      </div>
    </FloatingPortal>
  );
}
