"use client";

import dynamic from "next/dynamic";

import type { ChartSpec } from "@/lib/chart/types";


// canvas/sections/chart-block pulls in chart.js + chartjs-plugin-datalabels
// which are heavy and SSR-unfriendly. Load it lazily so the chat surface
// can render the streaming text first and the chart hydrates afterwards.
const ChartBlock = dynamic(
  () => import("@/components/canvas/sections/chart-block").then((m) => m.ChartBlock),
  { ssr: false, loading: () => <ChartSkeleton /> },
);

export interface ChatInlineChartProps {
  spec: ChartSpec;
}

/**
 * Inline chart inside a chat answer bubble. Thin wrapper that re-uses
 * the canvas ChartBlock renderer so the visual treatment stays
 * pixel-identical to canvas. Sized down to fit chat-bubble max-width
 * (~660px) via the surrounding `.chat-inline-chart` container.
 *
 * Per PRD N14/N15: no `↗` canvas trigger, no citation `[N]` chip —
 * the answer is pure prose; verification lives in the step expand +
 * canvas surfaces.
 */
export function ChatInlineChart({ spec }: ChatInlineChartProps) {
  // No outer border/background here: ChartBlock (`.v2-chart-block`) already
  // renders a complete bordered card. Re-boxing it produced a double frame
  // in chat (see globals.css:5010 UX-5 for the same double-frame problem nested in
  // InsightCard). Keep this as a pure spacing container so the inline chart
  // stays pixel-identical to the canvas chart, per this component's contract.
  return (
    <div className="chat-inline-chart" style={{ margin: "18px 0 6px" }}>
      <ChartBlock spec={spec} surface="chat" />
    </div>
  );
}

function ChartSkeleton() {
  return (
    <div
      style={{
        height: 240,
        background: "var(--c-surface-2)",
        position: "relative",
        overflow: "hidden",
      }}
      aria-hidden
    >
      <span
        style={{
          position: "absolute",
          inset: 0,
          background:
            "linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.5) 50%, transparent 100%)",
          animation: "bh-shimmer 1.6s linear infinite",
        }}
      />
    </div>
  );
}
