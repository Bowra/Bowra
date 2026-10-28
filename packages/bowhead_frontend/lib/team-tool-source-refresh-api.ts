/**
 * Two-step tool-list refresh for a team MCP tool source.
 *
 * Backend: POST /api/v1/team-tool-sources/{id}/refresh-preview and /refresh-apply
 * (team owner / team admin, not on read-only teams). Preview re-lists the remote
 * server's tools and freezes a server-owned candidate for five minutes; apply
 * saves exactly that candidate, guarded by the short-lived impact token and an
 * idempotency key. The legacy one-step `/refresh` is refused while the Impact
 * Guard is on, so the UI always uses this pair.
 */

import { apiFetch, readApiError } from "./api-client";
import type { ResourceUsageSummary } from "./resource-usage";
import { TeamToolApiError } from "./team-tool-sources-api";

const BASE = "/api/v1/team-tool-sources";

/** A tool listed before and after the refresh whose definition changed. */
export interface ToolSourceChangedTool {
  name: string;
  /** Which parts of the definition changed. */
  fields: Array<"description" | "input_schema">;
  /** Whether the source currently imports this tool (Agents see the new definition after apply). */
  imported: boolean;
}

/** Tool and Prompt name changes shared by the preview and apply responses. */
export interface ToolSourceRefreshDiff {
  kind: "mcp";
  /** Tool names the server lists now but the source did not have. */
  added: string[];
  /** Tool names the source had but the server no longer lists. */
  removed: string[];
  /**
   * Tools kept by the server whose description or input schema changed. Absent only from
   * gateways that predate the field; then whether existing tools changed is unknown.
   */
  changed?: ToolSourceChangedTool[];
  /** Imported (selected) tools among `removed`: Agents lose them after apply. */
  selected_dropped: string[];
  /** Tools listed by the server after the refresh. */
  tools_count: number;
  prompts_added: string[];
  prompts_removed: string[];
  prompts_count: number;
}

export interface ToolSourceRefreshPreview extends ToolSourceRefreshDiff {
  /** Names the frozen candidate; apply must send it back unchanged. */
  candidate_digest: string;
  impact_token: string;
  expires_in_seconds: number;
  /** Current dependency counts on this source (Agents, running tasks, schedules). */
  summary: ResourceUsageSummary;
  /** Active schedules bound to this source's tools; paused when imported tools are dropped. */
  required_follow_up: Array<{ kind: "schedule_reconfirm"; job_id: string }>;
}

export interface ToolSourceRefreshResult extends ToolSourceRefreshDiff {
  paused_schedule_ids: string[];
}

async function _ok(response: Response): Promise<Response> {
  if (response.ok) return response;
  const error = await readApiError(response);
  throw new TeamToolApiError(error.status, error.message, error.code, error.requestId);
}

export async function previewToolSourceRefresh(sourceId: string): Promise<ToolSourceRefreshPreview> {
  const response = await apiFetch(`${BASE}/${encodeURIComponent(sourceId)}/refresh-preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  await _ok(response);
  return (await response.json()) as ToolSourceRefreshPreview;
}

export async function applyToolSourceRefresh(
  sourceId: string,
  request: { candidateDigest: string; impactToken: string; idempotencyKey: string },
): Promise<ToolSourceRefreshResult> {
  const response = await apiFetch(`${BASE}/${encodeURIComponent(sourceId)}/refresh-apply`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Bowhead-Impact-Token": request.impactToken,
      "Idempotency-Key": request.idempotencyKey,
    },
    body: JSON.stringify({ candidate_digest: request.candidateDigest }),
  });
  await _ok(response);
  return (await response.json()) as ToolSourceRefreshResult;
}
