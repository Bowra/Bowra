"use client";

/**
 * credential-readiness R9: status of three kb capabilities (readable by members).
 * Shared by the contextual /knowledge notice and upload dialog; a module-level 60s cache coalesces their requests.
 * Fail silently with null (notices only enhance the UI and must never block the knowledge base page itself).
 */

import { fetchWithRefresh } from "@/lib/auth-api";

export type KbCapabilityState = "enabled" | "disabled" | "invalid" | "unknown";

export interface KbCapabilities {
  kb_rerank: KbCapabilityState;
  kb_vision: KbCapabilityState;
  kb_row_extractor: KbCapabilityState;
}

const TTL_MS = 60_000;
let cache: { at: number; promise: Promise<KbCapabilities | null> } | null = null;

export function fetchKbCapabilities(): Promise<KbCapabilities | null> {
  if (cache && Date.now() - cache.at < TTL_MS) return cache.promise;
  const promise = (async (): Promise<KbCapabilities | null> => {
    try {
      const r = await fetchWithRefresh("/api/v1/team/kb-capabilities");
      if (!r.ok) return null;
      const data = (await r.json()) as { capabilities?: KbCapabilities };
      return data.capabilities ?? null;
    } catch {
      return null;
    }
  })();
  cache = { at: Date.now(), promise };
  return promise;
}
