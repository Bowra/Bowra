/**
 * InsightCard — M137 #144 M3 actual rendering version.
 *
 * Changes vs M1 stub:
 *   - Chart evidence chart_section_id embeds real ChartBlock (chartLookup supplies spec)
 *   - Adaptive EvidenceGrid layout (1=full, 2=2col, ≥5=collapsible tabs)
 *   - Upgraded ImpactBox (arrow / color / time window / separate note line)
 *   - Missing chart: caption-only fallback, or a muted note when there is no caption (never silent)
 */
"use client";

import React, { useState } from "react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { NarrativeSection } from "./narrative";
import { ImageSection } from "./image";
import { UnverifiedNumbersNote } from "../unverified-numbers-note";
import type { ReportSection } from "@/lib/types";
import { useCanvasContext } from "@/lib/canvas-pm-adapter/canvas-context";
import { InlineEditableText } from "@/lib/canvas-pm-adapter/extensions/inline-text";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { CharIcon } from "@/components/char-icon";
// Load chart.js bundle on demand (vercel bundle-dynamic-imports)
const ChartBlock = dynamic(
  () => import("./chart-block").then((m) => m.ChartBlock),
  { ssr: false, loading: () => <div className="h-64 bg-gray-50 animate-pulse rounded" /> },
);

type Evidence = {
  kind: "chart" | "computation" | "table" | "callout";
  caption?: string;
  subcaption?: string;
  display_hint?: "full_width" | "half" | "third" | "collapsed";
  chart_section_id?: string;
  computation_artifact_id?: string;
  table_artifact_id?: string;
  callout_text?: string;
};

type Impact = {
  quantity?: number;
  unit?: string;
  direction?: "+" | "-" | "=";
  timeframe?: string;
  note?: string;
};

type InsightCardData = {
  id: string;
  kind: "insight_card";
  title: string;
  thesis?: string;
  evidence?: Evidence[];
  narrative?: string;
  impact?: Impact;
  confidence?: "high" | "med" | "low";
  source_refs?: string[];
  /** Numbers in card text (thesis + narrative + evidence[].callout_text) without an authorized source.
   *  canvas_composer scans all three together, so warning attaches at **card level**, not inner narrative
   *  — thesis-only / callout-only cards must render it too (see UnverifiedNumbersNote). */
  unverified_numbers?: string[];
};

type LayoutMode = "single_full" | "stack_full" | "grid_2col" | "tabbed";

function pickLayout(evidence: Evidence[]): LayoutMode {
  const n = evidence.length;
  if (n <= 1) return "single_full";
  // 2+ evidence defaults to grid_2col (even mixed callout/chart) — measured user feedback:
  // "一行一张太少". Retain stack_full only for text-only combinations such as callout-only.
  if (n === 2) {
    const allCallouts = evidence.every((e) => e.kind === "callout");
    return allCallouts ? "stack_full" : "grid_2col";
  }
  if (n <= 4) return "grid_2col";
  return "tabbed";
}

