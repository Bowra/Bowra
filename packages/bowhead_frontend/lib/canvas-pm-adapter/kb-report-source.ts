"use client";

/** Knowledge base sources in reports (KB answer citations, batch 3): original-text fetching and location lines shared by citation popovers and source panels.
 *
 *  Call only when the backend permits viewing (`linked_accessible` + kb location). Do not persist original text with the report; fetch under the viewer's
 *  current permissions (`kb_space_id` supplies the space header, using the same gate as the knowledge base page). Location lines match chat source cards:
 *  Knowledge Base · Scope · Page → Section → Paragraph N. */

import { useTranslations } from "next-intl";

import { kbChunkLocation } from "@/components/chat/step-list/LeadNarrative";
import { useKbChunkContent, type KbChunkState } from "./kb-chunk-cache";

export interface KbSourceLocator {
  fileId: string;
  chunkId: string;
  scope?: string;
  spaceId?: string;
}

export function useKbReportSource(kb: KbSourceLocator | null): { state: KbChunkState; location: string; warning: string | null } {
  const tStep = useTranslations("step_list");
  const state = useKbChunkContent(kb?.fileId, kb?.chunkId, kb?.spaceId, { single: true });
  const scopeKey = kb?.scope === "personal" || kb?.scope === "space" || kb?.scope === "team" ? kb.scope : null;
  const where = state.status === "ready" ? kbChunkLocation(state.chunk, tStep) : null;
  const location = [tStep("cite_kb_prefix"), scopeKey ? tStep(`cite_kb_scope_${scopeKey}`) : null, where]
    .filter(Boolean)
    .join(" · ");
  const warning = state.status === "ready" && state.chunk.current_source_state && state.chunk.current_source_state !== "eligible" ? tStep("cite_kb_current_warning") : null;
  return { state, location, warning };
}
