import { apiJson } from "@/lib/api-client";

export interface PersonalMcpTool {
  id: string;
  remote_name: string;
  description: string;
  risk: "unknown" | "write" | "read";
  selected: number;
  schema_digest: string;
}
export interface PersonalMcpConnection {
  id: string;
  name: string;
  endpoint: string;
  auth_type: "none" | "bearer" | "api_key";
  status: string;
  version: number;
  tools: PersonalMcpTool[];
}
export interface PersonalMcpAgent {
  id: string;
  display_name: string;
  /** Route name; also displayed when display_name is empty (matching the creation wizard's empty-means-slug behavior). */
  slug: string;
  revision_id: string;
  tool_ids_json: string;
  binding_version: number;
}
export interface PersonalMcpConfirmation {
  id: string;
  turn_id: string;
  session_id: string;
  connection_name: string;
  tool_name: string;
  description: string;
  agent_name: string;
  expires_at: string;
  arguments: Record<string, unknown>;
}
export function personalMcpApi<T>(
  path: string,
  method = "GET",
  body?: unknown,
) {
  return apiJson<T>(`/api/v1/personal-mcp${path}`, {
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
