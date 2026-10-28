/** Skill read_only_entry parameter_schema v1. This is a bounded UI reader and
 * validator, not authorization or runtime readiness. Keep it aligned with
 * bowhead_skill.readonly_entry and contracts/skill-readonly-entry.
 */
import type { JsonValue } from "./composer-input-document";

export type ParameterType =
  "object" | "array" | "string" | "integer" | "number" | "boolean" | "null";
export type ParameterSchemaNode = {
  type: ParameterType | [ParameterType, "null"] | ["null", ParameterType];
  title?: string;
  description?: string;
  enum?: JsonValue[];
  default?: JsonValue;
  properties?: Record<string, ParameterSchemaNode>;
  required?: string[];
  additionalProperties?: false;
  items?: ParameterSchemaNode;
  minItems?: number;
  maxItems?: number;
  minLength?: number;
  maxLength?: number;
  format?: "date";
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
};
export type ParameterIssue = {
  /** Known schema field path only; never an unknown parameter name or value. */
  path: (string | number)[];
  code:
    | "required"
    | "type"
    | "enum"
    | "format"
    | "range"
    | "length"
    | "unexpected"
    | "invalid_json";
};
export type ParameterInspection = {
  values: Record<string, JsonValue> | null;
  issues: ParameterIssue[];
  /** An incomplete list must never be used to infer that only required fields are missing. */
  truncated: boolean;
};

export class ReferenceParameterError extends Error {
  constructor(
    code:
      | "reference_parameter_schema_invalid"
      | "reference_parameter_json_invalid"
      | "reference_parameter_values_invalid",
  ) {
    super(code);
    this.name = "ReferenceParameterError";
  }
}
const encoder = new TextEncoder();
const TYPES: ParameterType[] = [
  "object",
  "array",
  "string",
  "integer",
  "number",
  "boolean",
  "null",
];
const MAX_BYTES = 16 * 1024;

function invalidSchema(): never {
  throw new ReferenceParameterError("reference_parameter_schema_invalid");
}
function record(value: unknown): value is Record<string, JsonValue> {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    [Object.prototype, null].includes(Object.getPrototypeOf(value))
  );
}
function characters(text: string): number {
  return [...text].length;
}

/** Copy JSON without calling getters/toJSON or keeping caller-owned objects. */
function copyJson(raw: unknown, maxDepth: number): JsonValue {
  let count = 0;
  let stringBytes = 0;
  function fail(): never {
    throw new ReferenceParameterError("reference_parameter_json_invalid");
  }
  function visit(value: unknown, depth: number): JsonValue {
    if (++count > 8192) fail();
    if (value === null || typeof value === "boolean") return value;
    if (typeof value === "number") {
      if (!Number.isFinite(value) || Math.abs(value) > Number.MAX_SAFE_INTEGER)
        fail();
      return value;
    }
    if (typeof value === "string") {
      for (const character of value) {
        const point = character.codePointAt(0)!;
        if (point >= 0xd800 && point <= 0xdfff) fail();
      }
      stringBytes += encoder.encode(value).length;
      if (stringBytes > MAX_BYTES) fail();
      return value;
    }
    if (depth >= maxDepth || value === undefined || typeof value !== "object")
      fail();
    if (Object.getOwnPropertySymbols(value).length) fail();
    const descriptors = Object.getOwnPropertyDescriptors(value);
    if (Array.isArray(value)) {
      if (
        Object.getPrototypeOf(value) !== Array.prototype ||
        value.length > 512 ||
        Object.keys(descriptors).length !== value.length + 1
      )
        fail();
      return Array.from({ length: value.length }, (_, index) => {
        const descriptor = descriptors[String(index)];
        if (!descriptor || !Object.hasOwn(descriptor, "value")) fail();
        return visit(descriptor.value, depth + 1);
      });
    }
    if (!record(value) || Object.keys(descriptors).length > 512) fail();
    return Object.fromEntries(
      Object.entries(descriptors).map(([key, descriptor]) => {
        if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value"))
          fail();
        visit(key, depth + 1);
        return [key, visit(descriptor.value, depth + 1)];
      }),
    );
  }
  const value = visit(raw, 0);
  if (encoder.encode(JSON.stringify(value)).length > MAX_BYTES) fail();
  return value;
}

