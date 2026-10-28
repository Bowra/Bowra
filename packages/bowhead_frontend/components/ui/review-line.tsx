"use client";

/**
 * ReviewLine — one quiet line of advice under a list item (Claude Design "Agent Eval Points", the review line
 * `.rv`, approved 2026-10-19): a sentence (12.5, ink-2) and at most two text buttons (24 high). While advice
 * is being prepared it is a single pale line (ink-4) with no buttons and no motion. No warning colour: the
 * advice never blocks anything, and nothing is shown when there is nothing to say.
 *
 * Styles: styles/ui-refresh-cards.css (`.ui-review-line*`).
 */
import clsx from "clsx";
import type { ReactNode } from "react";

export interface ReviewLineAction {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  testId?: string;
}

export function ReviewLine({
  children,
  busy = false,
  actions = [],
  className,
  "data-testid": testId,
}: {
  children: ReactNode;
  busy?: boolean;
  actions?: ReviewLineAction[];
  className?: string;
  "data-testid"?: string;
}) {
  return (
    <div className={clsx("ui-review-line", busy && "ui-review-line--busy", className)} data-testid={testId} aria-live="polite">
      <span>{children}</span>
      {!busy
        ? actions.slice(0, 2).map((action) => (
            <button
              key={action.label}
              type="button"
              className="settings-button settings-button--ghost settings-button--sm ui-card__btn--plain ui-review-line__btn"
              disabled={action.disabled}
              data-testid={action.testId}
              onClick={action.onSelect}
            >
              {action.label}
            </button>
          ))
        : null}
    </div>
  );
}
