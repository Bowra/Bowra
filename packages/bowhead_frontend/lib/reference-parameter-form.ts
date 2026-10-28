/** Local, uncommitted field edits. Missing values remain missing; only visible
 * scalar defaults are offered, and the caller must explicitly confirm them. */
import type { JsonValue } from "./composer-input-document";
import {
  ReferenceParameterSchema,
  type ParameterSchemaNode,
} from "./reference-parameter-schema";

export type ParameterCell =
  | { mode: "unset" }
  | { mode: "value"; value: JsonValue; defaulted?: boolean }
  | { mode: "number"; text: string };

export function parameterBaseType(schema: ParameterSchemaNode) {
  return Array.isArray(schema.type)
    ? schema.type.find((type) => type !== "null")!
    : schema.type;
}
export function isSimpleParameter(schema: ParameterSchemaNode): boolean {
  return !["object", "array"].includes(parameterBaseType(schema));
}

export function initialParameterCells(
  schema: ParameterSchemaNode,
  values: Record<string, JsonValue>,
): Map<string, ParameterCell> {
  const rootDefault = schema.default as Record<string, JsonValue> | undefined;
  return new Map<string, ParameterCell>(
    Object.entries(schema.properties!).map(([name, field]) => {
      if (Object.hasOwn(values, name))
        return [name, { mode: "value", value: values[name] }];
      if (isSimpleParameter(field)) {
        if (rootDefault && Object.hasOwn(rootDefault, name))
          return [
            name,
            { mode: "value", value: rootDefault[name], defaulted: true },
          ];
        if (Object.hasOwn(field, "default"))
          return [
            name,
            { mode: "value", value: field.default!, defaulted: true },
          ];
      }
      return [name, { mode: "unset" }];
    }),
  );
}

export function inspectParameterCells(
  reader: ReferenceParameterSchema,
  original: Record<string, JsonValue>,
  cells: Map<string, ParameterCell>,
) {
  const values = new Map(Object.entries(original));
  const invalidNumbers = new Set<string>();
  for (const [name, cell] of cells) {
    if (cell.mode === "unset") values.delete(name);
    else if (cell.mode === "value") values.set(name, cell.value);
    else if (cell.text === "") values.delete(name);
    else if (
      /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(cell.text) &&
      Number.isFinite(Number(cell.text)) &&
      Math.abs(Number(cell.text)) <= Number.MAX_SAFE_INTEGER
    )
      values.set(name, Number(cell.text));
    else invalidNumbers.add(name);
  }
  const inspection = reader.inspect(Object.fromEntries(values));
  return {
    ...inspection,
    invalidNumbers,
    // This permits a draft with missing values, never runtime execution. An
    // incomplete diagnostic list cannot prove that only required values are missing.
    canApply:
      inspection.values !== null &&
      !inspection.truncated &&
      invalidNumbers.size === 0 &&
      inspection.issues.every((issue) => issue.code === "required"),
  };
}
