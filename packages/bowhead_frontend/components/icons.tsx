/**
 * SVG icon set — strictly line-based, no emoji, ported 1:1 from the Claude Design handoff (icons.jsx).
 *
 * P2 batch 2 (icons): every export is a Lucide glyph (ISC, `lucide-react` pinned to 1.49.0) built with
 * `lucideIcon`, keeping the call site's box, aria attributes and className, stroke 1.5 (see
 * `components/icon-refresh.tsx`). The mapping table lives in the component-unification tech design,
 * P2 batch 2 results section (docs/design). `Mark` is the hand-drawn brand mark.
 */

import type { SVGProps } from "react";
import {
  Activity as LucideActivity,
  Archive as LucideArchive,
  ArrowDown as LucideArrowDown,
  ArrowLeft as LucideArrowLeft,
  ArrowRight as LucideArrowRight,
  ArrowUp as LucideArrowUp,
  ArrowUpRight as LucideArrowUpRight,
  AtSign as LucideAtSign,
  Ban as LucideBan,
  Bell as LucideBell,
  Book as LucideBook,
  BookOpen as LucideBookOpen,
  Bot as LucideBot,
  Brain as LucideBrain,
  Briefcase as LucideBriefcase,
  Building2 as LucideBuilding2,
  Calendar as LucideCalendar,
  CalendarClock as LucideCalendarClock,
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  CircleAlert as LucideCircleAlert,
  CircleCheck as LucideCircleCheck,
  CircleDot as LucideCircleDot,
  CircleX as LucideCircleX,
  Clock as LucideClock,
  Columns2 as LucideColumns2,
  Copy as LucideCopy,
  Cpu as LucideCpu,
  Database as LucideDatabase,
  Ellipsis as LucideEllipsis,
  Eye as LucideEye,
  ExternalLink as LucideExternalLink,
  FileCode as LucideFileCode,
  FileDown as LucideFileDown,
  FileText as LucideFileText,
  Folder as LucideFolder,
  Hand as LucideHand,
  Hash as LucideHash,
  History as LucideHistory,
  Inbox as LucideInbox,
  Info as LucideInfo,
  Layers as LucideLayers,
  Link as LucideLink,
  ListChecks as LucideListChecks,
  LoaderCircle as LucideLoaderCircle,
  Lock as LucideLock,
  LogOut as LucideLogOut,
  Mail as LucideMail,
  Maximize2 as LucideMaximize2,
  Menu as LucideMenu,
  MessageCircleQuestion as LucideMessageCircleQuestion,
  MessageSquare as LucideMessageSquare,
  Minimize2 as LucideMinimize2,
  Moon as LucideMoon,
  Package as LucidePackage,
  Palette as LucidePalette,
  PanelLeft as LucidePanelLeft,
  Paperclip as LucidePaperclip,
  Pause as LucidePause,
  Pencil as LucidePencil,
  Pin as LucidePin,
  Play as LucidePlay,
  Plug as LucidePlug,
  Plus as LucidePlus,
  RefreshCw as LucideRefreshCw,
  Repeat as LucideRepeat,
  RotateCcw as LucideRotateCcw,
  Search as LucideSearch,
  Server as LucideServer,
  Settings as LucideSettings,
  Share as LucideShare,
  Shield as LucideShield,
  Sparkle as LucideSparkle,
  Sparkles as LucideSparkles,
  Sun as LucideSun,
  Terminal as LucideTerminal,
  Trash2 as LucideTrash2,
  TriangleAlert as LucideTriangleAlert,
  Undo2 as LucideUndo2,
  Upload as LucideUpload,
  User as LucideUser,
  Users as LucideUsers,
  Wand as LucideWand,
  WandSparkles as LucideWandSparkles,
  Workflow as LucideWorkflow,
  X as LucideX,
  Zap as LucideZap,
} from "lucide-react";

import { lucideIcon } from "@/components/icon-refresh";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

export const Mark = ({ size = 22, ...p }: IconProps) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" {...p}>
    <path d="M2 17 Q 8 9 12 12 Q 16 15 22 7" stroke="currentColor" strokeWidth="1" strokeLinecap="round" opacity="0.45" />
    <path d="M2 19 Q 8 12 12 14 Q 16 16 22 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.7" />
    <path d="M2 21 Q 8 15 12 16 Q 16 17 22 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
  </svg>
);

