"use client";

import dynamic from "next/dynamic";
import { useMemo, useState, type CSSProperties } from "react";
import { useTranslations } from "next-intl";

import {
  ChartThemePreviewProvider,
  type ChartTheme,
} from "@/lib/chart-theme";
import type { ChartSpec } from "@/lib/chart/types";
import { ChartMenuRootContext } from "@/components/canvas/sections/chart-menu-root";
import { Segment as SegmentOption, SegmentedControl } from "@/components/ui/segmented-control";

const ChartBlock = dynamic(
  () => import("@/components/canvas/sections/chart-block").then((module) => module.ChartBlock),
  {
    ssr: false,
    loading: () => <div className="cs-preview__loading" />,
  },
);

type Scenario = "trend" | "comparison" | "composition";

function previewSpec(
  scenario: Scenario,
  t: ReturnType<typeof useTranslations>,
): ChartSpec {
  if (scenario === "trend") {
    return {
      kind: "line",
      title: t("previewData.trendTitle"),
      subtitle: t("previewData.subtitle"),
      row_count: 8,
      columns: [
        { key: "period", header: t("previewData.period"), type: "date" },
        { key: "revenue", header: t("previewData.revenue"), type: "numeric" },
        { key: "plan", header: t("previewData.plan"), type: "numeric" },
        { key: "prior", header: t("previewData.prior"), type: "numeric" },
        { key: "forecast", header: t("previewData.forecast"), type: "numeric" },
      ],
      rows: [
        ["01", 42, 39, 35, 40], ["02", 46, 42, 37, 44],
        ["03", 45, 46, 39, 48], ["04", 53, 49, 44, 51],
        ["05", 57, 53, 47, 56], ["06", 55, 57, 49, 60],
        ["07", 63, 60, 54, 64], ["08", 68, 64, 58, 69],
      ].map(([period, revenue, plan, prior, forecast]) => ({
        period: `2026-${period}`,
        revenue,
        plan,
        prior,
        forecast,
      })),
    };
  }
  if (scenario === "comparison") {
    return {
      kind: "hbar",
      title: t("previewData.comparisonTitle"),
      subtitle: t("previewData.subtitle"),
      row_count: 5,
      columns: [
        { key: "category", header: t("previewData.category"), type: "categorical" },
        { key: "amount", header: t("previewData.amount"), type: "numeric" },
      ],
      rows: [
        { category: t("previewData.regionEast"), amount: 68 },
        { category: t("previewData.regionSouth"), amount: 57 },
        { category: t("previewData.regionNorth"), amount: 49 },
        { category: t("previewData.regionWest"), amount: 41 },
        { category: t("previewData.regionCentral"), amount: 36 },
      ],
    };
  }
  return {
    kind: "pie",
    title: t("previewData.compositionTitle"),
    subtitle: t("previewData.subtitle"),
    row_count: 5,
    columns: [
      { key: "channel", header: t("previewData.channel"), type: "categorical" },
      { key: "revenue", header: t("previewData.revenue"), type: "numeric" },
    ],
    rows: [
      { channel: t("previewData.direct"), revenue: 42 },
      { channel: t("previewData.ecommerce"), revenue: 26 },
      { channel: t("previewData.partners"), revenue: 18 },
      { channel: t("previewData.enterprise"), revenue: 9 },
      { channel: t("previewData.other"), revenue: 5 },
    ],
  };
}

export function ChartStylePreview({
  themeForMode,
  view = "candidate",
}: {
  themeForMode: (mode: "light" | "dark") => ChartTheme;
  /**
   * What is on screen. `blocked` renders no chart at all: with no publishable
   * snapshot there is nothing truthful to draw, and drawing the product default
   * palette (which is what used to happen) shows the admin a style that belongs
   * to neither their draft nor their published revision.
   */
  view?: "candidate" | "loading" | "lastValid" | "blocked";
}) {
  const t = useTranslations("settings.appearance.chartStyle");
  const v2 = useTranslations("settings.appearance.chartStyleV2");
  const [scenario, setScenario] = useState<Scenario>("trend");
  const [mode, setMode] = useState<"light" | "dark">("light");
  const [width, setWidth] = useState<"chat" | "report">("report");
  const spec = useMemo(() => previewSpec(scenario, t), [scenario, t]);
  // Mount chart overlay menus inside the preview: color variables are inline on this container, inaccessible from body-mounted menus.
  const [viewport, setViewport] = useState<HTMLDivElement | null>(null);
  const theme = themeForMode(mode);
  const tokens = {
    "--c-bg": mode === "dark" ? "#131210" : "#FAF9F6",
    "--c-surface": theme.surface,
    "--c-surface-2": mode === "dark" ? "#211F1B" : "#F3EEE2",
    "--c-line": theme.grid,
    "--c-line-2": theme.gridSoft,
    "--c-ink": theme.ink,
    "--c-ink-2": mode === "dark" ? "#A8A49B" : "#3A342C",
    "--c-ink-3": theme.inkSoft,
    // Menu material is resolved from --c-surface where it is declared (:root), so the preview's own
    // surface would not reach it; overlay menus in the preview use the preview surface instead.
    "--ui-material-bg": "var(--c-surface)",
  } as CSSProperties;

  return (
    <section className="cs-preview" aria-labelledby="chart-style-preview-title">
      <div className="cs-preview__heading">
        <div>
          <span className="cs-kicker">{v2("previewKicker")}</span>
          <h2 id="chart-style-preview-title">{t("preview")}</h2>
        </div>
        {view === "loading" ? (
          <span className="cs-preview__stale" role="status">{v2("previewUpdating")}</span>
        ) : view === "lastValid" ? (
          <span className="cs-preview__stale cs-preview__stale--warn" role="status">
            {v2("previewShowingLastValid")}
          </span>
        ) : null}
      </div>
      <div className="cs-preview__controls">
        <Segment
          label={t("previewScenario")}
          value={scenario}
          options={[
            ["trend", t("trend")],
            ["comparison", t("comparison")],
            ["composition", t("composition")],
          ]}
          onChange={(value) => setScenario(value as Scenario)}
        />
        <Segment
          label={t("previewMode")}
          value={mode}
          options={[["light", t("light")], ["dark", t("dark")]]}
          onChange={(value) => setMode(value as "light" | "dark")}
        />
        <Segment
          label={t("previewWidth")}
          value={width}
          options={[["chat", t("chatWidth")], ["report", t("reportWidth")]]}
          onChange={(value) => setWidth(value as "chat" | "report")}
        />
      </div>
      <div ref={setViewport} className="cs-preview__viewport" data-width={width} style={tokens}>
        {view === "blocked" ? (
          <p className="cs-preview__blocked" role="status">{v2("previewBlocked")}</p>
        ) : (
          <ChartThemePreviewProvider theme={theme}>
            <ChartMenuRootContext.Provider value={viewport}>
              <ChartBlock
                spec={spec}
                surface={width === "chat" ? "chat" : "canvas"}
                renderPlacement="settings-preview"
                renderMode={mode}
              />
            </ChartMenuRootContext.Provider>
          </ChartThemePreviewProvider>
        )}
      </div>
    </section>
  );
}

function Segment({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}) {
  return (
    <SegmentedControl className="cs-segment" aria-label={label} value={value} onValueChange={onChange}>
      {options.map(([option, text]) => (
        <SegmentOption key={option} value={option}>
          {text}
        </SegmentOption>
      ))}
    </SegmentedControl>
  );
}
