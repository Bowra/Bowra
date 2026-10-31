"use client";

import { apiJson } from "./api-client";

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
