import { apiJson } from "@/lib/api-client";

/** A non-read Team MCP call waiting for the requester (FEATURE_TEAM_TOOL_CONFIRMATION). */
export interface TeamToolConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  team_id: string;
  source_name: string;
  tool_ref: string;
  tool_name: string;
  description: string;
  /** Effective category of the tool; older servers omit it, which reads as unknown. */
  category?: "external_write" | "destructive" | "unknown";
  arguments: Record<string, unknown>;
  expires_at: string;
}

export function teamToolConfirmationApi<T>(path: string, method = "GET", body?: unknown) {
  return apiJson<T>(`/api/v1/team-tool-confirmations${path}`, {
    method,
    cache: "no-store",
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
}
