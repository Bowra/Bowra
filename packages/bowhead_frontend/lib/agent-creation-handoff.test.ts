import { afterEach, describe, expect, it } from "vitest";

import {
  agentCreationDraftId,
  showAgentCreationDraftInUrl,
} from "./agent-creation-handoff";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("agent draft URL handoff", () => {
  it("keeps a server draft addressable across reload without losing the active session", () => {
    window.history.replaceState(null, "", "/?session=s_1#canvas");

    showAgentCreationDraftInUrl("sad_abc123");
    expect(window.location.href).toContain("session=s_1&agent_draft=sad_abc123#canvas");
    expect(agentCreationDraftId(new URLSearchParams(window.location.search))).toBe("sad_abc123");

    showAgentCreationDraftInUrl(null);
    expect(window.location.href).toContain("/?session=s_1#canvas");
  });

  it("ignores invalid draft ids from the URL", () => {
    expect(agentCreationDraftId(new URLSearchParams("agent_draft=other"))).toBeNull();
  });
});
