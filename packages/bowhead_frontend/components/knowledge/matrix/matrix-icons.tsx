/**
 * kb-matrix — icon set needed by the matrix (grid badges / panel / popover), as Lucide glyphs.
 */

import type { LucideIcon } from "lucide-react";
import {
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  Clock as LucideClock,
  Ellipsis as LucideEllipsis,
  File as LucideFile,
  Folder as LucideFolder,
  LoaderCircle as LucideLoaderCircle,
  Pencil as LucidePencil,
  Play as LucidePlay,
  Plus as LucidePlus,
  Quote as LucideQuote,
  RefreshCw as LucideRefreshCw,
  Share as LucideShare,
  Sheet as LucideSheet,
  Sparkles as LucideSparkles,
  TriangleAlert as LucideTriangleAlert,
  X as LucideX,
} from "lucide-react";
import type { CSSProperties } from "react";

import { renderRefreshSvg } from "@/components/icon-refresh";

interface IconProps {
  s?: number;
  style?: CSSProperties;
}

/** P2 batch 2: Lucide glyph on the icon size tiers (see `components/icon-refresh.tsx`). */
function icon(next: LucideIcon) {
  return function Icon({ s = 16, style }: IconProps) {
    return renderRefreshSvg(next, s, s, { style, "aria-hidden": true });
  };
}

export const IcBack = /* @__PURE__ */ icon(LucideChevronLeft);
export const IcShare = /* @__PURE__ */ icon(LucideShare);
export const IcX = /* @__PURE__ */ icon(LucideX);
export const IcChevR = /* @__PURE__ */ icon(LucideChevronRight);
export const IcQuote = /* @__PURE__ */ icon(LucideQuote);
export const IcCheck = /* @__PURE__ */ icon(LucideCheck);
export const IcPencil = /* @__PURE__ */ icon(LucidePencil);
export const IcWarn = /* @__PURE__ */ icon(LucideTriangleAlert);
export const IcDoc = /* @__PURE__ */ icon(LucideFile);
export const IcGrid = /* @__PURE__ */ icon(LucideSheet);

// ── M2 additions ──
export const IcPlay = /* @__PURE__ */ icon(LucidePlay);
export const IcCaret = /* @__PURE__ */ icon(LucideChevronDown);
export const IcPlus = /* @__PURE__ */ icon(LucidePlus);
export const IcDots = /* @__PURE__ */ icon(LucideEllipsis);
export const IcClock = /* @__PURE__ */ icon(LucideClock);
export const IcRefresh = /* @__PURE__ */ icon(LucideRefreshCw);
export const IcSpark = /* @__PURE__ */ icon(LucideSparkles);
export const IcFolder = /* @__PURE__ */ icon(LucideFolder);

export function IcSpin({ s = 14, style }: { s?: number; style?: CSSProperties }) {
  return renderRefreshSvg(LucideLoaderCircle, s, s, { className: "mx-spin", style, "aria-hidden": true });
}
