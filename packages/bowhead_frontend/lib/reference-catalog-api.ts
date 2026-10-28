import {
  ReferenceParameterSchema,
  type ParameterSchemaNode,
} from "./reference-parameter-schema";
import { ApiError, apiJson } from "./api-client";
import {
  parseResourceReference,
  type ResourceRef,
} from "./composer-input-document";
import type { ComposerPersistenceScope } from "./composer-persistence-scope";

export type ReferenceEntryMode = "ordinary" | "steer" | "plan" | "resume";
export type ReferenceCatalogReason =
  | "unsupported_entry"
  | "unsupported_binding"
  | "resource_unavailable"
  | "catalog_adapter_unavailable"
  | "catalog_metadata_unavailable";
export type ReferenceCatalogGroupId =
  "team_agents" | "system_agents" | "skills" | "capabilities" | "mcp";
export type ParameterResourceRef = Extract<ResourceRef, { kind: "skill" | "capability" | "mcp_tool" | "mcp_prompt" }>;
export interface ReferenceCatalogSelection {
  trigger: "@" | "/";
  query: string;
  entryMode: ReferenceEntryMode;
  selectedRefs: ResourceRef[];
  exactResource?: ParameterResourceRef;
}
interface AgentCatalogItem {
  kind: "agent";
  source_kind: "team" | "system";
  id: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "agent" }> | null;
  availability: "unavailable" | "baseline";
  reason_code: ReferenceCatalogReason | null;
  /** Sent only for the viewer's own team Agents. */
  created_by_me?: boolean;
}
interface SkillCatalogItem {
  kind: "skill";
  source_kind: "builtin" | "user" | "team";
  id: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "skill" }> | null;
  read_only_parameter_schema: ParameterSchemaNode | null;
  availability: "unavailable" | "baseline" | "readonly_extension";
  reason_code: ReferenceCatalogReason | null;
}
interface CapabilityCatalogItem {
  kind: "capability";
  source_kind: "platform";
  id: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "capability" }>;
  parameter_schema: ParameterSchemaNode;
  availability: "unavailable" | "baseline";
  reason_code: ReferenceCatalogReason | null;
}
interface McpRemoteVersion {
  status: "unknown";
  value: null;
  evidence: null;
}
interface McpSourceCatalogItem {
  kind: "mcp_source";
  source_kind: "team";
  id: string;
  source_id: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "mcp_source" }> | null;
  remote_version: McpRemoteVersion;
  availability: "unavailable" | "baseline";
  reason_code: ReferenceCatalogReason | null;
}
interface McpToolCatalogItem {
  kind: "mcp_tool";
  source_kind: "team";
  id: string;
  source_id: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "mcp_tool" }> | null;
  parameter_schema: ParameterSchemaNode | null;
  remote_version: McpRemoteVersion;
  availability: "unavailable" | "baseline";
  reason_code: ReferenceCatalogReason | null;
}
interface McpPromptCatalogItem {
  kind: "mcp_prompt";
  source_kind: "team";
  id: string;
  source_id: string;
  prompt_name: string;
  label: string;
  summary: string;
  resource: Extract<ResourceRef, { kind: "mcp_prompt" }> | null;
  parameter_schema: ParameterSchemaNode | null;
  remote_version: McpRemoteVersion;
  availability: "unavailable" | "baseline";
  reason_code: ReferenceCatalogReason | null;
}
export type ReferenceCatalogItem =
  AgentCatalogItem | SkillCatalogItem | CapabilityCatalogItem | McpSourceCatalogItem | McpToolCatalogItem | McpPromptCatalogItem;