export const Plus = /* @__PURE__ */ lucideIcon(LucidePlus, 16);
export const Chat = /* @__PURE__ */ lucideIcon(LucideMessageSquare, 16);
export const Brain = /* @__PURE__ */ lucideIcon(LucideBrain, 16, { sized: false });
export const Spark = /* @__PURE__ */ lucideIcon(LucideSparkle, 16);
export const Shield = /* @__PURE__ */ lucideIcon(LucideShield, 16);
export const Search = /* @__PURE__ */ lucideIcon(LucideSearch, 14);
export const Caret = /* @__PURE__ */ lucideIcon(LucideChevronDown, 10, { sized: false });
export const Hash = /* @__PURE__ */ lucideIcon(LucideHash, 12, { sized: false });
export const Side = /* @__PURE__ */ lucideIcon(LucidePanelLeft, 14);
export const Trace = /* @__PURE__ */ lucideIcon(LucideActivity, 14, { sized: false });
export const X = /* @__PURE__ */ lucideIcon(LucideX, 14);
export const Send = /* @__PURE__ */ lucideIcon(LucideArrowUp, 13, { sized: false });
export const Spinner = /* @__PURE__ */ lucideIcon(LucideLoaderCircle, 12);
export const Check = /* @__PURE__ */ lucideIcon(LucideCheck, 12);
export const Copy = /* @__PURE__ */ lucideIcon(LucideCopy, 14);
export const XSm = /* @__PURE__ */ lucideIcon(LucideX, 12, { sized: false });
export const Pause = /* @__PURE__ */ lucideIcon(LucidePause, 12, { sized: false });
export const Arrow = /* @__PURE__ */ lucideIcon(LucideArrowUpRight, 12, { sized: false });
export const Attach = /* @__PURE__ */ lucideIcon(LucidePaperclip, 13, { sized: false });
export const At = /* @__PURE__ */ lucideIcon(LucideAtSign, 13, { sized: false });
export const Slash = /* @__PURE__ */ lucideIcon(LucideBan, 12, { sized: false });
export const Lock = /* @__PURE__ */ lucideIcon(LucideLock, 11);
export const Edit = /* @__PURE__ */ lucideIcon(LucidePencil, 12, { sized: false });
export const Trash = /* @__PURE__ */ lucideIcon(LucideTrash2, 12);
export const Cog = /* @__PURE__ */ lucideIcon(LucideSettings, 14, { sized: false });
export const Folder = /* @__PURE__ */ lucideIcon(LucideFolder, 16);
export const Doc = /* @__PURE__ */ lucideIcon(LucideFileText, 16);
export const Book = /* @__PURE__ */ lucideIcon(LucideBook, 16, { sized: false });
export const Bolt = /* @__PURE__ */ lucideIcon(LucideZap, 16, { sized: false });

/* ── canvas-header glyphs (lucide-derived, 1.8 stroke · ported from v10 icons.jsx) ── */
export const FileDown = /* @__PURE__ */ lucideIcon(LucideFileDown, 14);
export const Maximize = /* @__PURE__ */ lucideIcon(LucideMaximize2, 16);
export const Minimize = /* @__PURE__ */ lucideIcon(LucideMinimize2, 16);
export const MoreH = /* @__PURE__ */ lucideIcon(LucideEllipsis, 16);
export const Pin = /* @__PURE__ */ lucideIcon(LucidePin, 14);
export const LogOut = /* @__PURE__ */ lucideIcon(LucideLogOut, 17);
export const History = /* @__PURE__ */ lucideIcon(LucideHistory, 15);
export const Link = /* @__PURE__ */ lucideIcon(LucideLink, 13);
export const Sun = /* @__PURE__ */ lucideIcon(LucideSun, 17);
export const Moon = /* @__PURE__ */ lucideIcon(LucideMoon, 17);
export const Bell = /* @__PURE__ */ lucideIcon(LucideBell, 17);
export const ArchiveBox = /* @__PURE__ */ lucideIcon(LucideArchive, 15);
export const Inbox = /* @__PURE__ */ lucideIcon(LucideInbox, 18);
export const Menu = /* @__PURE__ */ lucideIcon(LucideMenu, 20);