function kind(node: ParameterSchemaNode): ParameterType {
  return Array.isArray(node.type)
    ? node.type.find((type) => type !== "null")!
    : node.type;
}
function equal(left: JsonValue, right: JsonValue): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right))
    return (
      left.length === right.length &&
      left.every((item, index) => equal(item, right[index]))
    );
  if (record(left) && record(right)) {
    const keys = Object.keys(left);
    return (
      keys.length === Object.keys(right).length &&
      keys.every(
        (key) => Object.hasOwn(right, key) && equal(left[key], right[key]),
      )
    );
  }
  return false;
}
function isDate(value: string): boolean {
  if (/^\d{4}-\d{2}-\d{2}$/.exec(value)?.[0] !== value) return false;
  const [year, month, day] = value.split("-").map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return (
    year > 0 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]
  );
}

function issuesFor(
  schema: ParameterSchemaNode,
  value: JsonValue,
): Pick<ParameterInspection, "issues" | "truncated"> {
  const issues: ParameterIssue[] = [];
  let truncated = false;
  function inspect(
    node: ParameterSchemaNode,
    current: JsonValue,
    path: (string | number)[],
  ): void {
    if (issues.length >= 64) {
      truncated = true;
      return;
    }
    const add = (code: ParameterIssue["code"], target = path) => {
      if (issues.length < 64) issues.push({ code, path: target });
      else truncated = true;
    };
    const type = kind(node);
    const nullable =
      node.type === "null" ||
      (Array.isArray(node.type) && node.type.includes("null"));
    const matches =
      current === null
        ? nullable
        : type === "object"
          ? record(current)
          : type === "array"
            ? Array.isArray(current)
            : type === "integer"
              ? typeof current === "number" && Number.isInteger(current)
              : type === "number"
                ? typeof current === "number"
                : typeof current === type;
    if (!matches) {
      add("type");
      return;
    }
    if (node.enum && !node.enum.some((candidate) => equal(candidate, current)))
      add("enum");
    if (current === null) return;
    if (record(current)) {
      const properties = node.properties!;
      for (const key of node.required ?? [])
        if (!Object.hasOwn(current, key)) add("required", [...path, key]);
      if (Object.keys(current).some((key) => !Object.hasOwn(properties, key)))
        add("unexpected");
      for (const [key, child] of Object.entries(properties))
        if (Object.hasOwn(current, key))
          inspect(child, current[key], [...path, key]);
    } else if (Array.isArray(current)) {
      if (
        (node.minItems !== undefined && current.length < node.minItems) ||
        (node.maxItems !== undefined && current.length > node.maxItems)
      )
        add("length");
      current.forEach((child, index) =>
        inspect(node.items!, child, [...path, index]),
      );
    } else if (typeof current === "string") {
      const length = characters(current);
      if (
        (node.minLength !== undefined && length < node.minLength) ||
        (node.maxLength !== undefined && length > node.maxLength)
      )
        add("length");
      if (node.format === "date" && !isDate(current)) add("format");
    } else if (typeof current === "number") {
      if (
        (node.minimum !== undefined && current < node.minimum) ||
        (node.maximum !== undefined && current > node.maximum) ||
        (node.exclusiveMinimum !== undefined &&
          current <= node.exclusiveMinimum) ||
        (node.exclusiveMaximum !== undefined &&
          current >= node.exclusiveMaximum)
      )
        add("range");
    }
  }
  inspect(schema, value, []);
  return { issues, truncated };
}

