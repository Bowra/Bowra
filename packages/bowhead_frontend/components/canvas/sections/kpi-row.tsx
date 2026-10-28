"use client";

import type { ReportSection, Tone } from "@/lib/types";
import { useTranslations } from "next-intl";
import { formatInlineTokens } from "@/lib/format-inline-tokens";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";
import { splitKpiValue } from "@/lib/kpi-value-parts";
import { kpiMaxColumns } from "@/lib/kpi-grid";
import { useKpiColumns } from "@/lib/use-kpi-columns";
import clsx from "clsx";

type E = Extract<ReportSection, { kind: "kpi_row" }>;

export function KpiRowSection({ data }: { data: E }) {
  const t = useTranslations("report_edit");
  const { isEditMode, dispatchPatch } = useCanvasContext();
  const kpiGrid = useKpiColumns<HTMLDivElement>(data.cells?.length ?? 0);
  if ((!data.cells || !data.cells.length) && !isEditMode) return null;
  const cells = data.cells || [];

  // W3-11b: edit a cell's lbl/val/sub fields
  const updateCell = (idx: number, field: "lbl" | "val" | "sub", value: string) => {
    const next = cells.map((c, i) => (i === idx ? { ...c, [field]: value } : c));
    dispatchPatch(data.id, { cells: next } as Partial<ReportSection>);
  };

  return (
    <div ref={kpiGrid.ref} className="v2-kpi-row" data-max-cols={kpiMaxColumns(cells.length)} data-cols={kpiGrid.columns ?? undefined}>
      {cells.map((c, i) => (
        <div key={i} className="v2-kpi-cell">
          <InlineEditableText
            as="div"
            className="lbl"
            value={c.lbl || ""}
            isEditMode={isEditMode}
            onChange={(v) => updateCell(i, "lbl", v)}
            placeholder={t("label_placeholder")}
          />
          {/* val contains inline tokens (e.g. [num:..]); edit raw text, format for display. */}
          {isEditMode ? (
            <InlineEditableText
              as="div"
              className="val"
              value={c.val || ""}
              isEditMode
              onChange={(v) => updateCell(i, "val", v)}
              placeholder={t("value_placeholder")}
            />
          ) : (
            <div className="val" data-len={kpiValueSizeTier(c.val)}>
              <KpiValueText text={formatInlineTokens(c.val)} />
            </div>
          )}
          {(c.sub || isEditMode) ? (
            isEditMode ? (
              <InlineEditableText
                as="div"
                className={clsx("sub", toneClass(c.subTone))}
                value={c.sub || ""}
                isEditMode
                onChange={(v) => updateCell(i, "sub", v)}
                placeholder={t("supporting_placeholder")}
              />
            ) : (
              <div className={clsx("sub", toneClass(c.subTone))}>{formatInlineTokens(c.sub || "")}</div>
            )
          ) : null}
        </div>
      ))}
    </div>
  );
}

/**
 * P2 batch 6: the number keeps the KPI size, the unit or currency sign is set apart in
 * secondary text (styles/ui-refresh-content.css). Values that are not a clear "number + unit" stay as one string.
 */
export function KpiValueText({ text }: { text: string }) {
  const parts = splitKpiValue(text);
  if (!parts) return <>{text}</>;
  return (
    <>
      {parts.prefix ? <span className="v2-kpi-unit is-prefix">{parts.prefix}</span> : null}
      <span className="v2-kpi-num">{parts.number}</span>
      {parts.unit ? (
        <span className={clsx("v2-kpi-unit", /^[%‰]$/.test(parts.unit) && "is-tight")}>{parts.unit}</span>
      ) : null}
    </>
  );
}

/**
 * KPI value font-size tiers (feedback-triage-2026-08-24).
 *
 * Cards use minmax(180px, 1fr), with 30–32px serif values. Short 824.9 ¥M fits one line;
 * longer values **wrap**: observed on 116, 示例品｜23.45亿元 spans two lines,
 * 内销销售金额合计 104.71亿元 three, 26 ASCII characters four; each consumes
 * card bottom padding (last line sits exactly on its padding edge). Cards themselves grow taller
 * (91→125→158, **no border overflow**), but three/four-line KPI numbers are already unacceptable.
 *
 * Use **display width**, not character count: Chinese characters take about twice Western character width; length puts
 * 示例品｜23.45亿元 (11 characters) and 824.9 ¥M (8) in the same tier despite nearly
 * twice the actual width.
 *
 * Thresholds were **measured per tier on 116**, not guessed: scan the largest single-line font size for each display width
 * at 176px content width, choosing the worst case (surprisingly **digits are wider than Chinese**: at width 12, Chinese
 * fits at 28px, pure digits only 24px, so tier by digits). Final configuration across 14 measured cases:
 * zero padding consumption, at most two lines, 9/14 single-line.
 *
 * ⚠ PPTX has the same tiers in canvas-pptx/scripts/render_from_canvas.py
 * _kpi_value_font_size, an independent sandbox script that cannot share code across runtimes.
 * **Update both when changing thresholds**, or webpage/deck layouts for the same report diverge.
 */
export function kpiValueSizeTier(value: string | null | undefined): "s" | "m" | "l" | "xl" {
  // Measure the **rendered** string; raw values may include [cite:N] / legacy tokens removed by
  // formatInlineTokens; measuring raw text would select an excessively large tier.
  const plain = formatInlineTokens(value || "");
  let width = 0;
  for (const ch of plain) width += ch.charCodeAt(0) > 127 ? 2 : 1;
  // ≤9 rather than ≤10: ten digits ("8888888888") wrap into two lines at 32px, consuming bottom padding,
  // the sole missed observed case; lowering the threshold by one resolves it.
  if (width <= 9) return "s";
  if (width <= 14) return "m";
  if (width <= 18) return "l";
  return "xl";
}

function toneClass(t: Tone | undefined): string {
  if (t === "pos") return "pos";
  if (t === "neg") return "neg";
  return "";
}
