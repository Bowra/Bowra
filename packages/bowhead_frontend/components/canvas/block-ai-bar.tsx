/**
 * BlockAIBar: block-level AI rewrite input bar (design D2Input, WP3).
 *
 * Trigger: hover narrative/deck/callout → action cluster "✦ Edit with AI"
 * (SectionChrome → CanvasContext.onEditBlockWithAI → ReportView opens this bar).
 *
 * Behavior: free-form instruction input fixed at report-area center (feedback-0713 #8, same position as AIRewriteDiffBar,
 *   replacing floating-ui block anchoring, where flip + inconsistent dual anchors shifted dialogs).
 *   Submit → admitted whole-block or explicit prose passage → useAIRewrite.tryStart("custom",
 *   instruction, range) → existing narrative rewrite SSE (custom_prompt) →
 *   streaming → AIRewriteDiffBar accepts/reverts. This bar only collects instructions;
 *   rewrite execution/results reuse the existing pipeline.
 *
 * Chart edits retain their existing independent path.
 */

"use client";

import { useEffect, useId, useRef, useState } from "react";
import { FloatingPortal } from "@floating-ui/react";
import type { Editor } from "@tiptap/react";
import { useTranslations } from "next-intl";
import { Spark, Send } from "@/components/icons";
import { useReportCenterStyle } from "./use-report-center";
import { aiRewriteDraftKey, readAiRewriteDraft, writeAiRewriteDraft } from "@/lib/ai-rewrite-drafts";
import { CharIcon } from "@/components/char-icon";
import { RadioGroup, Radio } from "@/components/ui/radio-group";
import { blockRewriteScopes, scopeStillMatches, REWRITE_INSTRUCTION_LIMIT, type RewriteScope } from "@/lib/ai-rewrite-scope";
import { AiRewriteDecoKey, setAiRewriteDeco } from "@/lib/canvas-pm-adapter/extensions/ai-rewrite-decorations";

/** Describe the current editor block without changing the rewrite range or persisted sections. */
export function sectionRewriteContext(editor: Editor, sectionId: string): { title: string; excerpt: string } | null {
  let heading = "";
  let context: { title: string; excerpt: string } | null = null;
  editor.state.doc.descendants((node) => {
    if (context) return false;
    if (node.attrs.sectionId === sectionId && node.type.name.startsWith("section_")) {
      let title = "";
      const paragraphs: string[] = [];
      node.descendants((child) => {
        if (child.type.name === "heading" && !title) title = child.textContent;
        else if (child.isTextblock && child.textContent) paragraphs.push(child.textContent);
        return !child.isTextblock;
      });
      const text = paragraphs.join(" ").replace(/\s+/g, " ").trim();
      const excerpt = Array.from(text).slice(0, 160).join("");
      context = { title: title || heading, excerpt: excerpt + (Array.from(text).length > 160 ? "…" : "") };
      return false;
    }
    if (node.type.name === "section_section") heading = node.attrs.h2 || heading;
    else if (node.type.name === "section_h1") heading = node.attrs.payload?.text || heading;
    else if (node.type.name === "heading") heading = node.textContent;
    return true;
  });
  return context;
}

interface Props {
  editor: Editor | null;
  /** sectionId of currently open AI bar; null means closed. */
  sectionId: string | null;
  onClose: () => void;
  floatingRef?: (node: HTMLElement | null) => void;
  /** True only after the existing rewrite owner admits generation. */
  onSubmit: (instruction: string, range: { from: number; to: number }) => boolean;
  /** Preserve drafts by report + section across reopening (canvas id, falling back to session id). */
  draftScope?: string | null;
}

