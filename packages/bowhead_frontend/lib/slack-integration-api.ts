import { apiJson } from "./api-client";

export type SlackAdminConfig = {
  configured: boolean;
  id?: string;
  client_id?: string;
  slack_team_id?: string;
  bot_user_id?: string;
  status: "draft" | "verified" | "enabled" | "disabled";
  channel_answers_enabled: boolean;
  client_secret_configured?: boolean;
  signing_secret_configured?: boolean;
  bot_token_configured?: boolean;
  slack_settings?: {
    events: string;
    slash_commands: string;
    interactivity: string;
  } | null;
  event_url?: string;
  command_url?: string;
  interaction_url?: string;
  oauth_callback_url?: string;
  personal_mcp_oauth_callback_url?: string | null;
  required_scopes?: string[];
};

export type SlackAdminConfigUpdate = {
  client_id?: string;
  client_secret?: string;
  signing_secret?: string;
  channel_answers_enabled?: boolean;
  status?: SlackAdminConfig["status"];
};

export function fetchSlackAdminConfig() {
  return apiJson<SlackAdminConfig>("/api/v1/integrations/slack/admin/config");
}

export function updateSlackAdminConfig(body: SlackAdminConfigUpdate) {
  return apiJson<SlackAdminConfig>("/api/v1/integrations/slack/admin/config", {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

export function beginSlackInstall() {
  return apiJson<{ install_url: string }>("/api/v1/integrations/slack/admin/install", { method: "POST" });
}
