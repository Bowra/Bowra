import { apiJson } from "./api-client";

export interface OperationsDay {
  day: string;
  login_users: number;
  sessions: number;
  input_tokens: number;
  output_tokens: number;
  background_tokens: number;
  skill_loads: number;
  tool_loads: number;
  tool_calls: number;
}

export interface TeamOperationsReport {
  timezone: string;
  days: OperationsDay[];
}

export function getTeamOperations(days: 1 | 7 | 30): Promise<TeamOperationsReport> {
  return apiJson(`/api/v1/team-operations?days=${days}`);
}

export function saveTeamOperationsTimezone(timezone: string): Promise<{ timezone: string }> {
  return apiJson("/api/v1/team-operations/timezone", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone }),
  });
}
