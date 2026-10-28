"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "./status-badge";
import styles from "./canvas-object.module.css";

export interface StateBlockAction {
  label: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  busy?: boolean;
}

/**
 * C10 · whole-body state (loading, failure, no access, empty): 40 round icon, 14/650 title,
 * 12.5 ink3 body. At most one primary recovery and secondary actions. Errors announce assertively;
 * inline notices keep using the shared Notice.
 */
export function StateBlock({
  tone = "neutral",
  icon,
  title,
  body,
  primaryAction,
  secondaryActions,
  note,
}: {
  tone?: "neutral" | "error" | "progress" | "success";
  icon?: ReactNode;
  title: ReactNode;
  body?: ReactNode;
  primaryAction?: StateBlockAction;
  secondaryActions?: StateBlockAction[];
  note?: ReactNode;
}) {
  const actions = [...(secondaryActions ?? [])];
  return (
    <div className={styles.stateBlock} role={tone === "error" ? "alert" : "status"} aria-live={tone === "error" ? "assertive" : "polite"}>
      <span className={styles.stateIcon} data-tone={tone} aria-hidden>
        {tone === "progress" && !icon ? <Spinner size="large" /> : icon}
      </span>
      <span className={styles.stateTitle}>{title}</span>
      {body ? <span className={styles.stateBody}>{body}</span> : null}
      {actions.length || primaryAction ? (
        <span className={styles.stateActions}>
          {actions.map((action, index) => (
            // aria-disabled keeps a busy recovery action focusable (no focus loss during a status check).
            <Button key={index} aria-disabled={action.disabled || action.busy || undefined} loading={action.busy}
              onClick={() => { if (!action.disabled && !action.busy) action.onClick(); }}>{action.label}</Button>
          ))}
          {primaryAction ? (
            <Button variant="primary" onClick={primaryAction.onClick} disabled={primaryAction.disabled || primaryAction.busy} loading={primaryAction.busy}>
              {primaryAction.label}
            </Button>
          ) : null}
        </span>
      ) : null}
      {note ? <span className={styles.stateNote}>{note}</span> : null}
    </div>
  );
}
