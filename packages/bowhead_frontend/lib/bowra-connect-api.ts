"use client";

import { apiJson, readApiError } from "./api-client";

import { fetchWithRefresh } from "./auth-api";

export interface BowraCallbackResult {
  state: string;
  code?: string;
  error?: string;
}

export async function completeBowraConnection(result: BowraCallbackResult): Promise<Record<string, string>> {
  // Refresh expired access cookies without forwarding the callback through login.
  const response = await fetchWithRefresh("/api/v1/admin/llm-config/bowra/complete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(result),
  });
  if (!response.ok) throw await readApiError(response);
  return response.json();
}

export interface BowraImport {
  base_url: string;
  api_key: string;
  models: string[];
}

export function startBowraConnection(): Promise<{ authorize_url: string }> {
  return apiJson("/api/v1/admin/llm-config/bowra/start", { method: "POST" });
}

export function claimBowraImport(token: string): Promise<BowraImport> {
  return apiJson(`/api/v1/admin/llm-config/bowra/imports/${encodeURIComponent(token)}`, { method: "POST" });
}
