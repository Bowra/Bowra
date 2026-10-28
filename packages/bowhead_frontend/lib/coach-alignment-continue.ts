/**
 * Telling Coach to continue after the Owner acts on an alignment card. The message is an "@Coach …" input
 * document, so the Turn reaches Coach in the same loop rather than the Lead, and it goes through the
 * composer's ordinary Send path, but only into an empty composer, and only that exact message is sent:
 * a draft the Owner is writing, a recovered submission or a staged chat action is never sent in its place.
 */
import type { ComposerDraftControl } from "@/components/chat/v2-composer";

/** The system Coach's reference id in the @ catalog. */
export const COACH_AGENT_ID = "system:agent-coach";

/** Whether the composer is empty, so the continue message may be staged there. */
export function composerAcceptsContinue(control: ComposerDraftControl | null | undefined): boolean {
  if (!control) return false;
  const input = control.readInput();
  return !input.text.trim() && !input.hasAttachments && !input.hasInputDocument;
}

/** After a prefill was consumed: send only when the composer took exactly the staged "@Coach …" message. */
export function shouldSendStagedContinue(
  pending: string | null,
  disposition: "applied" | "discarded" | undefined,
  control: ComposerDraftControl | null | undefined,
): boolean {
  if (!pending || disposition !== "applied" || !control) return false;
  const input = control.readInput();
  return input.hasInputDocument && !input.hasAttachments && input.text === pending;
}