function parseSchema(raw: unknown): ParameterSchemaNode {
  let value: JsonValue;
  try {
    value = copyJson(raw, 16);
  } catch (error) {
    if (error instanceof ReferenceParameterError) invalidSchema();
    throw error;
  }
  if (!record(value) || value.type !== "object") invalidSchema();
  const pending: [JsonValue, number][] = [[value, 0]];
  const checked: ParameterSchemaNode[] = [];
  while (pending.length) {
    const [node, depth] = pending.pop()!;
    if (!record(node) || checked.length >= 128 || depth > 6) invalidSchema();
    const rawType = node.type;
    let type: JsonValue | undefined = rawType;
    if (Array.isArray(rawType)) {
      if (
        rawType.length !== 2 ||
        !rawType.includes("null") ||
        rawType.some((item) => typeof item !== "string")
      )
        invalidSchema();
      type = rawType.find((item) => item !== "null");
    }
    if (typeof type !== "string" || !TYPES.includes(type as ParameterType))
      invalidSchema();
    const allowed = new Set([
      "type",
      "title",
      "description",
      "enum",
      "default",
    ]);
    const permit = (...keys: string[]) =>
      keys.forEach((key) => allowed.add(key));
    if (type === "object") {
      permit("properties", "required", "additionalProperties");
      const properties = node.properties;
      if (
        node.additionalProperties !== false ||
        !record(properties) ||
        Object.keys(properties).length > 64
      )
        invalidSchema();
      if (
        Object.keys(properties).some(
          (key) => !key || characters(key) > 128 || /[\x00-\x1f]/.test(key),
        )
      )
        invalidSchema();
      const required = Object.hasOwn(node, "required") ? node.required : [];
      if (
        !Array.isArray(required) ||
        required.some(
          (key) => typeof key !== "string" || !Object.hasOwn(properties, key),
        ) ||
        new Set(required).size !== required.length
      )
        invalidSchema();
      pending.push(
        ...Object.values(properties).map((child): [JsonValue, number] => [
          child,
          depth + 1,
        ]),
      );
    } else if (type === "array") {
      permit("items", "minItems", "maxItems");
      if (!Object.hasOwn(node, "items")) invalidSchema();
      pending.push([node.items, depth + 1]);
    } else if (type === "string") {
      permit("minLength", "maxLength", "format");
      if (Object.hasOwn(node, "format") && node.format !== "date")
        invalidSchema();
    } else if (type === "number" || type === "integer")
      permit("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum");
    if (Object.keys(node).some((key) => !allowed.has(key))) invalidSchema();
    for (const [key, maximum] of [
      ["title", 256],
      ["description", 2048],
    ] as const)
      if (
        Object.hasOwn(node, key) &&
        (typeof node[key] !== "string" ||
          characters(node[key] as string) > maximum)
      )
        invalidSchema();
    for (const key of ["minItems", "maxItems", "minLength", "maxLength"])
      if (
        Object.hasOwn(node, key) &&
        (typeof node[key] !== "number" ||
          !Number.isInteger(node[key]) ||
          (node[key] as number) < 0)
      )
        invalidSchema();
    for (const key of [
      "minimum",
      "maximum",
      "exclusiveMinimum",
      "exclusiveMaximum",
    ])
      if (Object.hasOwn(node, key) && typeof node[key] !== "number")
        invalidSchema();
    if (
      Object.hasOwn(node, "enum") &&
      (!Array.isArray(node.enum) ||
        node.enum.length < 1 ||
        node.enum.length > 64)
    )
      invalidSchema();
    checked.push(node as ParameterSchemaNode);
  }
  // No annotation value is inspected until the entire schema has been checked.
  for (const node of checked) {
    if (
      Object.hasOwn(node, "default") &&
      issuesFor(node, node.default!).issues.length
    )
      invalidSchema();
    if (
      node.enum?.some((candidate) => issuesFor(node, candidate).issues.length)
    )
      invalidSchema();
  }
  return value as ParameterSchemaNode;
}

/** Construct once per exact metadata version; callers cannot forge unchecked state. */
export class ReferenceParameterSchema {
  readonly #schema: ParameterSchemaNode;
  constructor(raw: unknown) {
    this.#schema = parseSchema(raw);
  }
  snapshot(): ParameterSchemaNode {
    return JSON.parse(JSON.stringify(this.#schema));
  }
  inspect(raw: unknown): ParameterInspection {
    let values: JsonValue;
    try {
      values = copyJson(raw, 8);
    } catch (error) {
      if (!(error instanceof ReferenceParameterError)) throw error;
      return {
        values: null,
        issues: [{ path: [], code: "invalid_json" }],
        truncated: false,
      };
    }
    return {
      values: record(values) ? values : null,
      ...issuesFor(this.#schema, values),
    };
  }
  /** Strict complete validation. Missing fields are issues, never implicit defaults.
   * inspect() does not decide task acceptance or automatically execute clarification.
   */
  validate(raw: unknown): Record<string, JsonValue> {
    const result = this.inspect(raw);
    if (result.issues.length || !result.values)
      throw new ReferenceParameterError("reference_parameter_values_invalid");
    return result.values;
  }
}
