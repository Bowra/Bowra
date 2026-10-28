/**
 * Recommendations — action recommendation list.
 *
 * report.css `.rc-rec` design: each row = 42×20 mono priority pill (**soft background + colored text**, not white text on a solid fill) + body.
 * P0=err-soft/err · P1=accent-soft/accent · P2=surface-2/ink-3. No emoji (design uses plain-text metadata).
 */
"use client";

import React from "react";
import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";
import { CharIconText } from "@/components/char-icon";

type RecItem = {
  priority: "P0" | "P1" | "P2";
  action: string;
  owner?: string;
  timeline?: string;
  expected_impact?: string;
  rationale?: string;
};

type RecommendationsData = {
  id: string;
  kind: "recommendations";
  items: RecItem[];
};

export function RecommendationsSection({ data }: { data: RecommendationsData }) {
  const t = useTranslations("recommendations");
  const { isEditMode, dispatchPatch } = useCanvasContext();
  if ((!data.items || data.items.length === 0) && !isEditMode) return null;
  const items = data.items || [];

  const updateItem = (i: number, field: keyof RecItem, v: string) => {
    const next = items.map((x, j) => (j === i ? { ...x, [field]: v } : x));
    dispatchPatch(data.id, { items: next } as Partial<ReportSection>);
  };

  return (
    <section className="v2-recommendations">
      {/* canvas-sections: unified grid table — header row + hairline-separated rows (pri | action | meta) */}
      <div className="rc-rec-table">
        <div className="rc-rec-head-row"><CharIconText ch="🛠️">{t("head")}</CharIconText> · ranked</div>
        {items.map((rec, i) => {
          const meta = [rec.owner, rec.timeline, rec.expected_impact].filter(Boolean).join(" · ");
          const pri = (rec.priority || "P2").toLowerCase();
          return (
            <div key={i} className="rc-rec-row">
              <span className={`rec-pri ${pri}`}>{rec.priority}</span>
              <div className="rec-action">
                {isEditMode ? (
                  <InlineEditableText
                    as="div"
                    value={rec.action || ""}
                    isEditMode
                    onChange={(v) => updateItem(i, "action", v)}
                    placeholder={t("action_placeholder")}
                  />
                ) : (
                  <div className="rec-action-text">{rec.action}</div>
                )}
                {meta && <div className="rec-meta">{meta}</div>}
                {rec.rationale && <div className="rec-rationale">{rec.rationale}</div>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
