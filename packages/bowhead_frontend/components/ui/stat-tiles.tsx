"use client";

import type { CSSProperties, ReactNode } from "react";

import { formatPrecise, kpiValueLines } from "@/lib/chart/format";

import styles from "./stat-tiles.module.css";

/**
 * Page-level stat tile: label · main value · secondary line (Chart Embed Spec D6).
 *
 * One hairline band shares a single surface; tiles are separated by hairlines, the main value is 28 / 600 tabular.
 * The secondary line is a note (12, tertiary ink) or a link (passed in by the host). Values from one million up follow
 * the report KPI rule (`kpiValueLines`, option 1c): magnitude on the main line, every digit on a monospace line
 * beneath. A value that could not be read shows "—", never 0. Below 760px the band becomes 2 × 2.
 *
 * Not `ChartSpec.kind = "kpi"`: that is report content (`ChartKPI`), which shares the value rule but keeps its CSS.
 */
export interface StatTile {
  id: string;
  label: ReactNode;
  /** `null` while unread or unavailable → "—". */
  value: number | null;
  unit?: string;
  /** Override the default precise formatting (e.g. the interface locale's `format.number`). */
  format?: (value: number) => string;
  /** Note text or a link element. */
  secondary?: ReactNode;
  secondaryKind?: "note" | "link";
}

export function StatTiles({
  tiles,
  busy,
  className,
  ...aria
}: {
  tiles: readonly StatTile[];
  busy?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
}) {
  return (
    <dl className={[styles.band, className].filter(Boolean).join(" ")} aria-busy={busy || undefined} {...aria}>
      {tiles.map((tile) => {
        const display = tile.value === null
          ? "—"
          : tile.format ? tile.format(tile.value) : formatPrecise(tile.value, tile.unit);
        const { main, exact, swapped, ems } = tile.value === null
          ? { main: display, exact: null, swapped: false, ems: 1 }
          : kpiValueLines(tile.value, display, { unit: tile.unit });
        return (
          <div key={tile.id} className={styles.tile} data-stat-tile={tile.id}>
            <dt className={styles.label}>{tile.label}</dt>
            <dd
              className={styles.value}
              data-stat-swapped={swapped ? "" : undefined}
              style={{ "--stat-ems": ems } as CSSProperties}
            >
              {main}
            </dd>
            {exact !== null ? <dd className={styles.exact}>{exact}</dd> : null}
            {tile.secondary ? (
              <dd className={tile.secondaryKind === "link" ? styles.link : styles.note}>{tile.secondary}</dd>
            ) : null}
          </div>
        );
      })}
    </dl>
  );
}