export function InsightCardSection({
  data,
  chartLookup,
  turnStatus,
}: {
  data: InsightCardData;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  chartLookup?: Map<string, any>;
  /** Sprint ι (PRD §4.D.2): "running" → loading placeholder for chart-lookup miss;
   *  "done" → caption-only fallback or the muted missing-chart note. Default = "done" so legacy
   *  turn render (post-hydrate) keeps existing behavior. */
  turnStatus?: "running" | "done";
}) {
  const t = useTranslations("insight_card");
  const { isEditMode, dispatchPatch, isStreaming } = useCanvasContext();
  // Sprint ι: explicit prop overrides; else fall back to canvas isStreaming.
  const effectiveTurnStatus: "running" | "done" =
    turnStatus ?? (isStreaming ? "running" : "done");
  const conf = data.confidence ?? "med";
  const evidence = data.evidence ?? [];
  const layout = pickLayout(evidence);

  return (
    <section
      className="v2-insight-card"
      style={{
        // canvas-sections: insight = hero level, flat (no white card frame), only thin left accent + indentation marker.
        // Inline overrides block-card-frames border/bg/radius.
        border: "none",
        background: "transparent",
        borderLeft: "2px solid var(--c-accent)",
        borderRadius: 0,
        padding: "2px 0 2px 16px",
        marginBottom: "24px",
      }}
    >
      <header style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 6 }}>
        <span className="rc-insight-label">{t("label")}</span>
        <InlineEditableText
          as="h2"
          value={data.title || ""}
          isEditMode={isEditMode}
          onChange={(v) => dispatchPatch(data.id, { title: v } as Partial<ReportSection>)}
          placeholder={t("title_placeholder")}
          style={{ fontSize: "1.125rem", fontWeight: 600, margin: 0, flex: 1, letterSpacing: "-0.005em" }}
        />
        <ConfidenceBadge level={conf} />
      </header>

      {(data.thesis || isEditMode) && (
        <InlineEditableText
          as="p"
          className="rc-insight-thesis"
          value={data.thesis || ""}
          isEditMode={isEditMode}
          onChange={(v) => dispatchPatch(data.id, { thesis: v } as Partial<ReportSection>)}
          placeholder={t("thesis_placeholder")}
          style={{
            // canvas-sections: thesis = serif italic, quotation marks, hero size.
            fontSize: "1.3125rem",
            fontFamily: "var(--font-v2-serif, 'Source Serif 4', Georgia, serif)",
            fontStyle: "italic",
            fontWeight: 400,
            lineHeight: 1.35,
            color: "var(--c-ink)",
            margin: "8px 0 14px",
            textWrap: "pretty" as const,
          }}
        />
      )}

      {evidence.length > 0 && (
        <EvidenceGrid layout={layout}>
          {evidence.map((ev, i) => (
            <EvidenceBlockRender
              key={i}
              ev={ev}
              chartLookup={chartLookup}
              insightSectionId={data.id}
              turnStatus={effectiveTurnStatus}
            />
          ))}
        </EvidenceGrid>
      )}

      {data.narrative && (
        <div style={{ marginTop: 14, marginBottom: 12 }}>
          <NarrativeSection
            data={{ id: `${data.id}_narr`, kind: "narrative", markdown: data.narrative }}
          />
        </div>
      )}

      {data.impact && hasAnyValue(data.impact) ? <ImpactBox impact={data.impact} /> : null}

      <UnverifiedNumbersNote numbers={data.unverified_numbers} />
    </section>
  );
}

function EvidenceGrid({
  layout,
  children,
}: {
  layout: LayoutMode;
  children: React.ReactNode;
}) {
  const t = useTranslations("insight_card");
  const [activeTab, setActiveTab] = useState(0);
  const items = React.Children.toArray(children);

  if (layout === "tabbed") {
    return (
      <div className="v2-evidence-tabbed">
        <div
          style={{
            display: "flex",
            gap: 6,
            marginBottom: 12,
            overflowX: "auto",
            borderBottom: "1px solid var(--c-line)",
            paddingBottom: 6,
          }}
        >
          {items.map((_, i) => (
            <button
              key={i}
              onClick={() => setActiveTab(i)}
              style={{
                padding: "6px 12px",
                fontSize: "0.8rem",
                background: activeTab === i ? "var(--c-ink)" : "transparent",
                color: activeTab === i ? "var(--c-surface)" : "var(--c-ink-4)",
                border: "1px solid",
                borderColor: activeTab === i ? "var(--c-ink)" : "var(--c-line)",
                borderRadius: 6,
                cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {t("evidence_tab", { n: i + 1 })}
            </button>
          ))}
        </div>
        {items[activeTab]}
      </div>
    );
  }

  const styleByLayout: Record<Exclude<LayoutMode, "tabbed">, React.CSSProperties> = {
    single_full: { display: "block" },
    stack_full: { display: "flex", flexDirection: "column", gap: 12 },
    // Sprint θ Phase 8d prod-3 fix: 2 charts side-by-side overflowed when
    // the canvas pane was narrow (split mode, non-fullscreen). "1fr 1fr"
    // forces 2 columns regardless of width, so each chart's intrinsic
    // min-width pushed the second chart off-screen. Use auto-fit + minmax
    // so charts wrap to single column when narrower than 280px each.
    // min-width: 0 on children allows ChartBlock to shrink.
    grid_2col: {
      display: "grid",
      gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))",
      gap: 12,
      minWidth: 0,
    },
  };
  return (
    <div className={`v2-evidence-grid v2-evidence-grid--${layout}`} style={styleByLayout[layout]}>
      {children}
    </div>
  );
}

