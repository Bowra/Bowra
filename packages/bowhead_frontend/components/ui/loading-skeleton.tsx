"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";


/** Show skeleton only after this delay (Page Layout Spec: after 300ms, avoiding flashes for fast requests) */
export const SKELETON_DELAY_MS = 300;

/**
 * Page-level loading skeleton (Page Layout Spec §4, Page-level Empty / Loading / Error States): replace only content, keeping the header.
 *
 * Renders a skeleton:
 * - `rows` (default): list / table pages, same height as real table rows (`--table-row-h`);
 * - `form`: settings pages, label-left/control-right rows in group cards (minimum 44).
 * - `sessions`: the sidebar session list (polish spec 3b), title bar + time bar per row at the two-line row height,
 *   breathing gently unless reduced motion is requested.
 * Reserve invisible space for the first 300ms (`is-pending`) to avoid flashes for fast responses; always `role="status"` with a Loading name.
 */
export function LoadingSkeleton({
  variant = "rows",
  rows = 3,
  label,
}: {
  variant?: "rows" | "form" | "sessions";
  rows?: number;
  label?: string;
}) {
  const t = useTranslations("common");
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setShown(true), SKELETON_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <div
      className={`ui-skeleton ui-skeleton--${variant}${shown ? "" : " is-pending"}`}
      role="status"
      aria-busy="true"
      aria-label={label ?? t("loading")}
    >
      {/* Status text for screen readers that announce live-region content rather than its name. */}
      <span className="sr-only">{label ?? t("loading")}</span>
      {Array.from({ length: rows }, (_, index) =>
        variant === "sessions" ? (
          <div key={index} className="ui-skeleton-row" aria-hidden="true">
            <span className="ui-skeleton-bar" />
            <span className="ui-skeleton-bar" />
          </div>
        ) : variant === "form" ? (
          <div key={index} className="ui-skeleton-row" aria-hidden="true">
            <span className="ui-skeleton-bar" />
            <span className="ui-skeleton-bar" />
          </div>
        ) : (
          <div key={index} className="ui-skeleton-row" aria-hidden="true">
            <span className="ui-skeleton-circle" />
            <span className="ui-skeleton-bar" />
            <span className="ui-skeleton-bar" />
          </div>
        ),
      )}
    </div>
  );
}