export function BlockAIBar({ editor, sectionId, onClose, floatingRef, onSubmit, draftScope = null }: Props) {
  const t = useTranslations("ai_edit");
  const draftKey = aiRewriteDraftKey(draftScope, sectionId);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  const contextId = useId();
  const [selected, setSelected] = useState<RewriteScope | null>(null);
  const selectedRef = useRef<RewriteScope | null>(null);
  const [notice, setNotice] = useState<"scope_changed" | "start_rejected" | null>(null);
  const [, setDocumentRevision] = useState(0);

  const chooseScope = (scope: RewriteScope | null) => {
    selectedRef.current = scope;
    setSelected(scope);
    setNotice(null);
  };

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

  useEffect(() => {
    const scopes = editor && sectionId ? blockRewriteScopes(editor, sectionId).scopes : [];
    chooseScope(scopes.length === 1 ? scopes[0] : null);
  }, [editor, sectionId, draftKey]);

  useEffect(() => {
    if (!editor || !sectionId) return;
    const onTransaction = ({ transaction }: { transaction: import("@tiptap/pm/state").Transaction }) => {
      if (!transaction.docChanged) return;
      const previous = selectedRef.current;
      if (previous) {
        const from = transaction.mapping.mapResult(previous.from, 1);
        const to = transaction.mapping.mapResult(previous.to, -1);
        const next = { ...previous, from: from.pos, to: to.pos };
        const current = blockRewriteScopes(editor, sectionId).scopes.find((scope) => scope.from === next.from && scope.to === next.to);
        if (!(from.deleted && to.deleted) && current && scopeStillMatches(editor, next)) chooseScope(current);
        else {
          chooseScope(null);
          setNotice("scope_changed");
        }
      }
      setDocumentRevision((revision) => revision + 1);
    };
    editor.on("transaction", onTransaction);
    return () => { editor.off("transaction", onTransaction); };
  }, [editor, sectionId]);

  useEffect(() => {
    if (!editor || !sectionId || !selected) return;
    setAiRewriteDeco(editor.view, { old: null, new: null, target: { from: selected.from, to: selected.to } });
    return () => {
      // Generation replaces this target with the existing revision preview before the input closes.
      if (!editor.isDestroyed && AiRewriteDecoKey.getState(editor.state)?.target) setAiRewriteDeco(editor.view, null);
    };
  }, [editor, sectionId, selected]);

  if (!sectionId || !editor) return null;
  const context = sectionRewriteContext(editor, sectionId);
  const inventory = blockRewriteScopes(editor, sectionId);
  const problem = value.trim().length > REWRITE_INSTRUCTION_LIMIT ? "instruction_too_long"
    : inventory.reason ?? notice ?? (!selected ? "choose_scope" : null);
  const blocked = problem !== null && problem !== "start_rejected";
  const excerpt = selected ? Array.from(selected.text.replace(/\s+/g, " ").trim()).slice(0, 160).join("") : context?.excerpt;

  const submit = () => {
    const text = value.trim();
    if (!text || blocked || !selected) return;
    if (!scopeStillMatches(editor, selected)) {
      chooseScope(null);
      setNotice("scope_changed");
      return;
    }
    if (!onSubmit(text, { from: selected.from, to: selected.to })) {
      setNotice("start_rejected");
      return;
    }
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
        <div className="rc-ai-bar rc-ai-block-bar" role="dialog" aria-label={t("aria")} aria-describedby={contextId}>
          <div id={contextId} className="rc-ai-target">
            <strong>{context?.title || t("target_fallback")}</strong>
            {excerpt ? <p className="rc-ai-target-excerpt">{excerpt}</p> : null}
            <p className="rc-ai-target-scope">{t(inventory.whole ? "target_scope" : "scope_only")}</p>
          </div>
          {inventory.scopes.length > 0 && (inventory.scopes.length > 1 || !selected) ? (
            <RadioGroup
              className="rc-ai-scope-options"
              orientation="vertical"
              aria-label={t("scope_label")}
              value={selected ? String(selected.from) : null}
              onValueChange={(from) => chooseScope(inventory.scopes.find((scope) => String(scope.from) === from) ?? null)}
            >
              {inventory.scopes.map((scope, index) => <Radio key={scope.from} value={String(scope.from)}>
                <span>{t("scope_option", { index: index + 1, excerpt: Array.from(scope.text.replace(/\s+/g, " ")).slice(0, 90).join("") })}</span>
              </Radio>)}
            </RadioGroup>
          ) : null}
          {problem ? <p className="rc-ai-scope-notice" role="status">{t(problem)}</p> : null}
          <div className="rc-ai-input-row">
            <Spark width={15} height={15} className="spark" />
            <input
              ref={inputRef}
              className="rc-ai-text"
              type="text"
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                if (notice === "start_rejected") setNotice(null);
                writeAiRewriteDraft(draftKey, e.target.value);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder={t("placeholder")}
              aria-label={t("placeholder")}
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
              disabled={!value.trim() || blocked}
              aria-label={t("send")}
            >
              <Send width={13} height={13} />
            </button>
          </div>
        </div>
      </div>
    </FloatingPortal>
  );
}
