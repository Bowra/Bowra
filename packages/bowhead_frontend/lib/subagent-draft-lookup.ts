import {
  getManualSubagentDraft,
  getSubagentDraft,
  SubagentDraftApiError,
  type ManualSubagentDraftDetail,
  type SubagentDraftRequestContext,
} from "./subagent-drafts-api";

export type SubagentDraftLookup =
  | { kind: "canvas"; draft: ManualSubagentDraftDetail }
  | { kind: "legacy" }
  | { kind: "not_found" };

/** A Canvas 404 may still be a resumable conversational draft from the earlier flow. */
export async function lookupSubagentDraft(draftId: string, context?: SubagentDraftRequestContext): Promise<SubagentDraftLookup> {
  try {
    return { kind: "canvas", draft: await getManualSubagentDraft(draftId, context) };
  } catch (cause) {
    if (!(cause instanceof SubagentDraftApiError) || cause.status !== 404) throw cause;
  }
  try {
    await getSubagentDraft(draftId, undefined, context);
    return { kind: "legacy" };
  } catch (cause) {
    if (cause instanceof SubagentDraftApiError && cause.status === 404) return { kind: "not_found" };
    throw cause;
  }
}
