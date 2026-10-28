"use client";

import { getToolMeta } from "./tool-catalog";
import type { StepState } from "./types";
import {
  ChevronRight as LucideChevronRight,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

export interface ChevronProps {
  open: boolean;
  size?: number;
}

export function Chevron({ open, size = 10 }: ChevronProps) {
  return (
    <LucideGlyph icon={LucideChevronRight} size={size} style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)", transition: "transform .18s", color: "var(--c-ink-4)" }} />
  );
}

export interface CursorProps {
  color?: string;
}

export function Cursor({ color }: CursorProps) {
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block",
        width: "0.5ch",
        height: "1em",
        background: color ?? "var(--c-ink)",
        marginLeft: 1,
        verticalAlign: "-2px",
        animation: "bh-blink 1s steps(2) infinite",
      }}
    />
  );
}

export interface ActionBtnProps {
  primary?: boolean;
  quiet?: boolean;
  onClick?: () => void;
  children: React.ReactNode;
  type?: "button" | "submit";
}

export function ActionBtn({
  primary,
  quiet,
  onClick,
  children,
  type = "button",
}: ActionBtnProps) {
  return (
    <button
      type={type}
      onClick={onClick}
      style={{
        padding: "5px 12px",
        borderRadius: 999,
        border: `1px solid ${primary ? "var(--c-ink)" : quiet ? "transparent" : "var(--c-line)"}`,
        background: primary ? "var(--c-ink)" : "var(--c-surface)",
        color: primary ? "var(--c-bg)" : quiet ? "var(--c-ink-4)" : "var(--c-ink-2)",
        fontFamily: "var(--f-sans)",
        fontSize: "0.75rem",
        fontWeight: 500,
        cursor: "pointer",
      }}
    >
      {children}
    </button>
  );
}

export interface StatusGlyphProps {
  state: StepState;
  /** When state=active, use the tool's icon and pulse it. */
  toolName?: string | null;
}

/**
 * Single-glyph status indicator for a step row. Visual rules per
 * Claude Design v8 step-blocks.jsx StatusGlyph:
 *   done    → ✓ in ink2
 *   failed  → ❌ in err
 *   partial → ⚠ in warn
 *   pending → ○ in mute2
 *   active  → tool's emoji icon, pulse animation in accent
 */
export function StatusGlyph({ state, toolName }: StatusGlyphProps) {
  if (state === "done") return <Glyph ch="✓" color={"var(--c-ink-2)"} />;
  if (state === "failed") return <Glyph ch="❌" color={"var(--c-err)"} emoji />;
  if (state === "partial") return <Glyph ch="⚠" color={"var(--c-warn)"} />;
  if (state === "pending") return <Glyph ch="○" color={"var(--c-ink-4)"} />;
  const meta = getToolMeta(toolName);
  return <Glyph ch={meta?.icon ?? "○"} color={"var(--c-accent)"} emoji pulse />;
}

interface GlyphProps {
  ch: string;
  color: string;
  emoji?: boolean;
  pulse?: boolean;
}

function Glyph({ ch, color, emoji, pulse }: GlyphProps) {
  return (
    <span
      aria-hidden
      style={{
        width: 18,
        flex: "0 0 18px",
        color,
        fontFamily: emoji ? "var(--f-sans)" : "var(--f-mono)",
        fontSize: "0.75rem",
        lineHeight: "1.25rem",
        textAlign: "center",
        animation: pulse ? "bh-pulse 1.4s ease-in-out infinite" : "none",
      }}
    >
      {ch}
    </span>
  );
}

export interface CodeBlockProps {
  code: string;
  lang?: string;
  streaming?: boolean;
  /** Smaller variant for stdout-style output. */
  small?: boolean;
  /** Hide the language tag chip. */
  noLang?: boolean;
}

export function CodeBlock({ code, lang, streaming, small, noLang }: CodeBlockProps) {
  return (
    <div style={{ position: "relative" }}>
      {!noLang && lang ? (
        <div
          style={{
            position: "absolute",
            top: 0,
            right: 0,
            fontFamily: "var(--f-mono)",
            fontSize: "0.625rem",
            color: "var(--c-ink-4)",
            padding: "6px 10px",
            letterSpacing: "0.04em",
          }}
        >
          {lang}
        </div>
      ) : null}
      <pre
        style={{
          margin: 0,
          padding: "10px 12px",
          background: "var(--c-surface-2)",
          border: `1px solid var(--c-line)`,
          borderRadius: 6,
          fontFamily: "var(--f-mono)",
          fontSize: small ? "0.71875rem" : "0.75rem",
          lineHeight: 1.55,
          color: "var(--c-ink-2)",
          whiteSpace: "pre-wrap",
          overflowX: "auto",
        }}
      >
        {code}
        {streaming ? <Cursor /> : null}
      </pre>
    </div>
  );
}
