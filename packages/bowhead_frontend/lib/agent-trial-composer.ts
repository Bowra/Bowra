/**
 * A new Owner draft trial starts with an empty composer, like 新工作 (2026-11-27 dogfood F26). The new-chat draft
 * left from earlier (often another Agent's @ message) would otherwise fill the trial's input, and it can't be sent
 * as a trial question. A draft holding a pending send is kept: the discard refuses it.
 */

import { discardComposerOrdinaryDraft, type ComposerDraftScope } from "./composer-ordinary-draft";
import { composerDraftScopeSignature } from "./composer-persistence-scope";
import { forgetComposerTransientDraft } from "./composer-transient-draft";

/** Discard the new-chat draft of `scope`; resolves whether it did, so the page remounts the composer empty. */
export async function clearNewChatDraftForTrial(scope: ComposerDraftScope): Promise<boolean> {
  // Only the new-chat draft: a session's own draft belongs to that conversation.
  if (scope.requestedSessionId) return false;
  try {
    await discardComposerOrdinaryDraft(scope);
  } catch {
    return false;
  }
  forgetComposerTransientDraft(composerDraftScopeSignature(scope));
  return true;
}
