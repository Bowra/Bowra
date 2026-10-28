import { describe, expect, it } from "vitest";
import { ReferenceParameterSchema } from "./reference-parameter-schema";
import {
  initialParameterCells,
  inspectParameterCells,
} from "./reference-parameter-form";
import type { JsonValue } from "./composer-input-document";

const object = (properties: object, extra: object = {}) => ({
  type: "object",
  additionalProperties: false,
  properties,
  ...extra,
});
describe("parameter edits preserve the user's explicit choices", () => {
  it("offers root defaults before field defaults, but only for scalar fields absent from the source", () => {
    const reader = new ReferenceParameterSchema(
      object(
        {
          x: { type: "string", default: "field" },
          y: { type: "integer", default: 2 },
          z: object({ n: { type: "integer" } }, { default: { n: 8 } }),
        },
        { default: { x: "root", z: { n: 9 } } },
      ),
    );
    const cells = initialParameterCells(reader.snapshot(), { y: 0 });
    expect(inspectParameterCells(reader, { y: 0 }, cells).values).toEqual({
      x: "root",
      y: 0,
    });
    cells.set("x", { mode: "unset" });
    expect(inspectParameterCells(reader, { y: 0 }, cells).values).toEqual({
      y: 0,
    });
  });
  it("preserves prototype-like business keys and nested originals without object merging", () => {
    const reader = new ReferenceParameterSchema(
      JSON.parse(
        '{"type":"object","properties":{"__proto__":{"type":"string","default":"safe"},"constructor":{"type":"string"}},"additionalProperties":false}',
      ),
    );
    const values = JSON.parse('{"constructor":"value"}');
    const cells = initialParameterCells(reader.snapshot(), values);
    const result = inspectParameterCells(reader, values, cells);
    expect(result.canApply).toBe(true);
    expect(JSON.stringify(result.values)).toBe(
      '{"constructor":"value","__proto__":"safe"}',
    );
    expect(Object.getPrototypeOf(result.values)).toBe(Object.prototype);
  });
  it("never accepts an error list truncated after missing fields before an unknown parameter", () => {
    const properties = Object.fromEntries(
      Array.from({ length: 64 }, (_, i) => [`field${i}`, { type: "string" }]),
    );
    const reader = new ReferenceParameterSchema(
      object(properties, { required: Object.keys(properties) }),
    );
    const values = { unknown: "kept" };
    const result = inspectParameterCells(
      reader,
      values,
      initialParameterCells(reader.snapshot(), values),
    );
    expect(result.issues).toHaveLength(64);
    expect(result.issues.every((issue) => issue.code === "required")).toBe(
      true,
    );
    expect(result.truncated).toBe(true);
    expect(result.canApply).toBe(false);
    expect(result.values).toEqual(values);
  });
  it("lets a user defer invalid structured data explicitly, without inventing a replacement object", () => {
    const reader = new ReferenceParameterSchema(
      object(
        { detail: object({ x: { type: "string" } }, { required: ["x"] }) },
        { required: ["detail"] },
      ),
    );
    const values: Record<string, JsonValue> = { detail: { x: 3 } };
    const cells = initialParameterCells(reader.snapshot(), values);
    expect(inspectParameterCells(reader, values, cells).canApply).toBe(false);
    cells.set("detail", { mode: "unset" });
    expect(inspectParameterCells(reader, values, cells)).toMatchObject({
      values: {},
      canApply: true,
    });
    expect(values).toEqual({ detail: { x: 3 } });
  });
});
