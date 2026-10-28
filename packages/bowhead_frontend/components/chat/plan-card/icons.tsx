/**
 * Plan-card icon set: a `name → Lucide glyph` map drawn on the icon size tiers
 * (P2 batch 2, see `components/icon-refresh.tsx`).
 */

import type { LucideIcon } from "lucide-react";
import {
  ArrowUpRight as LucideArrowUpRight,
  Ban as LucideBan,
  BookOpen as LucideBookOpen,
  Bot as LucideBot,
  Briefcase as LucideBriefcase,
  ChartColumn as LucideChartColumn,
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  Circle as LucideCircle,
  Clock as LucideClock,
  Coins as LucideCoins,
  Compass as LucideCompass,
  CornerDownRight as LucideCornerDownRight,
  FileText as LucideFileText,
  Globe as LucideGlobe,
  GripVertical as LucideGripVertical,
  Layers as LucideLayers,
  List as LucideList,
  Megaphone as LucideMegaphone,
  Paperclip as LucidePaperclip,
  Pencil as LucidePencil,
  RotateCw as LucideRotateCw,
  Scale as LucideScale,
  Send as LucideSend,
  Settings as LucideSettings,
  ShieldCheck as LucideShieldCheck,
  SkipForward as LucideSkipForward,
  Trash2 as LucideTrash2,
  TrendingUp as LucideTrendingUp,
  TriangleAlert as LucideTriangleAlert,
  Upload as LucideUpload,
  X as LucideX,
  Zap as LucideZap,
} from "lucide-react";
import { renderRefreshSvg } from "@/components/icon-refresh";

export type PlanIconName =
  | "shieldCheck"
  | "coins"
  | "barChart"
  | "bookOpen"
  | "fileText"
  | "check"
  | "x"
  | "alertTriangle"
  | "clock"
  | "slash"
  | "cornerDownRight"
  | "paperclip"
  | "arrowUpRight"
  | "zap"
  | "list"
  | "layers"
  | "upload"
  | "grip"
  | "trash"
  | "pencil"
  | "send"
  | "rotateCw"
  | "skipForward"
  | "compass"
  | "trendingUp"
  | "briefcase"
  | "megaphone"
  | "scale"
  // graph-mode batch 5 dependency graph
  | "globe"
  | "circleSm"
  | "chevronDown"
  // built-in Agent rows (configuration Agent, generic fallback)
  | "settings"
  | "bot";

const PLAN_ICONS: Readonly<Record<PlanIconName, LucideIcon>> = {
  shieldCheck: LucideShieldCheck,
  coins: LucideCoins,
  barChart: LucideChartColumn,
  bookOpen: LucideBookOpen,
  fileText: LucideFileText,
  check: LucideCheck,
  x: LucideX,
  alertTriangle: LucideTriangleAlert,
  clock: LucideClock,
  slash: LucideBan,
  cornerDownRight: LucideCornerDownRight,
  paperclip: LucidePaperclip,
  arrowUpRight: LucideArrowUpRight,
  zap: LucideZap,
  list: LucideList,
  layers: LucideLayers,
  upload: LucideUpload,
  grip: LucideGripVertical,
  trash: LucideTrash2,
  pencil: LucidePencil,
  send: LucideSend,
  rotateCw: LucideRotateCw,
  skipForward: LucideSkipForward,
  compass: LucideCompass,
  trendingUp: LucideTrendingUp,
  briefcase: LucideBriefcase,
  megaphone: LucideMegaphone,
  scale: LucideScale,
  globe: LucideGlobe,
  circleSm: LucideCircle,
  chevronDown: LucideChevronDown,
  settings: LucideSettings,
  bot: LucideBot,
};

export interface PlanIconProps {
  name: PlanIconName;
  size?: number;
}

/** Render a single line icon (stroke = currentColor). */
export function PlanIcon({ name, size = 18 }: PlanIconProps) {
  return renderRefreshSvg(PLAN_ICONS[name], size, size, { "aria-hidden": "true" });
}
