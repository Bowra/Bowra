/** InputDocument v1. Shared contract fixtures live in contracts/chat-references.
 * Validation checks wire structure only; it never establishes permission to use
 * a resource. Keep the original pending recovery envelope separate from this
 * normalized representation.
 */

export type JsonValue =
  null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
type AgentResourceRef = {
  kind: "agent";
  id: string;
  version: string;
} & (
  | { source_kind: "team"; assignment_id: string; release_id: string }
  | { source_kind: "system"; assignment_id?: null; release_id?: null }
);
type McpBinding = {
  source_id: string;
  local_contract_revision: string;
  connection_binding_id: string;
  connection_subject_fingerprint: string;
};
export type ResourceRef =
  | AgentResourceRef
  | {
      kind: "skill";
      source_kind: "builtin" | "team" | "user";
      id: string;
      version: string;
    }
  | { kind: "capability"; id: string; version: number }
  | (McpBinding & { kind: "mcp_source" })
  | (McpBinding & {
      kind: "mcp_tool";
      operation_name: string;
      operation_contract_revision: string;
    })
  | (McpBinding & {
      kind: "mcp_prompt";
      prompt_name: string;
      prompt_contract_revision: string;
    });
export type ReferenceNode = {
  type: "reference";
  node_id: string;
  resource: ResourceRef;
  label: string;
  parameters: { [key: string]: JsonValue };
};
export type InputNode = { type: "text"; text: string } | ReferenceNode;
export type InputDocument = { schema_version: 1; nodes: InputNode[] };

const MAX_DOCUMENT_BYTES = 256 * 1024;
const MAX_DOCUMENT_NODES = 512;
const IDENTITY = /^[A-Za-z0-9_.:-]+$/;
const HASH = /^sha256:[0-9a-f]{64}$/;
const NODE_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function fail(code: string): never {
  throw new Error(code);
}

function object(value: unknown): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    ![Object.prototype, null].includes(Object.getPrototypeOf(value))
  ) {
    fail("reference_json_invalid_object");
  }
  return value as Record<string, unknown>;
}

function fields(
  value: Record<string, unknown>,
  required: string[],
  optional: string[] = [],
): void {
  if (
    required.some((key) => !Object.hasOwn(value, key)) ||
    Object.keys(value).some(
      (key) => !required.includes(key) && !optional.includes(key),
    )
  ) {
    fail("reference_invalid_fields");
  }
}

function string(
  value: unknown,
  max: number,
  pattern?: RegExp,
): asserts value is string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    [...value].length > max ||
    (pattern && pattern.exec(value)?.[0] !== value)
  ) {
    fail("reference_invalid_string");
  }
}

function boundedJson(
  value: unknown,
  maxDepth = 16,
  maxBytes = MAX_DOCUMENT_BYTES,
): void {
  const pending: [unknown, number][] = [[value, 0]];
  let count = 0;
  let stringBytes = 0;
  while (pending.length) {
    const [item, depth] = pending.pop()!;
    if (++count > 16384) fail("reference_json_too_many_values");
    if (item !== null && typeof item === "object") {
      if (depth >= maxDepth) fail("reference_json_too_deep");
      if (Array.isArray(item)) {
        if (item.length > 512) fail("reference_json_array_too_large");
        // Sparse arrays/undefined are not JSON values.
        for (let i = 0; i < item.length; i++)
          pending.push([item[i], depth + 1]);
      } else {
        const values = object(item);
        if (Object.keys(values).length > 128)
          fail("reference_json_invalid_object");
        for (const [key, child] of Object.entries(values))
          pending.push([key, depth + 1], [child, depth + 1]);
      }
    } else if (typeof item === "string") {
      for (const char of item) {
        const cp = char.codePointAt(0)!;
        if (cp >= 0xd800 && cp <= 0xdfff)
          fail("reference_json_invalid_unicode");
      }
      const itemBytes = new TextEncoder().encode(item).length;
      if (itemBytes > maxBytes)
        fail("reference_json_string_too_large");
      stringBytes += itemBytes;
      if (stringBytes > maxBytes) fail("reference_json_too_large");
    } else if (typeof item === "number") {
      if (!Number.isFinite(item) || Math.abs(item) > Number.MAX_SAFE_INTEGER)
        fail("reference_json_unsafe_number");
    } else if (item !== null && typeof item !== "boolean") {
      fail("reference_json_invalid_value");
    }
  }
  if (new TextEncoder().encode(JSON.stringify(value)).length > maxBytes)
    fail("reference_json_too_large");
}

