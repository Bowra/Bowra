"use client";

/**
 * Text and emoji characters used as icons (P2 character icon batch).
 *
 * `<CharIcon ch="✕" />` renders the Lucide glyph mapped to the character (a character without a mapping
 * stays plain text). The glyph is sized in
 * `em` so it follows the font size the character had, and `.bh-glyph` (styles/ui-refresh-workbench.css)
 * puts it on the text baseline. Colour follows text (`currentColor`).
 *
 * Stroke: the icon spec gives 1.5px from 13px up and scales inline markers below that by the 14 tier ratio
 * (12 -> 1.29). An `em` box has no pixel size at render time, so `refreshStrokeWidth` cannot apply that rule;
 * CharIcon writes it as CSS instead (`CHAR_ICON_STROKE`, resolved against the glyph's font size together
 * with the non-scaling stroke). A numeric `size` is a pixel box and snaps to the 14 / 16 / 20 tiers.
 *
 * Accessibility: a character that was read out keeps the same accessible name, because the Lucide glyph
 * becomes `role="img"` with the character as its label. Pass `aria-hidden` where the character was already
 * hidden (or sits inside a hidden parent) and the glyph stays hidden.
 *
 * Only for characters that act as icons. Typography (quotes, bullets inside prose, the "×" in "3×4",
 * truncation ellipses, currency) and user or model content stay plain text.
 */

import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  ArrowUpDown,
  ArrowUpRight,
  Ban,
  BookOpen,
  Bot,
  Brain,
  Calculator,
  ChartColumn,
  ChartLine,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Circle,
  CircleCheck,
  ClipboardList,
  Compass,
  Contrast,
  Download,
  Ellipsis,
  FileText,
  FlaskConical,
  FolderTree,
  Globe,
  Hash,
  Handshake,
  Image,
  Info,
  Library,
  Lightbulb,
  Lock,
  Mail,
  Map as MapIcon,
  MessageSquare,
  Network,
  Notebook,
  Palette,
  Paperclip,
  PenLine,
  Play,
  Pencil,
  Pin,
  Plug,
  Plus,
  Puzzle,
  RefreshCw,
  Rocket,
  RotateCw,
  Ruler,
  Save,
  Search,
  Settings,
  Shuffle,
  SlidersHorizontal,
  Sparkle,
  Sparkles,
  SquareTerminal,
  Telescope,
  Trash2,
  TriangleAlert,
  User,
  Wrench,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { createContext, useContext, type ReactNode } from "react";

import { renderRefreshSvg, type SvgIconProps } from "@/components/icon-refresh";

/**
 * Character -> Lucide glyph. Keys are stored without the emoji variation selector (U+FE0F). Call sites
 * whose character means something else than listed here pass `icon` explicitly.
 */
export const CHAR_ICON_MAP: Readonly<Record<string, LucideIcon>> = {
  // status
  "✓": Check,
  "✔": Check,
  "✅": CircleCheck,
  "✕": X,
  "✖": X,
  "×": X,
  "❌": X,
  "⚠": TriangleAlert,
  "ⓘ": Info,
  "⊘": Ban,
  "\u20E0": Ban,
  "○": Circle,
  "◐": Contrast,
  // disclosure and navigation
  "▾": ChevronDown,
  "▼": ChevronDown,
  "▴": ChevronUp,
  "▲": ChevronUp,
  "▸": ChevronRight,
  "›": ChevronRight,
  "‹": ChevronLeft,
  "←": ArrowLeft,
  "→": ArrowRight,
  "↑": ArrowUp,
  "↓": ArrowDown,
  "↕": ArrowUpDown,
  "↗": ArrowUpRight,
  "↻": RotateCw,
  "⟳": RotateCw,
  "⋯": Ellipsis,
  // objects and actions
  "⚡": Zap,
  "✦": Sparkle,
  "✨": Sparkles,
  "⚙": Settings,
  "🗑": Trash2,
  "📌": Pin,
  "🔒": Lock,
  "🖼": Image,
  "📎": Paperclip,
  "📄": FileText,
  "📑": FileText,
  "📝": FileText,
  "📃": FileText,
  "📊": ChartColumn,
  "📈": ChartLine,
  "🔍": Search,
  "🔎": Search,
  "🔄": RefreshCw,
  "🤖": Bot,
  "✏": Pencil,
  "✒": PenLine,
  "✍": PenLine,
  "📒": Notebook,
  "📚": Library,
  "📖": BookOpen,
  "🧠": Brain,
  "🔌": Plug,
  "💬": MessageSquare,
  "🐍": SquareTerminal,
  "🧮": Calculator,
  "📋": ClipboardList,
  "🔢": Hash,
  "🤝": Handshake,
  "🌐": Globe,
  "🎨": Palette,
  "💾": Save,
  "💡": Lightbulb,
  "🗂": FolderTree,
  "🔀": Shuffle,
  "🛠": Wrench,
  "🎛": SlidersHorizontal,
  "🧩": Puzzle,
  "🧪": FlaskConical,
  "＋": Plus,
  "👤": User,
  "🧭": Compass,
  "🚀": Rocket,
  "🗺": MapIcon,
  "🕸": Network,
  "🔭": Telescope,
  "📐": Ruler,
  "✉": Mail,
  "▶": Play,
  "⬇": Download,
};

