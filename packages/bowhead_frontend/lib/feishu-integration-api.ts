import { apiJson } from "./api-client";

export interface FeishuPublicHealth {
  ok: boolean;
  enabled: boolean;
  degraded: boolean;
}

export interface FeishuDiagnostics extends FeishuPublicHealth {
  app_type: string;
  event_ingress: string;
  group_public_answers_enabled: boolean;
  auth_mode: string;
  prod_auth_mode: boolean;
  long_connection: null | {
    running?: boolean;
    thread_alive?: boolean;
    start_error?: string | null;
  };
  long_connection_restart_required?: boolean;
  long_connection_restart_reason?: string | null;
  long_connection_restart_changed_fields?: string[];
  missing_credentials?: string[];
  missing_config: string[];
  missing_llm_provider_secrets?: string[];
  app_id_configured: boolean;
  app_secret_configured: boolean;
  verification_token_configured: boolean;
  encrypt_key_configured: boolean;
  bot_open_id_configured: boolean;
  event_store_configured: boolean;
  event_subscription_url: string;
  card_callback_url: string;
  tenant_token_cache_keys: unknown[];
  required_event_subscriptions: string[];
  required_permissions: string[];
  setup_checklist?: FeishuSetupChecklistItem[];
  launch_record_template?: string;
}

export interface FeishuSetupChecklistItem {
  key: string;
  label: string;
  status: "ok" | "missing" | "degraded" | "manual";
  detail: string;
}

export interface FeishuSetupSession {
  id: string;
  state: string;
  app_type: string;
  tenant_key: string | null;
  workspace_id: string | null;
  expires_at: string;
  created_at: string;
  updated_at: string;
  setup_url: string;
  qr_payload: string;
  qr_svg_url: string;
  test_phrase: string;
}

export interface FeishuBindingSession {
  id: string;
  state: string;
  tenant_key: string;
  feishu_email: string | null;
  expires_at: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  current_user_email: string | null;
  current_user_id: string;
  already_bound_to_current_user: boolean;
  conflict: boolean;
  can_complete: boolean;
}

export interface FeishuAdminConfig {
  source: "env" | "db";
  tenant_key: string;
  app_type: "internal" | "store";
  enabled: boolean;
  event_ingress: "webhook" | "long_connection" | "disabled";
  app_id: string;
  bot_open_id: string;
  group_public_answers_enabled: boolean;
  app_secret_configured: boolean;
  verification_token_configured: boolean;
  encrypt_key_configured: boolean;
  secret_sources: {
    app_secret: "db" | "env" | "missing";
    verification_token: "db" | "env" | "missing";
    encrypt_key: "db" | "env" | "missing";
  };
  updated_at: string | null;
  updated_by_user_id: string | null;
  long_connection_restart_required?: boolean;
}

export interface FeishuAdminConfigUpdate {
  enabled?: boolean;
  event_ingress?: "webhook" | "long_connection";
  app_id?: string;
  app_secret?: string;
  verification_token?: string;
  encrypt_key?: string;
  bot_open_id?: string;
  group_public_answers_enabled?: boolean;
}

export function fetchFeishuHealth(): Promise<FeishuPublicHealth> {
  return apiJson<FeishuPublicHealth>("/api/v1/integrations/feishu/health");
}

export function fetchFeishuDiagnostics(): Promise<FeishuDiagnostics> {
  return apiJson<FeishuDiagnostics>("/api/v1/integrations/feishu/admin/diagnostics");
}

export function fetchFeishuAdminConfig(): Promise<FeishuAdminConfig> {
  return apiJson<FeishuAdminConfig>("/api/v1/integrations/feishu/admin/config");
}

export async function updateFeishuAdminConfig(body: FeishuAdminConfigUpdate): Promise<FeishuAdminConfig> {
  return apiJson<FeishuAdminConfig>("/api/v1/integrations/feishu/admin/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function createFeishuSetupSession(): Promise<FeishuSetupSession> {
  return apiJson<FeishuSetupSession>("/api/v1/integrations/feishu/admin/setup-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app_type: "internal_guided" }),
  });
}

export function fetchFeishuSetupSession(sessionId: string): Promise<FeishuSetupSession> {
  return apiJson<FeishuSetupSession>(
    `/api/v1/integrations/feishu/admin/setup-sessions/${encodeURIComponent(sessionId)}`,
  );
}

export function fetchFeishuBindingSession(sessionId: string): Promise<FeishuBindingSession> {
  return apiJson<FeishuBindingSession>(`/api/v1/integrations/feishu/bindings/${encodeURIComponent(sessionId)}`);
}

export async function completeFeishuBindingSession(sessionId: string): Promise<FeishuBindingSession> {
  return apiJson<FeishuBindingSession>(`/api/v1/integrations/feishu/bindings/${encodeURIComponent(sessionId)}/complete`, {
    method: "POST",
  });
}
