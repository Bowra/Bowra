"use client";

/**
 * Interface icons (P2 batch 2: icons; unconditional since the 1.5.0 release).
 *
 * Every interface icon is a Lucide (ISC) glyph. Its box starts from the size the call site asks for and
 * snaps to the 14 / 16 / 20 tiers (P2 batch 3, `snapIconSize`), and `aria-hidden`, `focusable`, `role`,
 * `aria-label` and `className` pass through unchanged. Icons without `aria-hidden` stay without it: Lucide
 * adds `aria-hidden="true"` when no aria prop is present, so the key is always passed explicitly to turn
 * that off. Accessible names of icon buttons do not change.
 *
 * Stroke is 1.5 screen pixels (design spec: 14 / 16 / 20 tiers all 1.5, confirmed 2026-09-26); inline
 * markers below 13px scale down with the 14px tier ratio (spec: "12 is only for inline markers, stroke per
 * the 14 tier"). Uses Lucide `nonScalingStroke` (`vector-effect: non-scaling-stroke` on each child)
 * rather than `absoluteStrokeWidth`: the latter derives from the width/height attributes, so the line
 * width drifts when call-site CSS resizes the icon; the former uses the rendered size. The stroke is also
 * written inline so it wins over call-site CSS that tunes `stroke-width`. Colour follows text
 * (`currentColor`).
 */

import type { LucideIcon } from "lucide-react";
import type { ReactElement, SVGProps } from "react";


export type SvgIconProps = SVGProps<SVGSVGElement>;

/**
 * Icon size tiers (P2 batch 3, user decision 2026-09-30): 14 next to 12–13px text and in
 * small buttons, 16 is the default (navigation rows, menu items, buttons, topbar), 20 for the icon rail,
 * touch navigation, the mobile topbar and empty states.
 */
export const ICON_SIZE_TIERS = [14, 16, 20] as const;

/**
 * Snaps an icon size to the nearest tier. Ties (15, 18) resolve to the default tier 16; anything
 * smaller than 14 becomes 14 and anything larger than 20 becomes 20. Call sites whose role needs a different
 * tier than the nearest one are adjusted in CSS (`styles/ui-refresh-chrome.css`).
 */
export function snapIconSize(size: number): number {
  if (!Number.isFinite(size) || size <= 0) return size;
  if (size < 15) return 14;
  if (size <= 18) return 16;
  return 20;
}

/** Numeric pixel value of a width/height prop, or null for non-pixel values such as `1em`. */
function pixelValue(value: number | string): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  return /^\d+(\.\d+)?$/.test(value.trim()) ? Number(value) : null;
}

/** Stroke width (screen pixels) for interface icons. */
export function refreshStrokeWidth(size: number): number {
  if (!Number.isFinite(size) || size <= 0 || size >= 13) return 1.5;
  return Math.round(((1.5 * size) / 14) * 100) / 100;
}

/**
 * Lucide icon on the size tiers. `width` / `height` are the size the call site asks for; `props` are the
 * caller's SVG props (a drawing `strokeWidth` is dropped).
 * `strokeOverride` is a CSS stroke-width written to the inline style instead of the size-derived value,
 * for boxes whose size is not a number of pixels (e.g. `1em` glyphs, see components/char-icon.tsx).
 */
export function renderRefreshSvg(
  Icon: LucideIcon,
  width: number | string,
  height: number | string,
  props: SvgIconProps = {},
  strokeOverride?: string,
): ReactElement {
  const { className, style, strokeWidth: _drawingStroke, width: _w, height: _h, ...rest } = props;
  void _drawingStroke;
  void _w;
  void _h;
  // Square pixel boxes snap to the 14 / 16 / 20 tiers (so every snapped icon strokes at 1.5px); non-square
  // boxes (the 10 x 14 drag handle) and non-pixel boxes (`1em` glyphs) keep their size.
  const w = pixelValue(width);
  const h = pixelValue(height);
  const square = w !== null && w === h;
  const boxWidth = square ? snapIconSize(w) : width;
  const boxHeight = square ? snapIconSize(w) : height;
  const stroke = refreshStrokeWidth(Number(boxWidth));
  return (
    <Icon
      aria-hidden={props["aria-hidden"]}
      {...rest}
      width={boxWidth}
      height={boxHeight}
      strokeWidth={stroke}
      nonScalingStroke
      className={className ? `bh-icon ${className}` : "bh-icon"}
      style={{ ...style, strokeWidth: strokeOverride ?? stroke }}
    />
  );
}

/** Inline Lucide icon: `size` / `height` are the box the call site asks for; other SVG props pass through. */
export function LucideGlyph({
  icon,
  size,
  height,
  ...props
}: SvgIconProps & { icon: LucideIcon; size: number; height?: number }): ReactElement {
  return renderRefreshSvg(icon, size, height ?? size, props);
}

type SizedIconProps = SvgIconProps & { size?: number };

/**
 * Icon component for "size + SVG props" icon sets such as `components/icons.tsx`. `defaultSize` is the box
 * when no size is passed; `sized: false` means the icon has a fixed box and ignores `size`; `defaults` are
 * non-drawing props the caller can override (e.g. a default `aria-hidden`).
 */
export function lucideIcon<P extends SizedIconProps>(
  Next: LucideIcon,
  defaultSize: number,
  options: { sized?: boolean; defaults?: SvgIconProps } = {},
): (props: P) => ReactElement {
  const sized = options.sized !== false;
  function InterfaceIcon(props: P): ReactElement {
    const { size, ...svg } = props;
    const box = sized && size != null ? size : defaultSize;
    return renderRefreshSvg(Next, svg.width ?? box, svg.height ?? box, { ...options.defaults, ...svg });
  }
  InterfaceIcon.displayName = `Icon(${Next.displayName ?? "Icon"})`;
  return InterfaceIcon;
}
