"use client";

import { useTranslations } from "next-intl";
import { CharIcon } from "@/components/char-icon";


export interface SteerMessageProps {
  who: string;
  time: string;
  text: string;
  /** Emoji used as the bot's "got it" ack — defaults to 🤝. */
  emoji?: string;
  /** Accessible label for the emoji ack. */
  emojiLabel?: string;
  /** Short text shown beside the emoji (e.g. "收到"). */
  ackText?: string;
  compact?: boolean;
}

export function SteerMessage({
  who,
  time,
  text,
  emoji = "🤝",
  emojiLabel,
  ackText,
  compact = false,
}: SteerMessageProps) {
  const tStep = useTranslations("step_list");
  const resolvedEmojiLabel = emojiLabel ?? tStep("steer_ack");
  return (
    <div
      style={{
        position: "relative",
        margin: "4px 0 8px",
        padding: compact ? "10px 14px" : "12px 16px",
        background: "var(--c-surface-2)",
        border: `1px dashed var(--c-line)`,
        borderRadius: 12,
        display: "flex",
        flexDirection: "column",
        alignItems: "flex-end",
      }}
    >
      <span
        aria-label={resolvedEmojiLabel}
        title={resolvedEmojiLabel}
        style={{
          position: "absolute",
          top: -10,
          right: 14,
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          background: "var(--c-surface)",
          border: `1px solid var(--c-line)`,
          borderRadius: 999,
          padding: "2px 9px 2px 8px",
          fontFamily: "var(--f-sans)",
          fontSize: "0.71875rem",
          color: "var(--c-ink-2)",
          boxShadow: "0 1px 0 rgba(20,15,10,0.04)",
        }}
      >
        <span style={{ fontSize: "0.8125rem", lineHeight: 1 }}><CharIcon ch={emoji} /></span>
        {ackText ? (
          <span
            style={{
              fontFamily: "var(--f-mono)",
              fontSize: "0.65625rem",
              color: "var(--c-ink-4)",
              letterSpacing: "0.02em",
            }}
          >
            {ackText}
          </span>
        ) : null}
      </span>
      <div
        style={{
          fontFamily: "var(--f-mono)",
          fontSize: "0.65625rem",
          color: "var(--c-ink-4)",
          letterSpacing: "0.04em",
          marginBottom: 4,
        }}
      >
        {who} · {time} · {tStep("steer_byline_suffix")}
      </div>
      <div
        style={{
          fontFamily: "var(--f-sans)",
          fontSize: compact ? "0.875rem" : "0.90625rem",
          color: "var(--c-ink)",
          fontWeight: 500,
          lineHeight: 1.5,
        }}
      >
        {text}
      </div>
    </div>
  );
}