function EvidenceBlockRender({
  ev,
  chartLookup,
  insightSectionId,
  turnStatus,
}: {
  ev: Evidence;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  chartLookup?: Map<string, any>;
  insightSectionId: string;
  /** Sprint ι (PRD §4.D.2): "running" → loading placeholder; "done" → red error. */
  turnStatus: "running" | "done";
}) {
  const t = useTranslations("insight_card");
  // feedback-0713 #3: chart_section_id is a visual reference — same field for chart or image sections.
  // Compute peer matplotlib PNG becomes kind="image" via add_image and embeds through
  // context imageLookup (empty Map without Provider naturally falls back to caption-only).
  const { imageLookup } = useCanvasContext();
  const cap = ev.caption ?? "";

  if (ev.kind === "chart") {
    const spec = ev.chart_section_id ? chartLookup?.get(ev.chart_section_id) : null;
    if (!spec) {
      const img = ev.chart_section_id ? imageLookup.get(ev.chart_section_id) : null;
      if (img) {
        return (
          <div style={{ marginBottom: 4 }}>
            {cap && (
              <div
                style={{
                  fontSize: "0.85rem",
                  color: "var(--c-ink-3)",
                  marginBottom: 6,
                }}
              >
                {cap}
              </div>
            )}
            {/* Evidence cap supplies caption; clear section's own caption to avoid duplicate labels */}
            <div
              data-export-section-id={ev.chart_section_id}
              data-export-effective-kind="image"
              style={{ display: "contents" }}
            >
              <ImageSection data={{ ...img, caption: undefined }} />
            </div>
          </div>
        );
      }
      // Sprint ι (PRD §4.D.2): during running stream, chart section may emit
      // AFTER insight_card section. Show a loading placeholder until the turn is
      // done; after that the missing chart falls back as below.
      if (turnStatus === "running") {
        return <LoadingEvidencePlaceholder caption={cap} />;
      }
      // Sprint µ (2026-05-10): peer LLM hallucinates chart_section_id strings
      // without calling canvas.add_chart — happens often enough to be a regression
      // (prod t_059e6921 disaster). Per discovery.md design error #3:
      // - Backend should validate refs (µ-N1a)
      // - Frontend gracefully degrades: caption-only, no red error.
      // WP2 (design D3 ChartFallback / same no-disappearance principle as export contract): with no caption either,
      // no longer hide entire cell (μ-N1b) — render muted note to make missing chart a visible state.
      if (!cap) {
        return (
          <div className="v2-evidence-missing-note" role="note">
            {t("evidence_chart_missing")}
          </div>
        );
      }
      return (
        <div
          style={{
            fontSize: "0.85rem",
            color: "var(--c-ink-3)",
            fontStyle: "italic",
            marginBottom: 4,
          }}
        >
          {cap}
        </div>
      );
    }
    return (
      <div style={{ marginBottom: 4 }}>
        {cap && (
          <div
            style={{
              fontSize: "0.85rem",
              color: "var(--c-ink-3)",
              marginBottom: 6,
            }}
          >
            {cap}
          </div>
        )}
        <ChartBlock
          key={spec.data_artifact_id || spec.title}
          spec={spec}
          sectionId={ev.chart_section_id}
        />
      </div>
    );
  }

  if (ev.kind === "callout") {
    return (
      <div
        style={{
          background: "var(--c-accent-tint)",
          borderLeft: "3px solid var(--c-accent)",
          padding: "10px 14px",
          fontSize: "0.8125rem",
          color: "var(--c-ink)",
          borderRadius: "0 8px 8px 0",
        }}
      >
        <div style={{ fontWeight: 500 }}>{ev.callout_text}</div>
        {cap && (
          <div style={{ color: "var(--c-ink-3)", fontSize: "0.71875rem", marginTop: 4 }}>
            {cap}
          </div>
        )}
      </div>
    );
  }

  if (ev.kind === "computation") {
    // Sprint θ Phase 8d prod-1 fix: when backend emits kind=computation for
    // an artifact that has chart output, scan chartLookup for any chart spec
    // whose data_artifact_id matches this computation_artifact_id. If found,
    // render the chart inside the details. Resolves user-reported case
    // s_ad0ca525cc · t_3de059ba where "直接材料成本构成" referenced
    // art_2025_cost_components but only showed the placeholder hint.
    let matchedSpec: { data_artifact_id?: string; title?: string } | null = null;
    if (ev.computation_artifact_id && chartLookup) {
      for (const spec of chartLookup.values()) {
        if (spec?.data_artifact_id === ev.computation_artifact_id) {
          matchedSpec = spec as typeof matchedSpec;
          break;
        }
      }
    }
    return (
      <details
        open={!!matchedSpec}
        style={{
          background: "var(--c-surface-2)",
          padding: "10px 12px",
          borderRadius: 8,
          fontSize: "0.85rem",
          color: "var(--c-ink-2)",
        }}
      >
        <summary style={{ cursor: "pointer", fontFamily: "var(--font-v2-mono, monospace)" }}>
          <CharIcon ch={matchedSpec ? "📊" : "🧮"} /> {cap}
        </summary>
        {matchedSpec && (
          <div style={{ marginTop: 8 }}>
            {/* eslint-disable-next-line @typescript-eslint/no-explicit-any */}
            <ChartBlock spec={matchedSpec as any} sectionId={insightSectionId} />
          </div>
        )}
        {ev.computation_artifact_id && (
          <div style={{ marginTop: 6, color: "var(--c-ink-3)", fontSize: "0.75rem" }}>
            {t("comp_artifact_hint", { id: ev.computation_artifact_id })}
          </div>
        )}
      </details>
    );
  }

  if (ev.kind === "table") {
    return (
      <div
        style={{
          background: "var(--c-surface-2)",
          padding: "10px 12px",
          borderRadius: 8,
          fontSize: "0.85rem",
          color: "var(--c-ink-2)",
        }}
      >
        <div style={{ fontWeight: 500 }}><CharIcon ch="📋" /> {cap}</div>
        {ev.table_artifact_id && (
          <button
            type="button"
            onClick={() => requestCanvasDataFocus(ev.table_artifact_id!, {
              entryPoint: "report",
              reportContext: {
                label: cap || t("open_table"),
                sectionId: insightSectionId,
              },
            })}
            style={{
              fontSize: "0.75rem",
              color: "var(--c-accent)",
              marginTop: 4,
              display: "inline-flex",
              alignItems: "center",
              minHeight: 44,
              padding: "0 4px",
              border: 0,
              background: "transparent",
              cursor: "pointer",
            }}
          >
            {t("open_table")}
          </button>
        )}
      </div>
    );
  }

  return null;
}

