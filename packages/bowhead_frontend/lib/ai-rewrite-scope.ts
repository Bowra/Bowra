import type { Editor } from "@tiptap/react";
import type { Slice } from "@tiptap/pm/model";

export const REWRITE_TEXT_LIMIT = 12000;
export const REWRITE_INSTRUCTION_LIMIT = 1000;

/** Shared admission rule for selection, block scope and acceptance. */
export function isProseRange(editor: Editor, from: number, to: number): boolean {
  const doc = editor.state.doc;
  if (from < 0 || to > doc.content.size || from >= to) return false;
  let safe = true;
  const sections = new Set<string>();
  doc.nodesBetween?.(from, to, (node) => {
    if (node.type.name.startsWith("section_")) {
      sections.add(node.attrs.sectionId);
      if (!["section_narrative", "section_deck", "section_callout"].includes(node.type.name)) safe = false;
    }
    if ((node.isAtom && !node.isText && node.type.name !== "hardBreak") || ["codeBlock", "table"].includes(node.type.name)) safe = false;
  });
  return safe && sections.size <= 1;
}

export interface RewriteScope {
  from: number;
  to: number;
  text: string;
  slice: Slice;
}

export interface BlockRewriteScopes {
  whole: boolean;
  scopes: RewriteScope[];
  reason: "no_prose" | "text_too_long" | "scope_missing" | null;
}

/** Keep pure blocks whole; enumerate individual prose paragraphs around protected content. */
export function blockRewriteScopes(editor: Editor, sectionId: string): BlockRewriteScopes {
  const result: BlockRewriteScopes = { whole: false, scopes: [], reason: "scope_missing" };
  let found = false;
  editor.state.doc.descendants((section, pos) => {
    if (found) return false;
    if (section.attrs.sectionId !== sectionId) return true;
    found = true;
    result.reason = "no_prose";
    if (!["section_narrative", "section_deck", "section_callout"].includes(section.type.name)) return false;
    const add = (from: number, to: number): boolean => {
      if (!isProseRange(editor, from, to)) return false;
      const text = editor.state.doc.textBetween(from, to, "\n");
      if (!text.trim()) return false;
      if (text.length > REWRITE_TEXT_LIMIT) {
        result.reason = "text_too_long";
        return false;
      }
      result.scopes.push({ from, to, text, slice: editor.state.doc.slice(from, to) });
      result.reason = null;
      return true;
    };
    if (add(pos + 1, pos + section.nodeSize - 1)) {
      result.whole = true;
      return false;
    }
    section.descendants((node, offset) => {
      if (node.type.name.startsWith("section_") || ["table", "codeBlock"].includes(node.type.name) || (node.isAtom && !node.isText && node.type.name !== "hardBreak")) return false;
      if (node.type.name === "paragraph") {
        add(pos + offset + 2, pos + offset + node.nodeSize);
        return false;
      }
      return true;
    });
    if (result.scopes.length) result.reason = null;
    return false;
  });
  return result;
}

export function scopeStillMatches(editor: Editor, scope: RewriteScope): boolean {
  return isProseRange(editor, scope.from, scope.to)
    && editor.state.doc.textBetween(scope.from, scope.to, "\n") === scope.text
    && scope.slice.eq(editor.state.doc.slice(scope.from, scope.to));
}
