"use client";

import { CircleAlert, Inbox, SearchX } from "lucide-react";

export type ResultsStateTone = "empty" | "no-match" | "error";

const ICONS = { empty: Inbox, "no-match": SearchX, error: CircleAlert } as const;

/**
 * Leading 20px icon of a Results empty / no-match / error state (canvas results alignment, Page Layout Spec states).
 * Decorative (the state text carries the meaning).
 */
export function ResultsStateIcon({ tone }: { tone: ResultsStateTone }) {
  const Icon = ICONS[tone];
  return (
    <span className="results-state-icon" data-tone={tone} aria-hidden="true">
      <Icon size={20} strokeWidth={1.5} aria-hidden="true" />
    </span>
  );
}