/** Sprint chat-v4-streaming Phase 4F — chart-placeholder during streaming.
 *  Replaces ι's narrow callout with the design-r2 240px placeholder (dotted
 *  axis SVG + spinner + label) so layout doesn't shift when the chart fades
 *  in. design HTML §Part 5 5A; CSS in app/globals.css `.chart-placeholder`. */
function LoadingEvidencePlaceholder({ caption }: { caption: string }) {
  const t = useTranslations("insight_card");
  return (
    <div
      className="chart-placeholder"
      data-testid="loading-evidence-placeholder"
    >
      {/* Dotted-axis skeleton — pure SVG so it stays crisp at any width.
       *  Per design line 982 / I16: no fake bars / no fake lines, only the
       *  axis frame, so the user sees "axes are coming" not "fake data is
       *  showing now". */}
      <svg
        className="chart-skel"
        viewBox="0 0 320 240"
        preserveAspectRatio="none"
        aria-hidden
      >
        <line
          x1="40" y1="200" x2="300" y2="200"
          stroke="currentColor" strokeOpacity="0.18"
          strokeWidth="1" strokeDasharray="3 4"
        />
        <line
          x1="40" y1="32" x2="40" y2="200"
          stroke="currentColor" strokeOpacity="0.18"
          strokeWidth="1" strokeDasharray="3 4"
        />
      </svg>
      <div className="chart-placeholder-label">
        <span className="chart-spinner" aria-hidden />
        <span className="cpl-text">
          {t("evidence_loading")}
          {caption ? <> · <em>{caption}</em></> : null}
        </span>
      </div>
    </div>
  );
}

