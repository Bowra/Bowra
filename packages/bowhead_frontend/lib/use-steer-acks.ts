/**
 * use-steer-acks — hook surfacing 🤝 steer acks for the current turn so
 * the SteerMessage bubble can mount in the shell layer (workspace.tsx)
 * above the composer, OUTSIDE the BotTurn scroll body.
 *
 * Phase 1.7-B (chat-redesign-2026-05-21 tech-design §4.6): SteerMessage
 * relocates out of TurnContainer (was inside the bot turn's flow) and
 * mounts pinned-above-composer per v8 `variant-a.jsx:720-736`. Subscribing
 * via this hook keeps workspace.tsx from reaching into turn-store
 * directly.
 */

import { useMemo } from "react";

import { useTurn, type EmojiAck } from "./turn-store";

/**
 * Returns the list of 🤝 emoji acks for the given turn, filtered to those
 * that carry steer text (the user's interjection echoed by backend). When
 * `turnId` is null/empty or no acks exist yet, returns an empty array.
 */
export function useSteerAcks(turnId: string | null | undefined): EmojiAck[] {
  const state = useTurn(turnId ?? "");
  return useMemo(() => {
    return (state?.emojiAcks ?? []).filter(
      (a) => a.emoji === "🤝" && Boolean(a.text),
    );
  }, [state?.emojiAcks]);
}
