/** Value-free presentation metadata. This never initializes drafts or validates saves. */
export interface FieldSchema {
  type?: string | string[];
  description?: string;
  default?: unknown;
  properties?: Record<string, FieldSchema>;
  items?: FieldSchema;
  additionalProperties?: boolean | FieldSchema;
  anyOf?: FieldSchema[];
  oneOf?: FieldSchema[];
  allOf?: FieldSchema[];
  enum?: unknown[];
  const?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  minLength?: number;
  maxLength?: number;
  "x-bowhead-field"?: {
    id: string;
    text_revision: string;
    unit?: string;
    decimal_minimum?: number;
    decimal_maximum?: number;
    decimal_places?: number;
  };
}
export interface FieldContract {
  contract_version: 1;
  domain: string;
  contract_revision: string;
  schema: FieldSchema;
}

/** Reject a malformed unit as a whole. Unknown future annotations remain harmless data. */
export function parseFieldContract(value: unknown): FieldContract | undefined {
  if (!value || typeof value !== "object") return;
  const c = value as FieldContract;
  if (c.contract_version !== 1 || typeof c.domain !== "string" || typeof c.contract_revision !== "string" || !c.schema?.properties) return;
  let count = 0;
  function valid(node: FieldSchema): boolean {
    if (!node || typeof node !== "object" || ++count > 10000) return false;
    for (const key of ["minimum", "maximum", "exclusiveMinimum", "minLength", "maxLength"] as const)
      if (node[key] !== undefined && (typeof node[key] !== "number" || !Number.isFinite(node[key]))) return false;
    if (node.minimum !== undefined && node.maximum !== undefined && node.minimum > node.maximum) return false;
    const metadata = node["x-bowhead-field"];
    if (
      metadata &&
      (typeof metadata.id !== "string" ||
        !metadata.id.startsWith(`${c.domain}.`) ||
        typeof metadata.text_revision !== "string" ||
        typeof node.description !== "string")
    )
      return false;
    const children = [
      ...Object.values(node.properties ?? {}),
      ...(node.anyOf ?? []),
      ...(node.oneOf ?? []),
      ...(node.allOf ?? []),
      ...(node.items ? [node.items] : []),
      ...(typeof node.additionalProperties === "object" ? [node.additionalProperties] : []),
    ];
    return children.every(valid);
  }
  try {
    return valid(c.schema) ? c : undefined;
  } catch {
    return;
  }
}

export function indexContract(contract: FieldContract): Record<string, FieldSchema> {
  const fields: Record<string, FieldSchema> = {};
  function visit(node: FieldSchema) {
    const metadata = node["x-bowhead-field"];
    if (metadata) fields[metadata.id] = node;
    Object.values(node.properties ?? {}).forEach(visit);
    [...(node.anyOf ?? []), ...(node.oneOf ?? []), ...(node.allOf ?? [])].forEach(visit);
    if (node.items) visit(node.items);
    if (typeof node.additionalProperties === "object") visit(node.additionalProperties);
  }
  visit(contract.schema);
  return fields;
}

export function scalarFacts(node: FieldSchema): FieldSchema {
  const branch = node.anyOf?.find((item) => item.type !== "null") ?? node;
  return { ...branch, ...node, type: branch.type };
}