function ConfidenceBadge({ level }: { level: "high" | "med" | "low" }) {
  const t = useTranslations("insight_card");
  const colors = {
    high: { bg: "var(--c-ok-soft)", fg: "var(--c-pos)" },
    med: { bg: "var(--c-warn-soft)", fg: "var(--c-warn)" },
    low: { bg: "var(--c-err-soft)", fg: "var(--c-err)" },
  } as const;
  const titleKey = level === "high" ? "confidence_high_title" : level === "med" ? "confidence_med_title" : "confidence_low_title";
  const lvlKey = level === "high" ? "confidence_high" : level === "med" ? "confidence_med" : "confidence_low";
  const c = colors[level];
  const label = t("confidence_label", { level: t(lvlKey) });
  const explanation = t(titleKey);
  return (
    // feedback-triage-2026-08-24: explanation formerly used native `title` with `help` cursor
    // implying an explanation, but native tooltip takes about 1s hover; first pass showed
    // nothing, so customers concluded "看着能点却是坏的". Use immediate `.bh-tip` instead,
    // fulfilling `help` cursor promise; remove `title` or both tooltips overlap.
    // Badge itself remains **unclickable** (fixed explanation per level, no expandable per-card evidence),
    // so omit role="button"; add tabIndex + aria-label for keyboard/screen-reader explanation access.
    <span
      className="bh-tip"
      data-tip={explanation}
      data-tip-align="end"
      tabIndex={0}
      aria-label={`${label} — ${explanation}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        background: c.bg,
        color: c.fg,
        fontSize: "0.65625rem",
        padding: "2px 9px",
        borderRadius: 999,
        fontWeight: 600,
        cursor: "help",
        whiteSpace: "nowrap",
      }}
    >
      <span aria-hidden style={{ width: 6, height: 6, borderRadius: 6, background: c.fg, flex: "0 0 auto" }} />
      {label}
    </span>
  );
}

function ImpactBox({ impact }: { impact: Impact }) {
  const t = useTranslations("insight_card");
  const dirSymbol = impact.direction === "+" ? "↑" : impact.direction === "-" ? "↓" : "→";
  const dirColor =
    impact.direction === "+" ? "var(--c-pos)" : impact.direction === "-" ? "var(--c-err)" : "var(--c-ink-3)";
  const showQuantity = impact.quantity != null && impact.unit;

  return (
    <div
      style={{
        background: "var(--c-accent-tint)",
        padding: "9px 12px",
        marginTop: 12,
        borderRadius: 9,
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <strong style={{ fontSize: "0.75rem", color: "var(--c-accent)" }}>{t("impact_label")}</strong>
        {showQuantity && (
          <span
            style={{
              fontSize: "1rem",
              fontWeight: 600,
              color: dirColor,
            }}
          >
            <CharIcon ch={dirSymbol} /> {impact.quantity}
            {impact.unit}
          </span>
        )}
      </div>
      {impact.timeframe && (
        <div style={{ fontSize: "0.75rem", color: "var(--c-ink-3)" }}>
          {t("timeframe_label", { tf: impact.timeframe })}
        </div>
      )}
      {impact.note && (
        <div style={{ fontSize: "0.75rem", color: "var(--c-ink-2)" }}>{impact.note}</div>
      )}
    </div>
  );
}

function hasAnyValue(impact: Impact): boolean {
  return Boolean(
    impact.quantity != null ||
      impact.unit ||
      impact.direction ||
      impact.timeframe ||
      impact.note,
  );
}
