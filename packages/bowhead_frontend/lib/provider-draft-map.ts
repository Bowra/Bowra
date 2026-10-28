/** Copilot draft → ConnectDraft mapping (batch 1-d WP-2, tech-design-batch1d §7-2).
 *
 * Server DTO mirrors the **visible subset** of ConnectDraft, but shapes differ. Design review
 * identified three mismatches this layer must handle or the form receives invalid values:
 *
 * 1. `request_body_overrides`: DTO object versus `DraftModel.requestBodyOverrides` **JSON text**
 *    (textarea editing, parsed only before save) → `JSON.stringify`;
 * 2. Required `DraftModel.source` is `"preset" | "manual"`, absent in DTO → supply `"manual"`;
 * 3. Rename snake_case ↔ camelCase field by field.
 *
 * Map only fields **actually produced by the model**: copilot fills rather than resets the form; absent fields retain user values
 * (host passes result to `patchConnect(partial)`).
 */

export interface ProviderDraftModel {
  id?: string;
  caps?: string[];
  deployment?: string;
  request_body_overrides?: Record<string, Record<string, unknown>>;
  outbound_params?: Record<string, { mode: string; value?: number }>;
}

export interface ProviderDraftFields {
  protocol?: string;
  name?: string;
  base_url?: string;
  api_version?: string;
  messages_path?: string;
  auth_type?: string;
  auth_header_name?: string;
  auth_prefix?: string;
  models?: ProviderDraftModel[];
}

export interface MappedConnectPatch {
  protocol?: string;
  name?: string;
  baseUrl?: string;
  apiVersion?: string;
  messagesPath?: string;
  authType?: string;
  authHeaderName?: string;
  authPrefix?: string;
  models?: { id: string; caps: string[]; source: "manual"; deployment?: string; requestBodyOverrides?: string }[];
}

/** Backend names → ConnectDraft names (`models` handled separately because shapes differ). */
const SCALAR_KEYS: Readonly<Record<string, keyof MappedConnectPatch>> = {
  protocol: "protocol",
  name: "name",
  base_url: "baseUrl",
  api_version: "apiVersion",
  messages_path: "messagesPath",
  auth_type: "authType",
  auth_header_name: "authHeaderName",
  auth_prefix: "authPrefix",
};

export function mapProviderDraftToConnect(fields: ProviderDraftFields): MappedConnectPatch {
  const patch: MappedConnectPatch = {};
  for (const [backendKey, draftKey] of Object.entries(SCALAR_KEYS)) {
    const value = (fields as Record<string, unknown>)[backendKey];
    if (typeof value === "string") {
      // Pass even empty strings: explicit model clearing (e.g. anthropic needs no api_version) is meaningful output.
      (patch as Record<string, unknown>)[draftKey] = value;
    }
  }
  if (Array.isArray(fields.models)) {
    patch.models = fields.models
      .filter((model) => typeof model?.id === "string" && model.id.trim())
      .map((model) => {
        const overrides = model.request_body_overrides;
        const row: NonNullable<MappedConnectPatch["models"]>[number] = {
          id: model.id!.trim(),
          caps: Array.isArray(model.caps) ? model.caps.filter((cap) => typeof cap === "string") : [],
          source: "manual", // Absent in DTO, required by host.
        };
        if (typeof model.deployment === "string" && model.deployment) row.deployment = model.deployment;
        if (overrides && Object.keys(overrides).length > 0) {
          row.requestBodyOverrides = JSON.stringify(overrides, null, 2); // Host uses JSON text in textarea.
        }
        return row;
      });
  }
  return patch;
}

/** Secrets extracted through side channel → direct apiKey fill. */
export interface SecretHitLike {
  value: string;
  label?: string | null;
  kind?: string | null;
}

export interface SecretFillResult {
  /** Value for ConnectDraft.apiKey; null means no directly fillable candidate. */
  apiKey: string | null;
  /** `Bearer ` prefix also fixes authentication method to bearer. */
  authType?: "bearer";
  /** Unused candidates (let user choose labels, never guess). */
  others: string[];
}

const BEARER_PREFIX = /^Bearer\s+/i;

/** Fill directly from first candidate; **detect prefix by value shape**, not label/kind.
 *
 * Observed curl hit: `kind="kv"` / `label="Authorization"` / `value` contains `Bearer ` prefix;
 * design v1 used separate label/kind criteria, each matching the other's shape (review P2-2).
 * Do not reimplement server word-level `_is_secret_key` heuristic in frontend either (review P2-3: inevitable drift).
 */
export function pickSecretForApiKey(hits: readonly SecretHitLike[]): SecretFillResult {
  const usable = hits.filter((hit) => typeof hit?.value === "string" && hit.value.trim());
  if (usable.length === 0) return { apiKey: null, others: [] };
  const [first, ...rest] = usable;
  const stripped = first.value.replace(BEARER_PREFIX, "").trim();
  const result: SecretFillResult = {
    apiKey: stripped || null,
    others: rest.map((hit) => hit.label || hit.kind || "(未命名)"),
  };
  if (BEARER_PREFIX.test(first.value)) result.authType = "bearer";
  return result;
}
