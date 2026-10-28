"use client";

import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

export interface CapabilityChip {
  id: string;
  label: string;
  /** Extra inline content after the label (e.g. a "pending review" note). */
  note?: ReactNode;
}

/** Greedy wrap of `widths` into rows of `rowWidth`; returns the number of rows used. A chip wider than a row takes a row of its own (it ellipsizes). */
function rowsUsed(widths: readonly number[], rowWidth: number, gap: number): number {
  let rows = 0;
  let x = 0;
  for (const width of widths) {
    if (rows === 0 || x + gap + width > rowWidth) {
      rows += 1;
      x = Math.min(width, rowWidth);
    } else {
      x += gap + width;
    }
  }
  return rows;
}

/**
 * How many chips fit in `maxRows` wrapped rows before the rest collapse into "+N".
 *
 * `chipWidths` are the measured widths of every chip in order, `gap` the space between chips (both axes), and
 * `moreWidth(n)` the width of the "+n" chip. Returns the number of leading chips to show; when every chip fits, no
 * "+N" is needed. Never returns less than 1 for a non-empty list: a lone chip wider than the row ellipsizes instead.
 */
export function visibleChipCount(
  chipWidths: readonly number[],
  containerWidth: number,
  gap: number,
  moreWidth: (hidden: number) => number,
  maxRows = 1,
): number {
  const total = chipWidths.length;
  if (total === 0) return 0;
  if (rowsUsed(chipWidths, containerWidth, gap) <= maxRows) return total;
  for (let shown = total - 1; shown >= 1; shown -= 1) {
    if (rowsUsed([...chipWidths.slice(0, shown), moreWidth(total - shown)], containerWidth, gap) <= maxRows) return shown;
  }
  return 1;
}

/**
 * Capability chips in at most two rows, each showing its full label: chips that do not fit merge into an outlined "+N" chip whose tooltip lists every
 * capability on hover or focus. N comes from measurement (ResizeObserver), not a fixed cut-off.
 */
/** Rows of chips before the rest collapse into "+N"; matches the wrapped row in agent-marketplace.css. */
const CHIP_ROWS = 2;

export function CapabilityChips({ chips, label }: { chips: CapabilityChip[]; label: string }) {
  const t = useTranslations("agents_marketplace");
  const rowRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(chips.length);
  const tipId = useId();
  const key = chips.map((chip) => chip.id).join("|");

  useLayoutEffect(() => {
    const row = rowRef.current;
    const measure = measureRef.current;
    if (!row || !measure) return;
    const update = () => {
      const chipNodes = Array.from(measure.querySelectorAll<HTMLElement>("[data-measure-chip]"));
      const moreNode = measure.querySelector<HTMLElement>("[data-measure-more]");
      const gap = Number.parseFloat(getComputedStyle(row).columnGap) || 0;
      const widths = chipNodes.map((node) => node.getBoundingClientRect().width);
      const moreBase = moreNode?.getBoundingClientRect().width ?? 0;
      const width = row.getBoundingClientRect().width;
      // Rows without layout (hidden tabs, jsdom) keep every chip rather than collapsing to "+N".
      if (width <= 0) {
        setShown(chips.length);
        return;
      }
      setShown(visibleChipCount(widths, width, gap, () => moreBase, CHIP_ROWS));
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(row);
    return () => observer.disconnect();
    // `key` captures the chip identities; the chip objects themselves are rebuilt on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, chips.length]);

  if (!chips.length) return null;
  const hidden = chips.length - shown;
  return (
    <div ref={rowRef} className="am-capability-row" role="group" aria-label={label}>
      {chips.slice(0, shown).map((chip) => (
        <span key={chip.id} className="am-capability-chip" title={chip.label}>
          <span className="am-capability-chip-label">{chip.label}</span>
          {chip.note}
        </span>
      ))}
      {hidden > 0 ? (
        // A focusable button names how many capabilities are hidden and points at the tooltip listing all of them.
        <button type="button" className="am-capability-more" aria-label={t("capability_more_label", { count: hidden })} aria-describedby={tipId}>
          <span aria-hidden="true">+{hidden}</span>
          <span id={tipId} className="am-capability-more-tip" role="tooltip">
            {chips.map((chip) => chip.label).join(" · ")}
          </span>
        </button>
      ) : null}
      <div ref={measureRef} className="am-capability-measure" aria-hidden="true">
        {chips.map((chip) => (
          <span key={chip.id} className="am-capability-chip" data-measure-chip="">
            <span className="am-capability-chip-label">{chip.label}</span>
            {chip.note}
          </span>
        ))}
        <span className="am-capability-more" data-measure-more="">+{chips.length}</span>
      </div>
    </div>
  );
}
