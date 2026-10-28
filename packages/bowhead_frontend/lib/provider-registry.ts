/**
 * Provider Registry · preferred-model pack/unpack helpers.
 *
 * History: formerly included legacy provider templates (PROVIDER_TEMPLATES / instantiateFromTemplate /
 * validateProviderConfig / getProviderTemplate / migrateFromLegacy), removed in the tiering sprint
 * (2026-07-22). Their provider keys (doubao_ark / ollama_local …) disagreed with routing.yaml and
 * backend team config, risking drift (building UI from them would produce unsupported backend providers), and had no production references for some time.
 *
 * Team provider configuration now uses `/api/v1/admin/llm-config` (`components/settings/models.tsx`); chat
 * picker reads `/api/v1/llm/catalog`. This module only retains Composer picker ↔ localStorage
 * preferred_model encoding/decoding.
 */

/** `"provider_id:model"` ↔ `{ providerId, model }` */
export function packPreferredModel(providerId: string, model: string): string {
  return `${providerId}:${model}`;
}

export function unpackPreferredModel(s: string): { providerId: string; model: string } | null {
  const idx = s.indexOf(":");
  if (idx < 0) return null;
  return { providerId: s.slice(0, idx), model: s.slice(idx + 1) };
}
