/**
 * Fill-pattern textures: the second channel for filled marks (bar, stacked bar, area, pie slice).
 *
 * Lines already had one (dash + point style); filled marks had none, so a 7–16 series bar chart
 * leaned on color alone (audit 2026-09-28). Which series gets which pattern is decided by the
 * presentation resolver (`assignSeriesEncodings`, mirrored on the server so exports match);
 * this module only paints it.
 *
 * Contrast: the stroke is chosen per fill by measured WCAG contrast (`patternStrokeFor`), ≥3:1
 * against the color actually painted. Full-strength palette colors already pass 3:1 against the
 * chart surface, so they keep surface-colored strokes; faded non-lead fills (`mutedOf`, ~1.5:1
 * against the surface) and the 20% stacked-area tint fall through to ink.
 */

import type { ChartFillPattern } from "./chart-style-presentation";
import { parseHexColor } from "./color";

/** Minimum WCAG contrast between a pattern stroke and the fill under it. */
export const PATTERN_STROKE_MIN_CONTRAST = 3;

function channelLuminance(value: number): number {
  const channel = value / 255;
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
}

function luminance(rgb: [number, number, number]): number {
  return 0.2126 * channelLuminance(rgb[0]) + 0.7152 * channelLuminance(rgb[1]) + 0.0722 * channelLuminance(rgb[2]);
}

/** The opaque color a (possibly `#RRGGBBAA`) fill shows over `backdrop`. */
function composited(fill: string, backdrop: string): [number, number, number] | null {
  const rgb = parseHexColor(fill);
  if (!rgb) return null;
  const alphaHex = /^#[0-9a-f]{8}$/i.test(fill.trim()) ? fill.trim().slice(7, 9) : null;
  const under = parseHexColor(backdrop);
  if (alphaHex === null || !under) return rgb;
  const alpha = parseInt(alphaHex, 16) / 255;
  return rgb.map((value, index) => Math.round(value * alpha + under[index]! * (1 - alpha))) as [number, number, number];
}

export function contrastRatio(first: [number, number, number], second: [number, number, number]): number {
  const [high, low] = [luminance(first), luminance(second)].sort((a, b) => b - a) as [number, number];
  return (high + 0.05) / (low + 0.05);
}

/**
 * First candidate with ≥3:1 against the painted fill (translucent fills composited over
 * `backdrop`); if none qualifies, the candidate with the highest contrast. Black and white are
 * always appended, so some candidate reaches 3:1 for every opaque fill.
 */
export function patternStrokeFor(fill: string, candidates: readonly string[], backdrop: string): string {
  const painted = composited(fill, backdrop);
  const pool = [...candidates, "#FFFFFF", "#000000"];
  if (!painted) return pool[0]!;
  let best = pool[0]!;
  let bestRatio = -1;
  for (const candidate of pool) {
    const rgb = parseHexColor(candidate);
    if (!rgb) continue;
    const ratio = contrastRatio(rgb, painted);
    if (ratio >= PATTERN_STROKE_MIN_CONTRAST) return candidate;
    if (ratio > bestRatio) {
      best = candidate;
      bestRatio = ratio;
    }
  }
  return best;
}

/** Tile edge in CSS pixels. Small enough to show inside an 8–12px legend box. */
export const FILL_PATTERN_TILE_PX = 6;
const STROKE_WIDTH_PX = 1.25;
const DOT_RADIUS_PX = 1.05;

export interface FillPatternGeometry {
  /** Line segments `[x1, y1, x2, y2]` in tile coordinates. */
  lines: Array<[number, number, number, number]>;
  /** Dots `[cx, cy, r]` in tile coordinates. */
  dots: Array<[number, number, number]>;
}