/* ── settings nav glyphs ── */
export const UserIcon = /* @__PURE__ */ lucideIcon(LucideUser, 16);
export const Users = /* @__PURE__ */ lucideIcon(LucideUsers, 16);
export const Building = /* @__PURE__ */ lucideIcon(LucideBuilding2, 16);
export const Cpu = /* @__PURE__ */ lucideIcon(LucideCpu, 16);
export const Database = /* @__PURE__ */ lucideIcon(LucideDatabase, 16);
export const Plug = /* @__PURE__ */ lucideIcon(LucidePlug, 16);
export const Server = /* @__PURE__ */ lucideIcon(LucideServer, 16);
export const Bot = /* @__PURE__ */ lucideIcon(LucideBot, 16);
export const Workflow = /* @__PURE__ */ lucideIcon(LucideWorkflow, 16);
export const Sparkles = /* @__PURE__ */ lucideIcon(LucideSparkles, 16);
export const BookOpen = /* @__PURE__ */ lucideIcon(LucideBookOpen, 16);
export const Palette = /* @__PURE__ */ lucideIcon(LucidePalette, 16);

/* ── routines / scheduled-task glyphs (lucide-derived, 24×24) ── */
export const CalendarClock = /* @__PURE__ */ lucideIcon(LucideCalendarClock, 16);
export const Clock = /* @__PURE__ */ lucideIcon(LucideClock, 16);
export const Calendar = /* @__PURE__ */ lucideIcon(LucideCalendar, 16);
export const Briefcase = /* @__PURE__ */ lucideIcon(LucideBriefcase, 16);
export const Repeat = /* @__PURE__ */ lucideIcon(LucideRepeat, 16);
export const Hand = /* @__PURE__ */ lucideIcon(LucideHand, 16);
export const Play = /* @__PURE__ */ lucideIcon(LucidePlay, 16);
export const Mail = /* @__PURE__ */ lucideIcon(LucideMail, 16);
export const RefreshCw = /* @__PURE__ */ lucideIcon(LucideRefreshCw, 16);
export const AlertTriangle = /* @__PURE__ */ lucideIcon(LucideTriangleAlert, 16);
export const ChevronDown = /* @__PURE__ */ lucideIcon(LucideChevronDown, 16);
export const ChevronRight = /* @__PURE__ */ lucideIcon(LucideChevronRight, 16);
export const Info = /* @__PURE__ */ lucideIcon(LucideInfo, 16);
export const ExternalLink = /* @__PURE__ */ lucideIcon(LucideExternalLink, 16);
export const Layers = /* @__PURE__ */ lucideIcon(LucideLayers, 16);
export const Terminal = /* @__PURE__ */ lucideIcon(LucideTerminal, 16);
export const Upload = /* @__PURE__ */ lucideIcon(LucideUpload, 16);
export const Package = /* @__PURE__ */ lucideIcon(LucidePackage, 16);
export const Share2 = /* @__PURE__ */ lucideIcon(LucideShare, 16);
export const AlertCircle = /* @__PURE__ */ lucideIcon(LucideCircleAlert, 16);
export const ArrowLeft = /* @__PURE__ */ lucideIcon(LucideArrowLeft, 16);
export const ArrowUp = /* @__PURE__ */ lucideIcon(LucideArrowUp, 16);
export const ArrowDown = /* @__PURE__ */ lucideIcon(LucideArrowDown, 16);
export const FileText = /* @__PURE__ */ lucideIcon(LucideFileText, 16);
export const FileCode = /* @__PURE__ */ lucideIcon(LucideFileCode, 16);
/* config-copilot（Claude Design cc-icons.js 1:1）*/
export const Wand = /* @__PURE__ */ lucideIcon(LucideWand, 16);
export const Undo = /* @__PURE__ */ lucideIcon(LucideUndo2, 16);
export const ArrowRight = /* @__PURE__ */ lucideIcon(LucideArrowRight, 16);

/* ── canvas objects (Canvas Objects Spec C1–C14) ── */
export const ChevronLeft = /* @__PURE__ */ lucideIcon(LucideChevronLeft, 16);
export const CircleCheck = /* @__PURE__ */ lucideIcon(LucideCircleCheck, 16);
export const CircleDot = /* @__PURE__ */ lucideIcon(LucideCircleDot, 16);
export const CircleX = /* @__PURE__ */ lucideIcon(LucideCircleX, 16);
export const Columns2 = /* @__PURE__ */ lucideIcon(LucideColumns2, 16);
export const Eye = /* @__PURE__ */ lucideIcon(LucideEye, 16);
export const ListChecks = /* @__PURE__ */ lucideIcon(LucideListChecks, 16);
export const MessageCircleQuestion = /* @__PURE__ */ lucideIcon(LucideMessageCircleQuestion, 16);
export const RotateCcw = /* @__PURE__ */ lucideIcon(LucideRotateCcw, 16);
export const WandSparkles = /* @__PURE__ */ lucideIcon(LucideWandSparkles, 16);
