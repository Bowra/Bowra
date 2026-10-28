"use client";

/**
 * kb-management M1–M5 · shared inline SVG icons.
 * Ported from the Claude Design handoff (kb/kb-desktop.js `I`).
 *
 * P2 batch 2: Lucide glyphs on the icon size tiers (see `components/icon-refresh.tsx`).
 */
import type { CSSProperties } from "react";
import {
  ArrowDown as LucideArrowDown,
  ArrowUp as LucideArrowUp,
  ArrowUpDown as LucideArrowUpDown,
  Calendar as LucideCalendar,
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  Download as LucideDownload,
  EllipsisVertical as LucideEllipsisVertical,
  Eye as LucideEye,
  Folder as LucideFolder,
  LoaderCircle as LucideLoaderCircle,
  MessageSquare as LucideMessageSquare,
  RotateCw as LucideRotateCw,
  Search as LucideSearch,
  SlidersHorizontal as LucideSlidersHorizontal,
  Tag as LucideTag,
  Trash2 as LucideTrash2,
  Upload as LucideUpload,
  X as LucideX,
} from "lucide-react";

import { lucideIcon, renderRefreshSvg } from "@/components/icon-refresh";

type IconProps = { size?: number; style?: CSSProperties };

export const IconMag = /* @__PURE__ */ lucideIcon(LucideSearch, 13);
export const IconCaret = /* @__PURE__ */ lucideIcon(LucideChevronDown, 10);
export const IconCheck = /* @__PURE__ */ lucideIcon(LucideCheck, 9);
export const IconCheckBig = /* @__PURE__ */ lucideIcon(LucideCheck, 11);
export const IconX = /* @__PURE__ */ lucideIcon(LucideX, 11);
export const IconChevR = /* @__PURE__ */ lucideIcon(LucideChevronRight, 10);
export const IconArrowUp = /* @__PURE__ */ lucideIcon(LucideArrowUp, 11);
export const IconArrowDn = /* @__PURE__ */ lucideIcon(LucideArrowDown, 11);
export const IconDots = /* @__PURE__ */ lucideIcon(LucideEllipsisVertical, 15);
const SPIN_STYLE: CSSProperties = { animation: "kb-spin 0.9s linear infinite", transformOrigin: "50% 50%" };
/** The spin lives in the icon's inline style. */
export const IconSpin = ({ size = 10, style }: IconProps) =>
  renderRefreshSvg(LucideLoaderCircle, size, size, { style: { ...SPIN_STYLE, ...style } });
export const IconRetry = /* @__PURE__ */ lucideIcon(LucideRotateCw, 11);
export const IconTrash = /* @__PURE__ */ lucideIcon(LucideTrash2, 13);
export const IconFolder = /* @__PURE__ */ lucideIcon(LucideFolder, 13);
export const IconTag = /* @__PURE__ */ lucideIcon(LucideTag, 13);
export const IconSliders = /* @__PURE__ */ lucideIcon(LucideSlidersHorizontal, 13);
export const IconCal = /* @__PURE__ */ lucideIcon(LucideCalendar, 13);
export const IconSort = /* @__PURE__ */ lucideIcon(LucideArrowUpDown, 13);
export const IconUp = /* @__PURE__ */ lucideIcon(LucideUpload, 13);
export const IconChevL = /* @__PURE__ */ lucideIcon(LucideChevronLeft, 16);
export const IconEye = /* @__PURE__ */ lucideIcon(LucideEye, 13);
export const IconDownload = /* @__PURE__ */ lucideIcon(LucideDownload, 13);
export const IconChat = /* @__PURE__ */ lucideIcon(LucideMessageSquare, 13);
