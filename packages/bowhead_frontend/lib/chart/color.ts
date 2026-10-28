/**
 * Parse chart colors and compose opacity.
 *
 * ★ Exists because of an actual **production-build-only** incident (R7, caught on 2026-08-14 by the export golden
 *   visual gate): theme colors come from CSS custom properties, and Next production builds
 *   **compress** --c-surface: #ffffff **to #fff**. Thus:
 *
 *   - theme.surface + "eb" → "#fffeb" (five digits, invalid) → canvas retains its previous
 *     fillStyle → **every numeric-label backing renders solid black** (light theme; dark #1b1a17
 *     cannot shorten and remains unaffected);
 *   - mutedOf()'s six-digit-only parseHex returns null for #fff → returns unchanged
 *     → **batch 1 protagonist-layer 30/70 fading has no effect in production**.
 *
 *   Dev CSS remains uncompressed (#ffffff), so local screenshots, unit tests and typecheck all miss it.
 *   See [[feedback_prod_only_build_breakage]].
 *
 * Rule: **never concatenate strings to add opacity to colors**; use withAlpha.
 */

/** Parse #RGB / #RRGGBB / #RRGGBBAA → [r,g,b]; others (rgb()/color-mix()/named colors) → null. */
export function parseHexColor(color: string): [number, number, number] | null {
  const s = color.trim();
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(s);
  if (!m) return null;
  const hex = m[1]!;
  const full = hex.length === 3
    ? hex.split("").map((ch) => ch + ch).join("")
    : hex.slice(0, 6);
  const bytes = full.match(/../g)!.map((b) => parseInt(b, 16));
  return [bytes[0]!, bytes[1]!, bytes[2]!];
}

/** Add opacity to a color. alpha is two hex digits (same convention as before, e.g. "eb" = 92%).
 *
 *  Unparseable colors (rgb() / named colors / color-mix()) **return unchanged**: opaque but always
 *  valid, never an invalid string that makes canvas silently fall back to black. */
export function withAlpha(color: string, alpha: string): string {
  const rgb = parseHexColor(color);
  if (!rgb) return color;
  const hex = rgb.map((v) => v.toString(16).padStart(2, "0")).join("");
  return `#${hex}${alpha}`;
}

function parseRgbFunction(s: string): [number, number, number] | null {
  const m = /^rgba?\(\s*([\d.]+)(%?)[\s,]+([\d.]+)(%?)[\s,]+([\d.]+)(%?)(?:\s*[,/]\s*[\d.]+%?)?\s*\)$/i.exec(s);
  if (!m) return null;
  const channel = (value: string, percent: string) =>
    Math.round(Math.min(255, Math.max(0, percent ? (Number(value) / 100) * 255 : Number(value))));
  return [channel(m[1]!, m[2]!), channel(m[3]!, m[4]!), channel(m[5]!, m[6]!)];
}

function parseHslFunction(s: string): [number, number, number] | null {
  const m = /^hsla?\(\s*(-?[\d.]+)(deg|turn|rad)?[\s,]+([\d.]+)%[\s,]+([\d.]+)%(?:\s*[,/]\s*[\d.]+%?)?\s*\)$/i.exec(s);
  if (!m) return null;
  const unit = (m[2] ?? "deg").toLowerCase();
  const raw = Number(m[1]);
  const deg = unit === "turn" ? raw * 360 : unit === "rad" ? (raw * 180) / Math.PI : raw;
  const h = (((deg % 360) + 360) % 360) / 360;
  const sat = Math.min(1, Number(m[3]) / 100);
  const light = Math.min(1, Number(m[4]) / 100);
  const q = light < 0.5 ? light * (1 + sat) : light + sat - light * sat;
  const p = 2 * light - q;
  const hue = (t: number) => {
    const x = t < 0 ? t + 1 : t > 1 ? t - 1 : t;
    if (x < 1 / 6) return p + (q - p) * 6 * x;
    if (x < 1 / 2) return q;
    if (x < 2 / 3) return p + (q - p) * (2 / 3 - x) * 6;
    return p;
  };
  return [hue(h + 1 / 3), hue(h), hue(h - 1 / 3)].map((v) => Math.round(v * 255)) as [number, number, number];
}

/** Any other CSS color (named, oklch(), lab(), color(), …): let the browser paint it on a 1 × 1 canvas and read the
 *  sRGB pixel back. Null without a 2d context (SSR, jsdom) or when the browser rejects the color. */
function resolveColorByProbe(color: string): [number, number, number] | null {
  if (typeof document === "undefined") return null;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) return null;
    // Sentinel first: an unparseable color leaves fillStyle unchanged, which must not be read as the answer.
    ctx.fillStyle = "#010203";
    ctx.fillStyle = color;
    if (String(ctx.fillStyle).toLowerCase() === "#010203" && color.trim().toLowerCase() !== "#010203") return null;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    if (a === 0) return null;
    return [r!, g!, b!];
  } catch {
    return null;
  }
}

/** sRGB channels of a CSS color: hex, rgb()/rgba() and hsl()/hsla() parsed directly, anything else via the browser. */
export function parseCssColor(color: string): [number, number, number] | null {
  const s = color.trim();
  return parseHexColor(s) ?? parseRgbFunction(s) ?? parseHslFunction(s) ?? resolveColorByProbe(s);
}

/**
 * `color` at `fraction` opacity as an `rgba()` string — the canvas equivalent of
 * `color-mix(in srgb, <color> N%, transparent)`, which canvas cannot parse.
 *
 * Accepts every CSS color (`parseCssColor`): a theme ink written as `hsl()`, `oklch()` or a named color must still
 * yield a translucent separator, not the opaque ink. Only a color nothing can resolve returns unchanged (opaque but
 * valid, same rule as `withAlpha`).
 */
export function colorAtOpacity(color: string, fraction: number): string {
  const rgb = parseCssColor(color);
  if (!rgb) return color;
  const alpha = Math.round(Math.min(1, Math.max(0, fraction)) * 1000) / 1000;
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}

/** Interpolate two colors by t (t=0 entirely a, t=1 entirely b). On parse failure, return a unchanged. */
export function mixHex(a: string, b: string, t: number): string {
  const ca = parseHexColor(a);
  const cb = parseHexColor(b);
  if (!ca || !cb) return a;
  const m = ca.map((v, i) => Math.round(v + (cb[i]! - v) * t));
  return `#${m.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * Color of the ith series. Use only the server-validated effective palette.
 *
 * Why (23 charts measured from the 1948-chart corpus on 116): palette[i % 6] inevitably repeats
 * colors beyond six series, claiming to distinguish 15–20 series with six colors. These series cannot be caught
 * by the backend over-limit-series-to-table gate, which counts **numeric columns**;
 * excess series arise from **row values** (frontend pivotLongSpec expands long tables,
 * buildBar groups by a second dimension, force_kind bypasses the gate), structurally invisible to backend.
 * Handing this to that backend gate is an unreachable handoff on this path.
 *
 * Do not silently truncate to six here either: that swaps repeated colors for nine omitted series without explanation,
 * exactly the silent truncation this sprint eliminates. Gateway alone implements actual-surface
 * repair; browsers must not mix colors to derive an un-QA'd N+1th HEX. Wrap stably beyond capacity,
 * while presentation resolver adds non-color encoding such as line/point styles or boundaries.
 */
export function seriesColor(
  palette: readonly string[],
  i: number,
  _surface: string,
  ink: string,
): string {
  const n = palette.length;
  if (n === 0) return ink;
  return palette[((i % n) + n) % n] ?? ink;
}
