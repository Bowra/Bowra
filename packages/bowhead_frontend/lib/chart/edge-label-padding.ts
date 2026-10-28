/**
 * Numeric labels at category-axis ends can protrude beyond canvas — measure and pad only charts that overflow.
 *
 * ☠ 116 dogfood (1.2.45) photo: exported line chart's final label -76.9 lost half its 9 at the right edge,
 * making it read as −76. Before batch 2 the export surface had **no labels**, so batch 2 newly exposed this issue.
 *
 * Why other approaches do not work:
 * - datalabels clamp (already enabled in labelBase) clips the **anchor segment**
 *   (clipped(segment, area) in dist compute$1), and the point is already inside plot area
 *   ⇒ **ineffective** here; it does not move label boxes either (batch 2 confirmed: hide only, no movement).
 * - Category-axis offset: true moves first/last points inward, but **changes the chart's appearance**.
 *
 * Why measurement is required rather than unconditional padding:
 * Unconditional padding changes plot geometry of **every labeled chart** (77% of corpus exports have labels),
 * and deploy/export-goldens pixel baselines can only be regenerated inside the image
 * (canvas-export-visual-smoke.sh explicitly requires running inside the image).
 * After measurement, charts that already fit always receive 0 padding, unchanged pixel by pixel.
 */

/** Required padding on one side. 0 = this chart already fits; leave it alone. */
export interface EdgeLabelPadding {
  left: number;
  right: number;
}

export interface EdgeLabelPaddingInput {
  /**
   * Natural gaps between plot-area ends and canvas edges **excluding padding added by this function**.
   *
   * ★ Pass natural gaps, not gaps computed directly from current chartArea; otherwise the fixed point oscillates:
   * measuring after padding yields 0 overflow ⇒ removes padding ⇒ clipping returns next frame.
   * With natural gaps, results are **independent** of their own effect; one relayout converges.
   */
  readonly gapLeft: number;
  readonly gapRight: number;
  /**
   * Pixel width of the **widest** label in the first/last category (including its own horizontal padding).
   * 0 = no label at that end.
   *
   * Caller must measure width using the label's **actual font** (leadAwareLabelStyle uses different sizes
   * for the protagonist and other labels); character counts fail for mixed Chinese/English — batch 1 already failed this way.
   */
  readonly firstWidth: number;
  readonly lastWidth: number;
}

/** Labels centered on first/last categories protrude halfway; pad by overflow + 1px to avoid touching edge. */
export function edgeLabelPadding(input: EdgeLabelPaddingInput): EdgeLabelPadding {
  const need = (width: number, gap: number): number => {
    // No separate width <= 0 check: past > 0 excludes zero/negative values already (mutation comparison exposed a dead condition).
    // Number.isFinite is live — Infinity would yield infinite padding and squeeze the entire chart away.
    if (!Number.isFinite(width)) return 0;
    const past = width / 2 - gap;
    return past > 0 ? Math.ceil(past) + 1 : 0;
  };
  return {
    left: need(input.firstWidth, input.gapLeft),
    right: need(input.lastWidth, input.gapRight),
  };
}

/**
 * Top edge: upward numeric labels (above line points or vertical bar tops) can protrude when touching plot top.
 *
 * ☠ 116 phone photo: first-half revenue line maximum 19.9 nearly touches y-axis maximum 20, leaving only half the label,
 * whose background also covers half of tick "20". Both sides were padded already; the top was unhandled.
 *
 * gapTop: distance from highest upward label's anchor to canvas top **excluding this function's padding** (same natural gap
 * as horizontal sides; rationale in EdgeLabelPaddingInput). height: label-box height plus its anchor gap.
 * Return 0 when it fits (pixel-identical); otherwise add the shortfall + 1px.
 */
export function topLabelPadding(gapTop: number, height: number): number {
  if (!Number.isFinite(gapTop) || !Number.isFinite(height)) return 0;
  const past = height - gapTop;
  return past > 0 ? Math.ceil(past) + 1 : 0;
}

/** Axis-aligned rectangle in canvas coordinates (CSS pixels, y downward). */
export interface LabelBox {
  readonly left: number;
  readonly top: number;
  readonly right: number;
  readonly bottom: number;
}

/** Ink box of a y-axis tick label; side identifies which side of plot area the axis occupies. */
export interface AxisTickTextBox extends LabelBox {
  readonly side: "left" | "right";
}

/**
 * Choose an alternative angle when point labels (above/below line points) overlap y-axis tick text.
 *
 * ☠ 116 phone photo: first-half revenue line's first point 19.9 touches top y-axis tick 20. Top padding prevents clipping
 * at canvas top, but label remains centered above the first point — at plot area's left edge, so its left half enters
 * the y-axis tick region, with its background covering the right half of "20".
 *
 * Why angle sideways on conflict rather than another approach:
 * - y-axis grace / suggestedMax moves the highest point down, but centered label still enters tick region,
 *   now covering "18"; it also changes ticks on every chart whose maximum touches top, shifting all export pixel baselines.
 * - Horizontal layout.padding is outside the whole chart; tick region moves with it, preserving relative positions.
 * - Angling sideways moves only the conflicting label diagonally away from axis (left -45°, right -135°; downward labels
 *   mirror to 45° / 135°). At |vx| ≥ |vy| datalabels places the full box on the anchor's side away from axis;
 *   45° is the closest qualifying angle to straight up, keeping the label above and nearest its point.
 *
 * Return null = no conflict (or overlapping both sides, unfixable by angling); **do not move** this label —
 * other labels and nonconflicting charts remain pixel-identical. Boxes touching at a shared edge do not conflict.
 */
export function tickAvoidAlign(
  frame: LabelBox,
  align: "top" | "bottom",
  ticks: readonly AxisTickTextBox[],
): number | null {
  const sides = new Set<"left" | "right">();
  for (const tick of ticks) {
    const overlaps = frame.left < tick.right
      && tick.left < frame.right
      && frame.top < tick.bottom
      && tick.top < frame.bottom;
    if (overlaps) sides.add(tick.side);
  }
  if (sides.size !== 1) return null;
  const awayFromLeft = sides.has("left");
  if (align === "top") return awayFromLeft ? -45 : -135;
  return awayFromLeft ? 45 : 135;
}
