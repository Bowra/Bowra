import { getActiveSpaceId } from "./space-storage";
import { getActiveTeamId } from "./team-storage";

export interface SubagentDraftRequestContext {
  teamId: string;
  workspaceId: string;
}

/** Capture only at an entry point; retained objects keep their original context. */
export function captureSubagentDraftContext(workspaceId = getActiveSpaceId()): Readonly<SubagentDraftRequestContext> {
  return Object.freeze({ teamId: getActiveTeamId(), workspaceId });
}