export function referenceCatalogItemIdentity(
  item: ReferenceCatalogItem,
): string {
  return JSON.stringify([item.kind, item.source_kind, item.id]);
}
function resourceRefIdentity(resource: ResourceRef): string {
  return JSON.stringify(Object.entries(resource).sort(([left], [right]) => left.localeCompare(right)));
}
export interface ReferenceEntrySupport {
  submission_protocol: "chat-references.v1";
  entry_mode: ReferenceEntryMode;
  resource_kind: ResourceRef["kind"];
  authorization_mode: "baseline" | "readonly_extension";
  adapter_id: "unavailable" | "initial-agent-skill" | "initial-capability" | "initial-mcp-read" | "initial-mcp-prompt";
  adapter_version: 0 | 1;
  supported: boolean;
  reason_code: "unsupported_entry" | null;
}
export interface ReferenceCatalogResponse {
  catalog_generation: string;
  input_schema_versions: [1];
  entry_support: ReferenceEntrySupport[];
  groups: {
    id: ReferenceCatalogGroupId;
    items: ReferenceCatalogItem[];
    error: ReferenceCatalogReason | null;
  }[];
  next_cursor: string | null;
}

const REASONS: ReferenceCatalogReason[] = [
  "unsupported_entry",
  "unsupported_binding",
  "resource_unavailable",
  "catalog_adapter_unavailable",
  "catalog_metadata_unavailable",
];
const KINDS: ResourceRef["kind"][] = [
  "agent",
  "skill",
  "capability",
  "mcp_source",
  "mcp_tool",
  "mcp_prompt",
];
const MODES: ReferenceEntryMode[] = ["ordinary", "steer", "plan", "resume"];
const IDENTITY = /^[A-Za-z0-9_.:-]+$/;

function invalid(): never {
  throw new ApiError(502, "Reference catalog response is not compatible", {
    code: "reference_catalog_response_invalid",
  });
}
function record(raw: unknown, keys: string[]): Record<string, unknown> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) invalid();
  const value = raw as Record<string, unknown>;
  if (
    Object.keys(value).length !== keys.length ||
    keys.some((key) => !Object.hasOwn(value, key))
  )
    invalid();
  return value;
}
function string(raw: unknown, max: number, empty = false): string {
  if (typeof raw !== "string" || (!empty && !raw) || [...raw].length > max)
    invalid();
  return raw;
}
function reason(raw: unknown): ReferenceCatalogReason {
  if (!REASONS.includes(raw as ReferenceCatalogReason)) invalid();
  return raw as ReferenceCatalogReason;
}

/** Selectable metadata requires the matching known entry cells and input shape.
 * Compatibility is projected by the server and rechecked at submission/use. */
