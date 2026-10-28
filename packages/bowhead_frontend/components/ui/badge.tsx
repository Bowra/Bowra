/**
 * Badge and StatusDot (component unification P1, appearance batch two, docs/design/组件统一-技术设计.md, “外观段第二批”).
 *
 * Badge · Status Dot · Chip spec: badge height 20 (small 18), 11.5px/600, fully rounded;
 * six tones only — neutral / accent / positive / warning / danger / outline. Accent is reserved for
 * prominent identities such as Owner and MCP; statuses always use semantic colors. Status dots are 7px, always accompanied by text.
 *
 * Appearance comes from styles/ui-refresh.css (`.ui-badge` / `.ui-status-dot`), whose rules override the original classes
 * migrated badges still pass in `className`. New code needs no legacy classes.
 */
import type { HTMLAttributes } from "react";

export type BadgeTone = "neutral" | "accent" | "positive" | "warning" | "danger" | "outline";
export type BadgeSize = "sm" | "md";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  size?: BadgeSize;
}

function join(...parts: Array<string | undefined | false>): string {
  return parts.filter(Boolean).join(" ");
}

export function Badge({ tone = "neutral", size = "md", className, ...rest }: BadgeProps) {
  return <span {...rest} className={join("ui-badge", `ui-badge--${tone}`, `ui-badge--${size}`, className)} />;
}

export type StatusDotTone = "neutral" | "accent" | "positive" | "warning" | "danger";

export interface StatusDotProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: StatusDotTone;
}

/** Decorative 7px status dot (`aria-hidden`): adjacent text communicates status, never color alone. */
export function StatusDot({ tone = "neutral", className, ...rest }: StatusDotProps) {
  return <span aria-hidden {...rest} className={join("ui-status-dot", `ui-status-dot--${tone}`, className)} />;
}