/** Lucide glyph for a character used as an icon, or undefined when it has no mapping. */
export function charIconFor(ch: string): LucideIcon | undefined {
  return CHAR_ICON_MAP[ch.replace(/️/g, "").trim()];
}

export type CharIconProps = SvgIconProps & {
  /** The character the glyph stands for; it is also the accessible name. */
  ch: string;
  /** Glyph to use instead of the `CHAR_ICON_MAP` entry (for characters whose meaning differs here). */
  icon?: LucideIcon;
  /** Box size; defaults to 1em so the glyph matches the character's font size. */
  size?: number | string;
};

/**
 * Stroke for em-sized glyphs, in screen pixels: 1.5px from 13px up and 1.5 × size / 14 below, the same rule
 * as `refreshStrokeWidth`. The clamp() term is a step function (1.5px at 13px and up, 0 below), so the
 * max() selects 1.5px from 13px even though 1.5em / 14 is still below 1.5px at 13px.
 */
export const CHAR_ICON_STROKE =
  "max(min(1.5px, calc(1.5em / 14)), clamp(0px, calc((1em - 13px) * 1000 + 1.5px), 1.5px))";

export type CharIconTextProps = CharIconProps & {
  /** Where the glyph sits relative to the text. */
  at?: "start" | "end";
  /** The label: a catalog string, or rich-text nodes from `t.rich`. */
  children: ReactNode;
};

/**
 * A label whose icon glyph used to live inside the translated string ("✓ Verified", "Next →"): the
 * `<CharIcon>` glyph and the text separated by a space.
 */
export function CharIconText({ ch, at = "start", children, ...iconProps }: CharIconTextProps): ReactNode {
  const glyph = <CharIcon ch={ch} {...iconProps} />;
  return at === "start" ? <>{glyph} {children}</> : <>{children} {glyph}</>;
}

/**
 * Inside this provider `CharIcon` renders the bare character. Only the export-only visual route
 * (`/internal/canvas-export/visual`) uses it: exported PDF, Word, PowerPoint and image output keep their
 * pixels and the canvas visual renderer version does not change.
 */
const CharIconTextOnlyContext = createContext(false);

export function CharIconTextOnly({ children }: { children: ReactNode }) {
  return <CharIconTextOnlyContext.Provider value>{children}</CharIconTextOnlyContext.Provider>;
}

export function CharIcon({ ch, icon, size = "1em", className, ...props }: CharIconProps): ReactNode {
  const textOnly = useContext(CharIconTextOnlyContext);
  const Next = textOnly ? undefined : (icon ?? charIconFor(ch));
  if (!Next) return ch;
  const hidden = props["aria-hidden"] === true || props["aria-hidden"] === "true";
  const a11y: SvgIconProps = hidden ? {} : { role: "img", "aria-label": props["aria-label"] ?? ch.replace(/️/g, "") };
  return renderRefreshSvg(
    Next,
    size,
    size,
    { ...props, ...a11y, className: className ? `bh-glyph ${className}` : "bh-glyph" },
    typeof size === "number" ? undefined : CHAR_ICON_STROKE,
  );
}