function parseResource(raw: unknown): ResourceRef {
  const r = object(raw);
  if (r.kind === "agent" || r.kind === "skill" || r.kind === "capability") {
    fields(
      r,
      r.kind === "capability"
        ? ["kind", "id", "version"]
        : ["kind", "source_kind", "id", "version"],
      r.kind === "agent" ? ["assignment_id", "release_id"] : [],
    );
    string(r.id, 512, IDENTITY);
    if (r.kind === "capability") {
      if (
        typeof r.version !== "number" ||
        !Number.isSafeInteger(r.version) ||
        r.version < 1
      )
        fail("reference_invalid_version");
    } else {
      string(r.version, 512, r.kind === "skill" ? HASH : IDENTITY);
      if (r.kind === "skill") {
        if (!["builtin", "team", "user"].includes(r.source_kind as string))
          fail("reference_invalid_source_kind");
      } else if (r.source_kind === "team") {
        string(r.assignment_id, 512, IDENTITY);
        string(r.release_id, 512, IDENTITY);
      } else if (r.source_kind === "system") {
        if (r.assignment_id != null || r.release_id != null)
          fail("reference_system_agent_has_team_fields");
      } else fail("reference_invalid_source_kind");
    }
  } else if (
    r.kind === "mcp_source" ||
    r.kind === "mcp_tool" ||
    r.kind === "mcp_prompt"
  ) {
    const common = [
      "kind",
      "source_id",
      "local_contract_revision",
      "connection_binding_id",
      "connection_subject_fingerprint",
    ];
    const operation =
      r.kind === "mcp_tool"
        ? ["operation_name", "operation_contract_revision"]
        : r.kind === "mcp_prompt"
          ? ["prompt_name", "prompt_contract_revision"]
          : [];
    fields(r, [...common, ...operation]);
    string(r.source_id, 512, IDENTITY);
    string(r.connection_binding_id, 512, IDENTITY);
    string(r.local_contract_revision, 71, HASH);
    string(r.connection_subject_fingerprint, 71, HASH);
    if (operation.length) {
      string(r[operation[0]], 512);
      string(r[operation[1]], 71, HASH);
    }
  } else fail("reference_invalid_kind");
  // Detach validated JSON, never retain caller-owned parameters or objects.
  return JSON.parse(
    JSON.stringify(
      Object.fromEntries(
        Object.entries(r).filter(([, value]) => value !== null),
      ),
    ),
  ) as ResourceRef;
}

/** Parse a catalog identity using the same contract as a document node. */
export function parseResourceReference(raw: unknown): ResourceRef {
  boundedJson(raw);
  return parseResource(raw);
}