export function parseReferenceCatalog(
  raw: unknown,
  selection: ReferenceCatalogSelection,
): ReferenceCatalogResponse {
  try {
    const data = record(raw, [
      "catalog_generation",
      "input_schema_versions",
      "entry_support",
      "groups",
      "next_cursor",
    ]);
    const generation = string(data.catalog_generation, 64);
    if (
      !/^[0-9a-f]{64}$/.test(generation) ||
      !Array.isArray(data.input_schema_versions) ||
      data.input_schema_versions.length !== 1 ||
      data.input_schema_versions[0] !== 1
    )
      invalid();
    if (!Array.isArray(data.entry_support) || data.entry_support.length !== 7)
      invalid();
    const cells = new Set<string>();
    const entrySupport = data.entry_support.map((rawCell) => {
      const cell = record(rawCell, [
        "submission_protocol",
        "entry_mode",
        "resource_kind",
        "authorization_mode",
        "adapter_id",
        "adapter_version",
        "supported",
        "reason_code",
      ]);
      if (
        cell.submission_protocol !== "chat-references.v1" ||
        cell.entry_mode !== selection.entryMode ||
        !KINDS.includes(cell.resource_kind as ResourceRef["kind"]) ||
        !["baseline", "readonly_extension"].includes(
          cell.authorization_mode as string,
        ) ||
        (cell.authorization_mode === "readonly_extension" &&
          cell.resource_kind !== "skill") ||
        !(cell.adapter_id === "unavailable"
          ? cell.supported === false && cell.adapter_version === 0 && cell.reason_code === "unsupported_entry"
            : ["initial-agent-skill", "initial-capability", "initial-mcp-read", "initial-mcp-prompt"].includes(cell.adapter_id as string)
            && cell.adapter_version === 1
            && (cell.adapter_id === "initial-agent-skill"
              ? ["ordinary", "steer", "plan"].includes(cell.entry_mode as string)
                && ["agent", "skill"].includes(cell.resource_kind as string)
              : cell.adapter_id === "initial-capability"
                ? ["ordinary", "steer"].includes(cell.entry_mode as string) && cell.resource_kind === "capability" && cell.authorization_mode === "baseline"
                : cell.adapter_id === "initial-mcp-prompt"
                  ? ["ordinary", "steer"].includes(cell.entry_mode as string) && cell.resource_kind === "mcp_prompt" && cell.authorization_mode === "baseline"
                : ["ordinary", "steer"].includes(cell.entry_mode as string) && ["mcp_source", "mcp_tool"].includes(cell.resource_kind as string) && cell.authorization_mode === "baseline")
            && (cell.supported === true && cell.reason_code === null || cell.supported === false && cell.reason_code === "unsupported_entry"))
      )
        invalid();
      const key = `${cell.resource_kind}:${cell.authorization_mode}`;
      if (cells.has(key)) invalid();
      cells.add(key);
      return { ...cell } as unknown as ReferenceEntrySupport;
    });
    const expectedGroups: ReferenceCatalogGroupId[] =
      selection.trigger === "@"
        ? ["team_agents", "system_agents"]
        : ["skills", "capabilities", "mcp"];
    if (
      !Array.isArray(data.groups) ||
      data.groups.length !== expectedGroups.length
    )
      invalid();
    const identities = new Set<string>();
    const groups = data.groups.map((rawGroup, index) => {
      const group = record(rawGroup, ["id", "items", "error"]);
      if (
        group.id !== expectedGroups[index] ||
        !Array.isArray(group.items) ||
        group.items.length > 20
      )
        invalid();
      if (group.error === "catalog_adapter_unavailable" && group.items.length)
        invalid();
      const items = group.items.map((rawItem) => {
        const isSkill =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "skill";
        const isCapability =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "capability";
        const isMcpSource =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "mcp_source";
        const isMcpTool =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "mcp_tool";
        const isMcpPrompt =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "mcp_prompt";
        // Additive to the v1 contract: a team Agent row may say the viewer created it (always `true` when sent).
        const ownAgent =
          rawItem &&
          typeof rawItem === "object" &&
          (rawItem as { kind?: unknown }).kind === "agent" &&
          Object.hasOwn(rawItem, "created_by_me");
        const item = record(rawItem, [
          "kind",
          "source_kind",
          "id",
          "label",
          "summary",
          "resource",
          "availability",
          "reason_code",
          ...(ownAgent ? ["created_by_me"] : []),
          ...(isSkill
            ? ["read_only_parameter_schema"]
            : isCapability || isMcpTool || isMcpPrompt
              ? ["parameter_schema"]
              : []),
          ...(isMcpSource || isMcpTool || isMcpPrompt
            ? ["source_id", "remote_version"]
            : []),
          ...(isMcpPrompt ? ["prompt_name"] : []),
        ]);
        const id = string(item.id, 512);
        const identity = JSON.stringify([item.kind, item.source_kind, id]);
        if (
          !IDENTITY.test(id) ||
          identities.has(identity) ||
          (isSkill
            ? group.id !== "skills" ||
              !["builtin", "user", "team"].includes(item.source_kind as string) ||
              (item.source_kind === "builtin" && !id.startsWith("builtin:"))
            : isCapability
              ? group.id !== "capabilities" || item.source_kind !== "platform"
            : isMcpSource
              ? group.id !== "mcp" || item.source_kind !== "team" || item.source_id !== id
            : isMcpTool
              ? group.id !== "mcp" || item.source_kind !== "team"
            : isMcpPrompt
              ? group.id !== "mcp" || item.source_kind !== "team"
            : item.kind !== "agent" ||
              (item.source_kind === "team"
                ? group.id !== "team_agents"
                : item.source_kind === "system"
                  ? group.id !== "system_agents" || !id.startsWith("system:")
                  : true)) ||
          !(item.availability === "unavailable" || item.availability === "baseline" || isSkill && item.availability === "readonly_extension")
        )
          invalid();
        identities.add(identity);
        const resource =
          item.resource === null ? null : parseResourceReference(item.resource);
        const invalidMcpResource =
          (isMcpSource || isMcpTool || isMcpPrompt) &&
          (!IDENTITY.test(string(item.source_id, 512)) ||
            (!resource && item.reason_code === "unsupported_entry") ||
            ((isMcpTool || isMcpPrompt) &&
              item.parameter_schema === null &&
              item.reason_code === "unsupported_entry") ||
            (resource &&
              (!("source_id" in resource) ||
                resource.source_id !== item.source_id ||
                (isMcpTool &&
                  resource.kind === "mcp_tool" &&
                  resource.operation_name !== id) ||
                (isMcpPrompt &&
                  (resource.kind !== "mcp_prompt" ||
                    resource.prompt_name !== item.prompt_name)))));
        const invalidOrdinaryResource =
          resource &&
          !isMcpSource &&
          !isMcpTool &&
          !isMcpPrompt &&
          (resource.kind !== item.kind ||
            !("id" in resource) ||
            resource.id !== id ||
            (!isCapability &&
              (!("source_kind" in resource) ||
                resource.source_kind !== item.source_kind)));
        if (
          (isSkill &&
            !resource &&
            (item.source_kind === "builtin" ||
              item.read_only_parameter_schema !== null)) ||
          (isCapability && !resource) ||
          invalidMcpResource ||
          (isMcpPrompt && !string(item.prompt_name, 512)) ||
          invalidOrdinaryResource
        )
          invalid();
        if (isMcpSource || isMcpTool || isMcpPrompt) {
          const remote = record(item.remote_version, ["status", "value", "evidence"]);
          if (remote.status !== "unknown" || remote.value !== null || remote.evidence !== null) invalid();
          if (resource && resource.kind !== item.kind) invalid();
        }
        const code = item.reason_code === null ? null : reason(item.reason_code);
        if (item.availability !== "unavailable") {
          const ready = (kind: ResourceRef["kind"], mode: "baseline" | "readonly_extension") => entrySupport.some((cell) =>
            cell.resource_kind === kind && cell.authorization_mode === mode
            && cell.adapter_id === (kind === "capability"
              ? "initial-capability"
              : kind === "mcp_prompt" ? "initial-mcp-prompt"
              : kind === "mcp_source" || kind === "mcp_tool"
                ? "initial-mcp-read"
                : "initial-agent-skill")
            && cell.adapter_version === 1 && cell.supported);
          const selectedPromptCount = selection.selectedRefs.filter((ref) => ref.kind === "mcp_prompt").length;
          const selectedAgentCount = selection.selectedRefs.filter((ref) => ref.kind === "agent").length;
          const selectedSkillCount = selection.selectedRefs.filter((ref) => ref.kind === "skill").length;
          const selectedCapabilityCount = selection.selectedRefs.filter((ref) => ref.kind === "capability").length;
          if (!resource || code !== null
            || selection.selectedRefs.some((ref) => !["agent", "skill", "capability", "mcp_source", "mcp_tool", "mcp_prompt"].includes(ref.kind))
            || selectedAgentCount > 1 || selectedSkillCount > 3 || selectedCapabilityCount > 1 || selectedPromptCount > 3) invalid();
          if (isSkill) {
            if (!ready("agent", "baseline")
              || selectedSkillCount >= 3
              || selection.selectedRefs.some((ref) => ref.kind === "mcp_source" || ref.kind === "mcp_tool" || ref.kind === "mcp_prompt")
              || selectedCapabilityCount > 0 && !ready("capability", "baseline")
              || selection.selectedRefs.some((ref) => ref.kind === "skill" && resource.kind === "skill" && ref.source_kind === resource.source_kind && ref.id === resource.id)
              || item.read_only_parameter_schema === null || !ready("skill", item.availability as "baseline" | "readonly_extension")) invalid();
          } else if (isCapability) {
            if (!ready("agent", "baseline") || !ready("capability", "baseline")
              || selection.selectedRefs.some((ref) => ref.kind === "mcp_source" || ref.kind === "mcp_tool" || ref.kind === "mcp_prompt")
              || selectedSkillCount > 0 && !ready("skill", "baseline") && !ready("skill", "readonly_extension")
              || selectedCapabilityCount >= 1) invalid();
          } else if (!isMcpSource && !isMcpTool && !isMcpPrompt) {
            if (!ready("agent", "baseline")) invalid();
            if (selectedPromptCount && (!ready("mcp_prompt", "baseline") || resource.kind !== "agent" || resource.source_kind !== "team"
              || selection.selectedRefs.some((ref) => ref.kind !== "agent" && ref.kind !== "mcp_prompt"))) invalid();
            if (selection.selectedRefs.some((ref) => ref.kind === "skill") && !ready("skill", "baseline") && !ready("skill", "readonly_extension")) invalid();
            if (selection.selectedRefs.some((ref) => ref.kind === "capability") && !ready("capability", "baseline")) invalid();
          } else if (isMcpPrompt) {
            if (!ready("agent", "baseline") || !ready("mcp_prompt", "baseline") || selectedPromptCount >= 3
              || selection.selectedRefs.some((ref) => ref.kind !== "agent" && ref.kind !== "mcp_prompt" || ref.kind === "agent" && ref.source_kind !== "team")
              || selection.selectedRefs.some((ref) => resourceRefIdentity(ref) === resourceRefIdentity(resource))) invalid();
          } else {
            const selectedSources = selection.selectedRefs.filter((ref) => ref.kind === "mcp_source").length;
            const selectedTools = selection.selectedRefs.filter((ref) => ref.kind === "mcp_tool").length;
            const sourceIds = new Set(selection.selectedRefs.flatMap((ref) =>
              ref.kind === "mcp_source" || ref.kind === "mcp_tool" ? [ref.source_id] : []));
            if (resource.kind === "mcp_source" || resource.kind === "mcp_tool") sourceIds.add(resource.source_id);
            if (!ready("agent", "baseline")
              || !ready(item.kind as "mcp_source" | "mcp_tool", "baseline")
              || selectedPromptCount > 0
              || selectedSkillCount > 0
              || selectedCapabilityCount > 0
              || isMcpSource && selectedSources >= 3
              || isMcpTool && selectedTools >= 3
              || sourceIds.size > 3
              || selection.selectedRefs.length >= 10
              || selection.selectedRefs.some((ref) => resourceRefIdentity(ref) === resourceRefIdentity(resource))) invalid();
          }
        } else if (code === null) invalid();
        if (!resource && code === "unsupported_entry") invalid();
        if (ownAgent && (item.created_by_me !== true || item.source_kind !== "team")) invalid();
        return {
          kind: item.kind,
          source_kind: item.source_kind,
          ...(ownAgent ? { created_by_me: true } : {}),
          ...(isSkill
            ? {
                read_only_parameter_schema:
                  item.read_only_parameter_schema === null
                    ? null
                    : new ReferenceParameterSchema(
                        item.read_only_parameter_schema,
                      ).snapshot(),
              }
            : isCapability
              ? {
                  parameter_schema: new ReferenceParameterSchema(
                    item.parameter_schema,
                  ).snapshot(),
                }
              : isMcpTool
                ? {
                    source_id: item.source_id,
                    parameter_schema: item.parameter_schema === null
                      ? null
                      : new ReferenceParameterSchema(item.parameter_schema).snapshot(),
                    remote_version: { status: "unknown", value: null, evidence: null },
                  }
                : isMcpPrompt
                  ? {
                      source_id: item.source_id,
                      prompt_name: item.prompt_name,
                      parameter_schema: item.parameter_schema === null
                        ? null
                        : new ReferenceParameterSchema(item.parameter_schema).snapshot(),
                      remote_version: { status: "unknown", value: null, evidence: null },
                    }
                : isMcpSource
                  ? {
                      source_id: item.source_id,
                      remote_version: { status: "unknown", value: null, evidence: null },
                    }
              : {}),
          id,
          label: string(item.label, 256),
          summary: string(item.summary, 1024, true),
          resource,
          availability: item.availability,
          reason_code: code,
        } as ReferenceCatalogItem;
      });
      return {
        id: expectedGroups[index],
        items,
        error: group.error === null ? null : reason(group.error),
      };
    });
    const cursor =
      data.next_cursor === null ? null : string(data.next_cursor, 2048);
    const itemCount = groups.reduce(
      (count, group) => count + group.items.length,
      0,
    );
    if (itemCount > 20 || (cursor && itemCount !== 20)) invalid();
    if (selection.exactResource && (cursor || itemCount > 1 || groups.some((group) => group.items.some((item) =>
      !item.resource || resourceRefIdentity(item.resource) !== resourceRefIdentity(parseResourceReference(selection.exactResource)),
    )))) invalid();
    return {
      catalog_generation: generation,
      input_schema_versions: [1],
      entry_support: entrySupport,
      groups,
      next_cursor: cursor,
    };
  } catch {
    return invalid();
  }
}

