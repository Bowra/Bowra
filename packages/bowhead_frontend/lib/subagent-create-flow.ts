import type { SubagentDraftDetail } from "./subagent-drafts-api";
import type { CreateSubagentOpenPayload } from "./types";

/** Keep user-stated naming hints when switching between AI and manual creation. */
export function manualSubagentPayload(
  current: CreateSubagentOpenPayload | null,
  purposeHint: string | null,
): CreateSubagentOpenPayload {
  return {
    purpose_hint: purposeHint,
    draft_id: null,
    draft_status: null,
    draft_title: null,
    name_hint: current?.name_hint ?? null,
    display_name_hint: current?.display_name_hint ?? null,
  };
}

export function reviewSubagentPayload(
  current: CreateSubagentOpenPayload | null,
  draft: SubagentDraftDetail,
): CreateSubagentOpenPayload {
  return {
    purpose_hint: draft.purpose,
    draft_id: draft.draft_id,
    draft_status: draft.draft_status,
    draft_title: draft.definition?.display_name ?? null,
    name_hint: current?.name_hint ?? null,
    display_name_hint: current?.display_name_hint ?? null,
  };
}
