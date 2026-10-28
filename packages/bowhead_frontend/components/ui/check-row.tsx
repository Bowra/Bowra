"use client";

/**
 * CheckRow — one answer to check inside a chat card (Claude Design "Agent Eval Alignment Loop", the check row
 * `.chk`, approved 2026-10-19). A grouped list row with an inset hairline above it:
 *
 *   question (12, ink-3, with an optional secondary note) → excerpt (13, ink; the quoted part highlighted)
 *   → one verdict sentence (13, ink-2) and the row's control (a segmented control, right-aligned on wide
 *   cards and full width on narrow ones).
 *
 * The row only lays out; the caller owns the control and its state. `control` receives the verdict
 * sentence's id so the control can use it as its description.
 *
 * Styles: styles/ui-refresh-cards.css (`.ui-check-row*`).
 */
import clsx from "clsx";
import { useId, useState, type ReactNode } from "react";

import { plainExcerpt } from "@/lib/plain-excerpt";

/** Split `text` around the first occurrence of `quote`, so the quoted words can be highlighted. */
export function highlightQuote(text: string, quote: string | null | undefined): ReactNode {
  const needle = quote?.trim();
  const at = needle ? text.indexOf(needle) : -1;
  if (!needle || at < 0) return text;
  return (
    <>
      {text.slice(0, at)}
      <mark>{needle}</mark>
      {text.slice(at + needle.length)}
    </>
  );
}

const LONG_EXCERPT = 240;

export function CheckRow({
  question,
  note,
  excerpt,
  quote,
  verdict,
  control,
  first = false,
  moreLabel,
  className,
  "data-testid": testId,
}: {
  question?: ReactNode;
  /** Secondary words after the question, e.g. "Edited by Coach". */
  note?: ReactNode;
  excerpt?: string | null;
  /** The judge's quote, highlighted inside the excerpt. */
  quote?: string | null;
  verdict?: ReactNode;
  control?: (ids: { verdictId: string }) => ReactNode;
  /** No hairline above the first row of a card that has no title. */
  first?: boolean;
  /** Label of the button that unfolds a long excerpt; long excerpts fold to three lines without it. */
  moreLabel?: string;
  className?: string;
  "data-testid"?: string;
}) {
  const verdictId = `${useId().replaceAll(":", "")}-verdict`;
  const [open, setOpen] = useState(false);
  // Answers are markdown: an excerpt shows their words, not raw `**`, `#` or list markers.
  const shown = excerpt ? plainExcerpt(excerpt) : excerpt;
  const long = Boolean(shown && shown.length > LONG_EXCERPT && moreLabel);
  return (
    <div className={clsx("ui-check-row", first && "ui-check-row--first", className)} data-testid={testId}>
      {question || note ? (
        <div className="ui-check-row__question">
          {question}
          {question && note ? " · " : null}
          {note}
        </div>
      ) : null}
      {shown ? (
        <div className="ui-check-row__excerpt" data-folded={long && !open ? "true" : undefined}>
          {highlightQuote(shown, quote ? plainExcerpt(quote) : quote)}
        </div>
      ) : null}
      {long && !open ? (
        <button type="button" className="ui-check-row__more" onClick={() => setOpen(true)}>
          {moreLabel}
        </button>
      ) : null}
      {verdict || control ? (
        <div className="ui-check-row__verdict">
          {verdict ? <span id={verdictId}>{verdict}</span> : null}
          {control ? control({ verdictId }) : null}
        </div>
      ) : null}
    </div>
  );
}
