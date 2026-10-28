import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ manual: vi.fn(), legacy: vi.fn() }));
vi.mock("./subagent-drafts-api", () => ({
  getManualSubagentDraft: mocks.manual,
  getSubagentDraft: mocks.legacy,
  SubagentDraftApiError: class extends Error {
    constructor(public status: number) { super("draft unavailable"); }
  },
}));

import { SubagentDraftApiError } from "./subagent-drafts-api";
import { lookupSubagentDraft } from "./subagent-draft-lookup";

afterEach(() => vi.clearAllMocks());

describe("draft deep-link lookup", () => {
  it("opens an existing Canvas draft without falling back", async () => {
    const draft = { draft_id: "sad_canvas" };
    mocks.manual.mockResolvedValue(draft);
    await expect(lookupSubagentDraft("sad_canvas")).resolves.toEqual({ kind: "canvas", draft });
    expect(mocks.legacy).not.toHaveBeenCalled();
  });

  it("keeps a valid legacy AI draft resumable", async () => {
    mocks.manual.mockRejectedValue(new SubagentDraftApiError(404, "not_found", "missing"));
    mocks.legacy.mockResolvedValue({ draft_id: "sad_legacy" });
    await expect(lookupSubagentDraft("sad_legacy")).resolves.toEqual({ kind: "legacy" });
  });

  it("reports a missing link only after both draft versions return 404", async () => {
    mocks.manual.mockRejectedValue(new SubagentDraftApiError(404, "not_found", "missing"));
    mocks.legacy.mockRejectedValue(new SubagentDraftApiError(404, "not_found", "missing"));
    await expect(lookupSubagentDraft("sad_missing")).resolves.toEqual({ kind: "not_found" });
  });

  it("preserves temporary failures for retry", async () => {
    mocks.manual.mockRejectedValue(new Error("offline"));
    await expect(lookupSubagentDraft("sad_unavailable")).rejects.toThrow("offline");
    expect(mocks.legacy).not.toHaveBeenCalled();
  });
});