/** Geometry of one seamless tile. Pure, so the shapes can be pinned in tests. */
export function fillPatternGeometry(pattern: ChartFillPattern, size = FILL_PATTERN_TILE_PX): FillPatternGeometry {
  const rising: FillPatternGeometry["lines"] = [
    [0, size, size, 0],
    [-size / 2, size / 2, size / 2, -size / 2],
    [size / 2, size * 1.5, size * 1.5, size / 2],
  ];
  const falling: FillPatternGeometry["lines"] = [
    [0, 0, size, size],
    [-size / 2, size / 2, size / 2, size * 1.5],
    [size / 2, -size / 2, size * 1.5, size / 2],
  ];
  switch (pattern) {
    case "diagonal":
      return { lines: rising, dots: [] };
    case "crosshatch":
      return { lines: [...rising, ...falling], dots: [] };
    case "dots":
      return {
        lines: [],
        dots: [
          [size / 4, size / 4, DOT_RADIUS_PX],
          [(size * 3) / 4, (size * 3) / 4, DOT_RADIUS_PX],
        ],
      };
    case "solid":
      return { lines: [], dots: [] };
  }
}

type TileContext = Pick<
  CanvasRenderingContext2D,
  "fillStyle" | "strokeStyle" | "lineWidth" | "lineCap" | "fillRect" | "beginPath" | "moveTo" | "lineTo" | "stroke" | "arc" | "fill" | "scale"
>;

/** Paint one tile (fill, then texture) onto `context`, already sized `size * scale` device pixels. */
export function paintFillPatternTile(
  context: TileContext,
  pattern: ChartFillPattern,
  fill: string,
  stroke: string,
  scale = 1,
  size = FILL_PATTERN_TILE_PX,
): void {
  context.scale(scale, scale);
  context.fillStyle = fill;
  context.fillRect(0, 0, size, size);
  const geometry = fillPatternGeometry(pattern, size);
  context.strokeStyle = stroke;
  context.fillStyle = stroke;
  context.lineWidth = STROKE_WIDTH_PX;
  context.lineCap = "square";
  for (const [x1, y1, x2, y2] of geometry.lines) {
    context.beginPath();
    context.moveTo(x1, y1);
    context.lineTo(x2, y2);
    context.stroke();
  }
  for (const [cx, cy, radius] of geometry.dots) {
    context.beginPath();
    context.arc(cx, cy, radius, 0, Math.PI * 2);
    context.fill();
  }
}

const cache = new Map<string, CanvasPattern>();

function devicePixelRatio(): number {
  const ratio = typeof window !== "undefined" ? window.devicePixelRatio : 1;
  return Number.isFinite(ratio) && ratio > 0 ? Math.min(4, Math.ceil(ratio)) : 1;
}

/**
 * The Chart.js paint for `fill` with `pattern`. `solid`, or an environment without a 2D canvas
 * (SSR, jsdom), returns the color unchanged, so non-browser callers degrade to color only.
 */
export function fillPatternPaint(fill: string, pattern: ChartFillPattern, stroke: string): string | CanvasPattern {
  if (pattern === "solid" || typeof document === "undefined") return fill;
  const ratio = devicePixelRatio();
  const key = `${pattern}\u0000${fill}\u0000${stroke}\u0000${ratio}`;
  const cached = cache.get(key);
  if (cached) return cached;
  const tile = document.createElement("canvas");
  tile.width = FILL_PATTERN_TILE_PX * ratio;
  tile.height = FILL_PATTERN_TILE_PX * ratio;
  let context: CanvasRenderingContext2D | null = null;
  try {
    context = tile.getContext("2d");
  } catch {
    context = null;
  }
  if (!context) return fill;
  paintFillPatternTile(context, pattern, fill, stroke, ratio);
  const paint = context.createPattern(tile, "repeat");
  if (!paint) return fill;
  // Chart.js scales its context by the device pixel ratio; undo it for the tile so the texture
  // stays FILL_PATTERN_TILE_PX CSS pixels and crisp instead of an upscaled bitmap.
  if (ratio !== 1 && typeof DOMMatrix !== "undefined") paint.setTransform(new DOMMatrix().scale(1 / ratio));
  cache.set(key, paint);
  return paint;
}