export async function getReferenceCatalog(
  scope: ComposerPersistenceScope,
  selection: ReferenceCatalogSelection,
  signal: AbortSignal,
  cursor: string | null = null,
): Promise<ReferenceCatalogResponse> {
  if (
    !scope.userId ||
    !scope.teamId ||
    !scope.authSessionGeneration ||
    !IDENTITY.test(scope.workspaceId) ||
    !["@", "/"].includes(selection.trigger) ||
    !MODES.includes(selection.entryMode) ||
    [...selection.query].length > 128 ||
    selection.selectedRefs.length > 10
    || selection.exactResource && (!["skill", "capability", "mcp_tool", "mcp_prompt"].includes(selection.exactResource.kind) || selection.trigger !== "/" || selection.query !== "" || cursor !== null)
  ) {
    throw new ApiError(422, "Invalid reference catalog request", {
      code: "reference_input_invalid",
    });
  }
  const snapshot = {
    ...selection,
    selectedRefs: selection.selectedRefs.map(parseResourceReference),
    ...(selection.exactResource ? { exactResource: parseResourceReference(selection.exactResource) as ParameterResourceRef } : {}),
  };
  const body = JSON.stringify({
    workspace_id: scope.workspaceId,
    session_id: scope.requestedSessionId,
    trigger: snapshot.trigger,
    query: snapshot.query,
    cursor,
    selected_refs: snapshot.selectedRefs,
    entry_mode: snapshot.entryMode,
    submission_protocol: "chat-references.v1",
    ...(snapshot.exactResource ? { exact_resource: snapshot.exactResource } : {}),
  });
  if (new TextEncoder().encode(body).byteLength > 32 * 1024)
    throw new ApiError(413, "Reference catalog request is too large", {
      code: "reference_request_too_large",
    });
  return parseReferenceCatalog(
    await apiJson<unknown>("/api/v1/chat/references/catalog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body,
      signal,
    }),
    snapshot,
  );
}

/** Resolves current, visible metadata for the saved resource revision. Missing,
 * disabled or changed resources preserve the draft; no parameter fallback. */
export async function getReferenceParameterSchema(
  scope: ComposerPersistenceScope,
  resource: ParameterResourceRef,
  entryMode: ReferenceEntryMode,
  signal: AbortSignal,
): Promise<ParameterSchemaNode> {
  const result = await getReferenceCatalog(scope, {
    trigger: "/", query: "", entryMode, selectedRefs: [resource], exactResource: resource,
  }, signal);
  const item = result.groups.flatMap((group) => group.items)[0];
  const schema = item?.kind === "skill" ? item.read_only_parameter_schema
    : item && "parameter_schema" in item ? item.parameter_schema : null;
  if (!item || !schema || item.reason_code !== "unsupported_entry" && item.reason_code !== null) {
    throw new ApiError(409, "Resource parameter metadata is unavailable", { code: "reference_parameter_metadata_unavailable" });
  }
  return schema;
}
