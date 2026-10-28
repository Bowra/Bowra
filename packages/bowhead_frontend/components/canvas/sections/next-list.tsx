"use client";

import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";
import { CharIcon } from "@/components/char-icon";

type E = Extract<ReportSection, { kind: "next_list" }>;
type NextItem = E["items"][number];

/**
 * NextList — next-step suggestions. Each is a **clickable button**; clicking automatically dispatches
 * a `canvas:followup` CustomEvent, captured by page.tsx and sent as a new turn.
 * W3-11b: title / detail are editable in edit mode.
 */
export function NextListSection({ data }: { data: E }) {
  const t = useTranslations("report_edit");
  const { isEditMode, readOnly, dispatchPatch } = useCanvasContext();
  if ((!data.items || !data.items.length) && !isEditMode) return null;
  const items = data.items || [];

  const dispatchFollowup = (text: string) => {
    window.dispatchEvent(
      new CustomEvent("canvas:followup", { detail: { text } }),
    );
  };
  const updateItem = (i: number, field: keyof NextItem, v: string) => {
    const next = items.map((x, j) => (j === i ? { ...x, [field]: v } : x));
    dispatchPatch(data.id, { items: next } as Partial<ReportSection>);
  };

  return (
    <div className="v2-next-list">
      {items.map((it, i) => {
        const prompt =
          it.detail && it.detail.length <= 60
            ? `${it.title} — ${it.detail}`
            : it.title;
        if (isEditMode) {
          return (
            <div key={i} className="v2-next-item v2-next-item-edit">
              <span className="arrow" aria-hidden><CharIcon ch="→" aria-hidden /></span>
              <span className="body">
                <InlineEditableText
                  as="span"
                  className="title"
                  value={it.title}
                  isEditMode
                  onChange={(v) => updateItem(i, "title", v)}
                  placeholder={t("title_placeholder")}
                />
                <InlineEditableText
                  as="span"
                  className="detail"
                  value={it.detail || ""}
                  isEditMode
                  onChange={(v) => updateItem(i, "detail", v)}
                  placeholder={t("detail_placeholder")}
                />
              </span>
            </div>
          );
        }
        if (readOnly) {
          return (
            <div key={i} className="v2-next-item" aria-disabled="true">
              <span className="arrow" aria-hidden><CharIcon ch="→" aria-hidden /></span>
              <span className="body">
                <span className="title">{it.title}</span>
                {it.detail ? <span className="detail">{it.detail}</span> : null}
              </span>
            </div>
          );
        }
        return (
          <button
            key={i}
            type="button"
            className="v2-next-item"
            onClick={() => dispatchFollowup(prompt)}
            title={it.detail || it.title}
          >
            <span className="arrow" aria-hidden><CharIcon ch="→" aria-hidden /></span>
            <span className="body">
              <span className="title">{it.title}</span>
              {it.detail ? <span className="detail">{it.detail}</span> : null}
            </span>
          </button>
        );
      })}
    </div>
  );
}
