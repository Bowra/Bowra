"use client";

import { useId, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import styles from "./canvas-object.module.css";

export interface CanvasFooterPrimary {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  /** Read by screen readers through aria-describedby; also shown on the left when no message is given. */
  disabledReason?: ReactNode;
  busy?: boolean;
}

export interface CanvasFooterSecondary {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}

export type CanvasFooterTone = "neutral" | "warning" | "danger" | "strong";

/**
 * C9 · object footer (52, bar material). Left: reason or state (optionally a leading checkbox);
 * right: secondary buttons and the single primary button. A disabled primary uses a neutral fill
 * and its first missing condition is written out (and attached through aria-describedby).
 */
export function CanvasObjectFooter({
  message,
  tone = "neutral",
  icon,
  leading,
  secondary,
  primary,
  live = true,
}: {
  message?: ReactNode;
  tone?: CanvasFooterTone;
  icon?: ReactNode;
  leading?: ReactNode;
  secondary?: CanvasFooterSecondary[];
  primary?: CanvasFooterPrimary;
  live?: boolean;
}) {
  const reasonId = useId();
  const reason = primary?.disabled ? primary.disabledReason : undefined;
  const messageNode = message ?? reason;
  const messageTone = message ? tone : reason ? "warning" : tone;
  const messageElement = messageNode ? (
    <span id={reasonId} className={styles.footerMessage} data-tone={messageTone} role={live ? "status" : undefined}>{icon}{messageNode}</span>
  ) : null;
  // With a leading control (the document acknowledgement) the reason sits beside the primary button.
  return (
    <div className={styles.footer}>
      {leading ? <span className={styles.footerLeading}>{leading}</span> : messageElement ?? <span className={styles.footerMessage} />}
      {secondary?.length || primary ? (
        <span className={styles.footerActions}>
          {leading ? messageElement : null}
          {secondary?.map((action, index) => (
            <Button key={index} onClick={action.onClick} disabled={action.disabled}>{action.label}</Button>
          ))}
          {primary ? (
            // A blocked primary stays focusable (aria-disabled) so its reason remains readable; activation is ignored.
            <Button
              variant="primary"
              onClick={() => { if (!primary.disabled && !primary.busy) primary.onClick(); }}
              disabled={primary.busy}
              aria-disabled={primary.disabled && !primary.busy ? true : undefined}
              loading={primary.busy}
              aria-describedby={reason ? reasonId : undefined}
            >
              {primary.label}
            </Button>
          ) : null}
        </span>
      ) : leading ? messageElement : null}
    </div>
  );
}
