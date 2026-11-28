// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./composer-ordinary-draft", () => ({ discardComposerOrdinaryDraft: vi.fn(async () => undefined) }));
vi.mock("./composer-transient-draft", () => ({ forgetComposerTransientDraft: vi.fn() }));

import { clearNewChatDraftForTrial } from "./agent-trial-composer";
import { discardComposerOrdinaryDraft } from "./composer-ordinary-draft";
import { composerDraftScopeSignature } from "./composer-persistence-scope";
import { forgetComposerTransientDraft } from "./composer-transient-draft";

const NEW_CHAT = { userId: "u_1", teamId: "team_1", workspaceId: "ws_1", requestedSessionId: null, authSessionGeneration: "g_1" };

afterEach(() => vi.clearAllMocks());

describe("clearNewChatDraftForTrial (F26)", () => {
  it("discards the new-chat draft left from earlier, stored and in memory", async () => {
    // Mutation: skip the discard → the trial opens with another Agent's @ message in its input.
    await expect(clearNewChatDraftForTrial(NEW_CHAT)).resolves.toBe(true);
    expect(discardComposerOrdinaryDraft).toHaveBeenCalledWith(NEW_CHAT);
    expect(forgetComposerTransientDraft).toHaveBeenCalledWith(composerDraftScopeSignature(NEW_CHAT));
  });

  it("leaves a conversation's own draft alone", async () => {
    await expect(clearNewChatDraftForTrial({ ...NEW_CHAT, requestedSessionId: "s_1" })).resolves.toBe(false);
    expect(discardComposerOrdinaryDraft).not.toHaveBeenCalled();
  });

  it("keeps a draft whose send is pending (the discard refuses it)", async () => {
    vi.mocked(discardComposerOrdinaryDraft).mockRejectedValueOnce(new Error("composer pending action cannot be discarded"));
    await expect(clearNewChatDraftForTrial(NEW_CHAT)).resolves.toBe(false);
    expect(forgetComposerTransientDraft).not.toHaveBeenCalled();
  });
});