export function parseInputDocument(raw: unknown): InputDocument {
  boundedJson(raw);
  const doc = object(raw);
  fields(doc, ["schema_version", "nodes"]);
  if (
    doc.schema_version !== 1 ||
    !Array.isArray(doc.nodes) ||
    doc.nodes.length > MAX_DOCUMENT_NODES
  )
    fail("reference_invalid_document");
  const nodes: InputNode[] = [];
  const nodeIds = new Set<string>();
  const resources = new Set<string>();
  const bindings = new Map<string, string>();
  const counts = new Map<string, number>();
  let referenceCount = 0;
  for (const rawNode of doc.nodes) {
    const n = object(rawNode);
    if (n.type === "text") {
      fields(n, ["type", "text"]);
      if (typeof n.text !== "string") fail("reference_invalid_text");
      const previous = nodes.at(-1);
      if (previous?.type === "text") previous.text += n.text;
      else nodes.push({ type: "text", text: n.text });
      continue;
    }
    if (n.type !== "reference") fail("reference_invalid_node");
    fields(n, ["type", "node_id", "resource", "label", "parameters"]);
    string(n.node_id, 36, NODE_ID);
    string(n.label, 256);
    object(n.parameters);
    boundedJson(n.parameters, 8, 16 * 1024);
    const resource = parseResource(n.resource);
    if (nodeIds.has(n.node_id)) fail("reference_duplicate_node_id");
    nodeIds.add(n.node_id);
    if (++referenceCount > 10) fail("reference_count_exceeded");
    counts.set(resource.kind, (counts.get(resource.kind) ?? 0) + 1);
    let identity: string;
    if ("source_id" in resource) {
      const binding = JSON.stringify([
        resource.local_contract_revision,
        resource.connection_binding_id,
        resource.connection_subject_fingerprint,
      ]);
      const previous = bindings.get(resource.source_id);
      if (previous !== undefined && previous !== binding)
        fail("reference_source_binding_conflict");
      bindings.set(resource.source_id, binding);
      identity = JSON.stringify([
        resource.kind,
        resource.source_id,
        resource.kind === "mcp_tool"
          ? resource.operation_name
          : resource.kind === "mcp_prompt"
            ? resource.prompt_name
            : "",
      ]);
    } else {
      identity = JSON.stringify([
        resource.kind,
        "source_kind" in resource ? resource.source_kind : "",
        resource.id,
      ]);
    }
    if (resources.has(identity)) fail("reference_duplicate_resource");
    resources.add(identity);
    nodes.push({
      type: "reference",
      node_id: n.node_id,
      label: n.label,
      resource,
      parameters: JSON.parse(JSON.stringify(n.parameters)),
    });
  }
  if (
    (counts.get("agent") ?? 0) > 1 ||
    (counts.get("skill") ?? 0) > 3 ||
    (counts.get("capability") ?? 0) > 1
  )
    fail("reference_kind_quota_exceeded");
  if (
    bindings.size > 3 ||
    (counts.get("mcp_tool") ?? 0) + (counts.get("mcp_prompt") ?? 0) > 3
  )
    fail("reference_mcp_quota_exceeded");
  // Normalize after merging, including CRLF pairs split across text nodes.
  for (const node of nodes) {
    if (node.type === "text") node.text = node.text.replace(/\r\n?/g, "\n");
  }
  return { schema_version: 1, nodes };
}

/** Untrusted display snapshot for wire matching/copying, never an LLM prompt. */
export function inputDocumentDisplayText(doc: InputDocument): string {
  return doc.nodes
    .map((node) =>
      node.type === "text"
        ? node.text
        : `${node.resource.kind === "agent" ? "@" : "/"}${node.label}`,
    )
    .join("");
}

export function inputDocumentTextContent(doc: InputDocument): string {
  return doc.nodes
    .map((node) => (node.type === "text" ? node.text : ""))
    .join("");
}

export function validateInputDocumentTask(
  doc: InputDocument,
  hasAttachments: boolean,
): void {
  const text = inputDocumentTextContent(doc).trimStart();
  if (!text.trim() && !hasAttachments) fail("reference_task_content_required");
  if (
    doc.nodes.some((node) => node.type === "reference") &&
    text.startsWith("/remember")
  )
    fail("reference_command_conflict");
}

/** Contract comparison only. The server owns the HMAC and decimal encoding. */
export function inputDocumentSemanticProjection(doc: InputDocument): object {
  return {
    schema_version: 1,
    nodes: doc.nodes
      .filter((node) => node.type !== "text" || node.text !== "")
      .map((node) =>
        node.type === "text"
          ? { ...node }
          : {
              type: "reference",
              resource: Object.fromEntries(
                Object.entries(node.resource).filter(
                  ([, value]) => value != null,
                ),
              ),
              parameters: JSON.parse(JSON.stringify(node.parameters)),
            },
      ),
  };
}

export function inputDocumentFromText(text: string): InputDocument {
  return parseInputDocument({
    schema_version: 1,
    nodes: [{ type: "text", text }],
  });
}
