"use client";

import { useLocale, useTranslations } from "next-intl";
import { useChartTheme } from "@/lib/chart-theme";
import { analyticalValue, intervalPosition, type AnalyticalView } from "@/lib/chart/analytical";
import { seriesColor } from "@/lib/chart/color";
import type { ChartSpec } from "@/lib/chart/types";
import type { TypedCell } from "@/lib/chart/linked-focus";

/** The existing Table owns rows, sorting, scroll and selection; this cell owns marks only. */
export function ChartIntervalCell({ spec, profile, row, width = 320 }: { spec: ChartSpec; profile: AnalyticalView; row?: Record<string, TypedCell>; width?: number }) {
  const locale = useLocale();
  const theme = useChartTheme();
  const t = useTranslations("chart_block");
  const domain = profile.binding.value_domain;
  const scale = (value: number) => intervalPosition(value, domain, width);
  const ticks = Array.from({ length: 5 }, (_, index) => domain[0] + (domain[1] - domain[0]) * index / 4);
  const bounds = profile.range!;
  const low = row?.[bounds.lower_key], high = row?.[bounds.upper_key];
  const description = row ? [...new Set([...profile.binding.observation_keys, ...(profile.group_key ? [profile.group_key] : []), ...profile.binding.measure_keys, ...(bounds.coverage_key ? [bounds.coverage_key] : []), ...(bounds.origin_key ? [bounds.origin_key] : []), ...(profile.detail_keys ?? [])])].map((key) => `${spec.columns.find((column) => column.key === key)?.header ?? key}: ${analyticalValue(spec, key, row[key], locale)}`).join("; ") : t(`analytical_range_${bounds.kind}`);
  const color = seriesColor(theme.palette, 0, theme.surface, theme.ink);
  const estimated = bounds.origin_key && row?.[bounds.origin_key] === "estimated";
  return <svg width={width} height={row ? 36 : 46} viewBox={`0 0 ${width} ${row ? 36 : 46}`} role="img" aria-label={description} data-chart-interval={row ? "row" : "axis"} style={{ display: "block", width: "100%", minWidth: 320 }}>
    <title>{description}</title>
    {ticks.map((tick, i) => <g key={i}><line x1={scale(tick)} x2={scale(tick)} y1={row ? 0 : 35} y2={row ? 36 : 46} stroke={theme.gridSoft} />{!row ? <text x={scale(tick)} y={23} textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"} fontSize="10" fill={theme.inkSoft}>{analyticalValue(spec, bounds.lower_key, Number(tick.toPrecision(4)), locale, false)}</text> : null}</g>)}
    {typeof low === "number" && typeof high === "number" ? low === high
      ? <circle cx={scale(low)} cy={18} r={4} fill={color} />
      : <line x1={scale(low)} x2={scale(high)} y1={18} y2={18} stroke={color} strokeWidth={9} strokeOpacity={estimated ? 0.38 : 0.62} strokeDasharray={estimated ? "10 4" : undefined} strokeLinecap={estimated ? "butt" : "round"} /> : null}
    {(profile.points ?? []).map((point) => {
      const value = row?.[point.key];
      if (typeof value !== "number") return null;
      const x = scale(value);
      return point.role === "reference"
        ? <path key={point.key} d={`M ${x} 12 l 6 6 l -6 6 l -6 -6 Z`} fill={seriesColor(theme.palette, 1, theme.surface, theme.ink)} stroke={theme.surface} strokeWidth={1.5} />
        : <circle key={point.key} cx={x} cy={18} r={point.role === "primary" ? 5 : 4} fill={point.role === "primary" ? theme.ink : theme.inkSoft} stroke={theme.surface} strokeWidth={1.5} />;
    })}
  </svg>;
}


export function ChartIntervalLegend({ spec, profile }: { spec: ChartSpec; profile: AnalyticalView }) {
  const theme = useChartTheme(), t = useTranslations("chart_block");
  const color = seriesColor(theme.palette, 0, theme.surface, theme.ink);
  return <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" style={{ color: theme.inkSoft }} data-chart-interval-legend>
    <span className="inline-flex items-center gap-1"><svg width="22" height="12" aria-hidden><line x1="4" x2="18" y1="6" y2="6" stroke={color} strokeWidth="6" strokeLinecap="round" /></svg>{t(`analytical_range_${profile.range!.kind}`)}</span>
    {profile.range!.origin_key ? <span className="inline-flex items-center gap-1"><svg width="22" height="12" aria-hidden><line x1="3" x2="19" y1="6" y2="6" stroke={color} strokeWidth="4" strokeDasharray="5 3" /></svg>{t("analytical_estimated")}</span> : null}
    {(profile.points ?? []).map((point) => <span className="inline-flex items-center gap-1" key={point.key}><svg width="14" height="12" aria-hidden>{point.role === "reference" ? <path d="M7 1L12 6L7 11L2 6Z" fill={seriesColor(theme.palette, 1, theme.surface, theme.ink)} /> : <circle cx="7" cy="6" r={point.role === "primary" ? 4 : 3} fill={point.role === "primary" ? theme.ink : theme.inkSoft} />}</svg>{spec.columns.find((column) => column.key === point.key)?.header}</span>)}
  </div>;
}
