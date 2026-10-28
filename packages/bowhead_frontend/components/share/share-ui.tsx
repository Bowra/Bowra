// components/share/share-ui.tsx — shared primitives for the content-sharing UI.
// Verbatim translation of the Claude Design handoff (sharing/ui-bits.jsx):
// glyphs, dimensions and font sizes are kept exactly; the design's hard-coded
// hex `shTok` is mapped to the app's identical `--c-*` light/dark tokens.

"use client";

import type { CSSProperties } from "react";
import { useTranslations } from "next-intl";
import type { Audience } from "@/lib/content-share-api";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import type { LucideIcon } from "lucide-react";
import {
  Bot as LucideBot,
  Check as LucideCheck,
  ChevronLeft as LucideChevronLeft,
  ChevronRight as LucideChevronRight,
  File as LucideFile,
  Globe as LucideGlobe,
  Lock as LucideLock,
  Plus as LucidePlus,
  Search as LucideSearch,
  Share as LucideShare,
  Sparkle as LucideSparkle,
  Trash2 as LucideTrash2,
  User as LucideUser,
  Users as LucideUsers,
  X as LucideX,
} from "lucide-react";
import { renderRefreshSvg } from "@/components/icon-refresh";

export const T = {
  bg: "var(--c-bg)",
  surface: "var(--c-surface)",
  s2: "var(--c-surface-2)",
  s3: "var(--c-surface-3)",
  line: "var(--c-line)",
  line2: "var(--c-line-2)",
  ink: "var(--c-ink)",
  ink2: "var(--c-ink-2)",
  ink3: "var(--c-ink-3)",
  ink4: "var(--c-ink-4)",
  accent: "var(--c-accent)",
  accentHover: "var(--c-accent-hover)",
  accentSoft: "var(--c-accent-soft)",
  accentTint: "var(--c-accent-tint)",
  err: "var(--c-err)",
  errSoft: "var(--c-err-soft)",
  pos: "var(--c-pos)",
  mono: "var(--font-v2-mono)",
} as const;

/* ───────── icons ───────── */
type IconName =
  | "lock" | "users" | "user" | "globe" | "search" | "x" | "tick" | "plus"
  | "doc" | "bot" | "spark" | "trash" | "share" | "back" | "chevR";


/** P2 batch 2: Lucide icon per name (see `components/icon-refresh.tsx`). */
const SH_ICONS: Readonly<Record<IconName, LucideIcon>> = {
  lock: LucideLock,
  users: LucideUsers,
  user: LucideUser,
  globe: LucideGlobe,
  search: LucideSearch,
  x: LucideX,
  tick: LucideCheck,
  plus: LucidePlus,
  doc: LucideFile,
  bot: LucideBot,
  spark: LucideSparkle,
  trash: LucideTrash2,
  share: LucideShare,
  back: LucideChevronLeft,
  chevR: LucideChevronRight,
};

export function ShIcon({
  name, size = 14, style, className,
}: { name: IconName; size?: number; style?: CSSProperties; className?: string }) {
  return renderRefreshSvg(SH_ICONS[name], size, size, { style, className, "aria-hidden": "true" });
}

/* ───────── avatars ───────── */
/**
 * Member avatar. Legacy appearance chooses color by name, with white initial; styles/ui-controls-legacy.css
 * .legacy-share-avatar provides background/text
 * (color passed via --sh-avatar-bg); the refreshed styles use a neutral background, accent for self (decision 22), see
 * styles/ui-refresh-surfaces.css.
 */
export function ShAvatar({ name, color = "#5e7d8a", size = 28, fontSize, self = false }: { name: string; color?: string; size?: number; fontSize?: number; self?: boolean }) {
  return (
    <span className={self ? "legacy-share-avatar is-self" : "legacy-share-avatar"} style={{
      "--sh-avatar-bg": color,
      width: size, height: size, flex: `0 0 ${size}px`, borderRadius: "50%",
      display: "inline-grid", placeItems: "center",
      fontSize: fontSize || Math.round(size * 0.43), fontWeight: 600, userSelect: "none",
    } as CSSProperties}>{name.slice(0, 1)}</span>
  );
}
export function ShGroupAvatar({ size = 28 }: { size?: number }) {
  return (
    <span style={{
      width: size, height: size, flex: `0 0 ${size}px`, borderRadius: 9,
      background: T.s2, color: T.ink3, display: "inline-grid", placeItems: "center",
      border: `1px solid ${T.line}`,
    }}><ShIcon name="users" size={Math.round(size * 0.52)} /></span>
  );
}

/* ───────── tags & badges ───────── */
export function ShTypeTag({ kind }: { kind: string }) {
  return (
    <span className="legacy-share-type-tag">{kind}</span>
  );
}

/** Fixed permission label (read-only, not dropdown): Can view / Can use. */
export function ShPermTag({ label, small }: { label?: string; small?: boolean }) {
  const t = useTranslations("share");
  const text = label ?? t("perm_view");
  return (
    <span title={t("perm_tag_title")} className={`legacy-share-perm-tag${small ? " is-small" : ""}`}>
      <ShIcon name="lock" size={small ? 9 : 10} className="legacy-share-perm-tag-icon" />{text}
    </span>
  );
}

