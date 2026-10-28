/**
 * StreamingPreview: streaming body text, **read-only rendering outside ProseMirror**.
 *
 * Why not load directly into the editor (discovery §8.3, actual Chromium + actual SSE):
 *   11 canvas sections (prod p50) → 424ms long task, 58% main-thread utilization
 *   28 canvas sections (prod p90) → 924ms long task, 75% utilization
 *   96 canvas sections (prod max) → **one blocking task of 1833ms**, 86% utilization
 * Report body is **one** ProseMirror doc; any section content change triggers full-document
 * setContent reconstruction per chunk, with cost linear in doc size while the streaming doc keeps growing.
 * This reproduces the frozen-editor regression history described in canvas-store.ts comments.
 *
 * Stream each section here as a plain-text node, rerendering only this component per chunk.
 * On section_patched (content finalized, backend full-text cleanup complete), canvas-store removes it from
 * the streaming set; it returns to filtered for one normal setContent, rebuilding the doc only once overall.
 *
 * ⚠ **Deliberately render no Markdown / citation / figure anchors here**:
 *   - Streamed draft is unfinished ([cite:N] not yet numbered, [figure:id] not validated); rendering would flicker;
 *   - Especially no citations: backend already stripped forged bare [N], but this adds a safeguard:
 *     preview blocks produce no clickable source linkage.
 * Match final body font size and line height to minimize the visual jump when the patch arrives.
 */

"use client";

import { useTranslations } from "next-intl";

import type { ReportSection } from "@/lib/types";

import "./streaming-preview.css";

/**
 * Appearance refresh batch 4b (Report Canvas Chrome Spec §9): section currently being written. Use only a Markdown heading
 * on the first streamed body line (`## 闭店影响`); otherwise null, letting callers show "正在写这一节".
 */
export function streamingSectionTitle(markdown: string): string | null {
  const firstLine = markdown.split("\n", 1)[0] ?? "";
  const match = /^#{1,6}\s+(.+?)\s*#*\s*$/.exec(firstLine);
  return match ? match[1] : null;
}

export function StreamingPreview({ sections }: { sections: ReportSection[] }) {
  const t = useTranslations("streaming");
  if (sections.length === 0) return null;
  return (
    <>
      {sections.map((sec) => {
        const s = sec as unknown as { id?: string; markdown?: string };
        const text = s.markdown ?? "";
        if (!text) return null;
        // Section being written: 2px inset accent border left + one description line + two static skeleton lines below (ui-refresh-canvas.css).
        // Description is ordinary text; screen readers encounter "正在写…" in order.
        const title = streamingSectionTitle(text);
        return (
          <div key={s.id} className="sp-block sp-block--writing" aria-busy="true" data-writing="true">
            <p className="sp-writing-label">
              {title ? t("writing_named", { title }) : t("writing_section")}
            </p>
            <p className="sp-text">
              {text}
              <span className="sp-caret" aria-hidden="true" />
            </p>
            <div className="sp-skeleton" aria-hidden="true">
              <span />
              <span />
            </div>
          </div>
        );
      })}
    </>
  );
}
