// email-config sprint M2—team-level SMTP configuration API client.
// Backend: routes/admin_email_config.py (tech-design §6b).
import { apiJson } from "./api-client";

export interface EmailConfigView {
  host: string;
  port: number;
  user: string;
  from_addr: string;
  from_name: string;
  tls: boolean;
  password_set: boolean; // Boolean only (no plaintext/fingerprint)
}

export interface EmailConfigResponse {
  configured: "team" | "env" | "none";
  version: number;
  config?: EmailConfigView; // Returned only when configured==="team"
}

export interface EmailConfigUpdate {
  host: string;
  port: number;
  user: string;
  password: string; // Empty = retain old password
  from_addr: string;
  from_name: string;
  tls: boolean;
}

export interface EmailTestResult {
  ok: boolean;
  source: "team" | "env";
  error_code: string | null;
  /** Localized API fallback for non-browser clients; the UI renders error_code. */
  error: string | null;
}

const BASE = "/api/v1/admin/email-config";

export async function getEmailConfig(): Promise<EmailConfigResponse> {
  return apiJson<EmailConfigResponse>(BASE);
}

export async function putEmailConfig(body: EmailConfigUpdate): Promise<{ ok: boolean }> {
  return apiJson<{ ok: boolean }>(BASE, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function deleteEmailConfig(): Promise<{ ok: boolean }> {
  return apiJson<{ ok: boolean }>(BASE, { method: "DELETE" });
}

export async function sendTestEmail(): Promise<EmailTestResult> {
  return apiJson<EmailTestResult>(`${BASE}/test`, { method: "POST" });
}