/** Audience badge: Only me / N specific people / Company-wide, three terracotta shades. */
export function ShAudienceBadge({ level, n, size }: { level: Audience; n?: number; size?: number }) {
  const t = useTranslations("share");
  const baseStyle: CSSProperties = {
    display: "inline-flex", alignItems: "center", gap: 4.5, flex: "0 0 auto",
    fontSize: `${(size || 11) / 16}rem`, fontWeight: 600, borderRadius: 999, padding: "3px 9px",
    letterSpacing: 0.1, whiteSpace: "nowrap",
  };
  if (level === "private") return (
    <span style={{ ...baseStyle, background: T.s2, color: T.ink3, fontWeight: 500 }}>
      <ShIcon name="lock" size={10.5} />{t("audience_private")}
    </span>
  );
  if (level === "custom") return (
    <span style={{ ...baseStyle, background: T.accentSoft, color: T.accentHover }}>
      <ShIcon name="users" size={11} />{t("audience_custom_n", { n: n ?? 0 })}
    </span>
  );
  return (
    <span style={{ ...baseStyle, background: T.accent, color: "#fff" }}>
      <ShIcon name="globe" size={11} />{t("audience_team_wide")}
    </span>
  );
}

/* ───────── Three-state audience segmented control ───────── */
const SEGMENT_DEFS: { id: Audience; icon: IconName; labelKey: "audience_private" | "audience_custom" | "audience_team_wide" }[] = [
  { id: "private", icon: "lock", labelKey: "audience_private" },
  { id: "custom", icon: "users", labelKey: "audience_custom" },
  { id: "team_wide", icon: "globe", labelKey: "audience_team_wide" },
];
export function ShSegmented({
  active, onPick, height = 38, fontSize = 13, disabled, busy, lockedIds, lockedTitle,
}: {
  active: Audience; onPick?: (a: Audience) => void; height?: number; fontSize?: number; disabled?: boolean;
  /** Saving: ignore selection but retain focusability (see SegmentedControl.busy). */
  busy?: boolean;
  /** skill-team-governance: locked audience levels (e.g. team_skill team_wide requires approval); render disabled. */
  lockedIds?: Audience[]; lockedTitle?: string;
}) {
  const t = useTranslations("share");
  const SEGMENTS = SEGMENT_DEFS.map((s) => ({ ...s, label: t(s.labelKey) }));
  return (
    // Selection submits (changes server sharing scope) ⇒ arrows only move focus; Space / Enter / click select.
    <SegmentedControl appearance="none" activation="manual" value={active} onValueChange={(a) => onPick?.(a)} busy={busy} aria-label={t("aria_audience")} className="legacy-share-seg">
      {SEGMENTS.map((s) => {
        const on = s.id === active;
        const locked = !on && !!lockedIds?.includes(s.id);
        return (
          <Segment key={s.id} value={s.id} disabled={disabled || locked}
            title={locked ? lockedTitle : undefined}
            className={["legacy-share-seg-item", on ? "is-on" : "", locked ? "is-locked" : "", disabled || busy || locked ? "is-static" : ""].filter(Boolean).join(" ")}
            style={{ "--sh-seg-h": `${height}px`, "--sh-seg-fs": `${fontSize / 16}rem` } as CSSProperties}>
            <ShIcon name={s.icon} size={13} />{s.label}
          </Segment>
        );
      })}
    </SegmentedControl>
  );
}

/* ───────── button ───────── */
type BtnKind = "primary" | "ghost" | "quiet" | "danger";
export function ShBtn({
  kind = "primary", children, full, h = 36, fs = 13, style, onClick, disabled, ariaLabel,
}: {
  kind?: BtnKind; children: React.ReactNode; full?: boolean; h?: number; fs?: number;
  style?: CSSProperties; onClick?: () => void; disabled?: boolean; ariaLabel?: string;
}) {
  // Four buttons' legacy appearance uses styles/ui-controls-legacy.css legacy-share-btn--* (UI refresh batch 5 maps to variants);
  // Height/font size vary by caller, supplied as CSS variables.
  return (
    <button onClick={onClick} disabled={disabled} aria-label={ariaLabel}
      className={`legacy-share-btn legacy-share-btn--${kind}`}
      style={{
        "--sh-btn-h": `${h}px`, "--sh-btn-fs": `${fs / 16}rem`,
        width: full ? "100%" : undefined, ...style,
      } as CSSProperties}>{children}</button>
  );
}

/** Quiet placeholder explanation during audience switching (Only me / Company-wide). */
export function ShQuietState({
  icon, title, sub, tint,
}: { icon: IconName; title: string; sub: string; tint?: boolean }) {
  return (
    <div className="legacy-share-quiet">
      <span className={`legacy-share-quiet-mark${tint ? " is-tint" : ""}`}><ShIcon name={icon} size={22} /></span>
      <div className="legacy-share-quiet-title">{title}</div>
      <div className="legacy-share-quiet-sub">{sub}</div>
    </div>
  );
}

/** Name → stable avatar background (design palette). */
export function avatarColor(seed: string): string {
  const palette = ["#5e7d8a", "#7a5a3f", "#a98028", "#5a3f6b", "#3a724a", "#b04a2a"];
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}
