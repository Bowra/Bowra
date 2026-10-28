"use client";

export interface BotBylineProps {
  /** Display time string (e.g. "14:32"). When omitted, no time stamp shows. */
  time?: string;
}

/**
 * BotByline — bot block header: the turn time once at the top of a BotTurn's flow (above the first
 * step row), not duplicated for every narrative/answer chunk.
 *
 * Phase 1.7-B (chat-redesign-2026-05-21 tech-design §4.4).
 *
 * 2026-09-15: removed the “✓ 已核验” pill (user decision). It only tracked `turnComplete` (this turn finished without error),
 * not any numerical verification, yet appeared on incorrect answers. Plan cards and other surfaces still show actual verification results using their own criteria.
 * See `docs/sprints/bi-answer-correctness-2026-09-15/d-remove-verified-byline.md`.
 */
export function BotByline({ time }: BotBylineProps) {
  // P2 batch 6 (brand appears once): the brand mark lives only in the sidebar header, so answers do not
  // repeat the logo and the product name; the byline keeps only the turn time.
  return (
    <div className="bh-bot-byline bh-bot-byline--time" data-testid="bot-byline">
      {time ? <span>{time}</span> : null}
    </div>
  );
}
