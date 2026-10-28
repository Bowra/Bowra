"use client";

import type { ReportSection } from "@/lib/types";

import { KpiRowSection } from "@/components/canvas/sections/kpi-row";

export type KpiRowSectionData = Extract<ReportSection, { kind: "kpi_row" }>;

export interface ChatInlineKpiProps {
  /** Pre-shaped kpi_row ReportSection (turn-store derives from answerPayload.kpis). */
  data: KpiRowSectionData;
}

/**
 * Inline KPI row inside a chat answer bubble. Re-uses canvas KpiRowSection
 * so the visual treatment + tone/inline-token formatting stays identical.
 *
 * No CanvasContextProvider needed — KpiRowSection falls back to the
 * default context (isEditMode=false, no dispatch) when rendered outside
 * the canvas. The chat bubble surface is intentionally read-only.
 */
export function ChatInlineKpi({ data }: ChatInlineKpiProps) {
  return (
    <div className="chat-inline-kpi" style={{ margin: "12px 0" }}>
      <KpiRowSection data={data} />
    </div>
  );
}
