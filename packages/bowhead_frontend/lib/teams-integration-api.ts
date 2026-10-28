import { apiJson } from "./api-client";

export type TeamsAdminConfig = {
  configured: boolean;
  status: "draft" | "verified" | "enabled" | "disabled";
  entra_tenant_id?: string;
  microsoft_app_id?: string;
  app_type?: "SingleTenant" | "UserAssignedMSI";
  sso_auth_app_id?: string;
  sso_audience?: string;
  sso_connection_name?: string;
  teams_manifest_id?: string;
  secret_expires_at?: string | null;
  app_secret_configured?: boolean;
  allowed_scopes: ["personal"];
};

export type TeamsAdminConfigUpdate = Partial<Omit<TeamsAdminConfig, "configured" | "allowed_scopes" | "app_secret_configured">> & { app_secret?: string };

export function fetchTeamsAdminConfig() {
  return apiJson<TeamsAdminConfig>("/api/v1/integrations/teams/admin/config");
}

export function updateTeamsAdminConfig(body: TeamsAdminConfigUpdate) {
  return apiJson<TeamsAdminConfig>("/api/v1/integrations/teams/admin/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
