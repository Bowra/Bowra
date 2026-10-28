import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
// Read wire JSON. Bundler-generated object literals change __proto__ semantics.
const fixtures = JSON.parse(
  readFileSync(
    resolve(
      __dirname,
      "../../../contracts/skill-readonly-entry/parameter-validation-v1.json",
    ),
    "utf8",
  ),
) as {
  valid_schemas: {
    name: string;
    schema: unknown;
    valid_values: unknown[];
    invalid_values: unknown[];
  }[];
  invalid_schemas: { name: string; schema: unknown }[];
};
import declaration from "../../../contracts/skill-readonly-entry/v1.json";
import { ReferenceParameterSchema } from "./reference-parameter-schema";

describe("read-only parameter contract shared with Python", () => {
  for (const fixture of fixtures.valid_schemas) {
    it(fixture.name, () => {
      const schema = new ReferenceParameterSchema(fixture.schema);
      for (const values of fixture.valid_values)
        expect(schema.validate(values)).toEqual(values);
      for (const values of fixture.invalid_values)
        expect(() => schema.validate(values)).toThrow(
          "reference_parameter_values_invalid",
        );
    });
  }
  for (const fixture of fixtures.invalid_schemas) {
    it(`rejects ${fixture.name}`, () => {
      expect(() => new ReferenceParameterSchema(fixture.schema)).toThrow(
        "reference_parameter_schema_invalid",
      );
    });
  }
});

it("keeps schema and defaults detached, and never fills absent parameters", () => {
  const input = structuredClone(declaration.parameter_schema);
  const schema = new ReferenceParameterSchema(input);
  input.properties.limit.maximum = 100;
  const snapshot = schema.snapshot();
  snapshot.properties!.limit.maximum = 200;
  expect(schema.snapshot().properties!.limit.maximum).toBe(10);
  expect(() => schema.validate({ period: "2026-09-13", limit: 11 })).toThrow();
  expect(schema.validate({ period: "2026-09-13" })).toEqual({
    period: "2026-09-13",
  });
  const values = {
    period: "2026-09-13",
    adjustments: [{ label: "net", amount: 2 }],
  };
  const result = schema.validate(values);
  expect(result).not.toBe(values);
  expect(result.adjustments).not.toBe(values.adjustments);
  (result.adjustments as { label: string; amount: number }[])[0].amount = 99;
  expect(values.adjustments[0].amount).toBe(2);
});

it("reports only bounded known field paths and finite reasons", () => {
  const schema = new ReferenceParameterSchema(declaration.parameter_schema);
  const result = schema.inspect({
    "private-field-name": "private-value",
    adjustments: [{ label: "private-label" }],
  });
  expect(result.issues).toEqual([
    { path: ["period"], code: "required" },
    { path: [], code: "unexpected" },
    { path: ["adjustments", 0, "amount"], code: "required" },
  ]);
  expect(JSON.stringify(result.issues)).not.toContain("private");
});

function objectSchema(
  properties: Record<string, unknown>,
  required: string[] = [],
) {
  return { type: "object", properties, required, additionalProperties: false };
}

it.each([
  undefined,
  NaN,
  Infinity,
  -Infinity,
  2 ** 53,
  "\ud800",
  new Date(),
  () => "x",
  Symbol("x"),
])("rejects non-JSON parameter values without coercion: %s", (value) => {
  const schema = new ReferenceParameterSchema(
    objectSchema({ value: { type: "string" } }),
  );
  const result = schema.inspect({ value });
  expect(result.values).toBeNull();
  expect(result.issues).toEqual([{ path: [], code: "invalid_json" }]);
});

it("never executes accessors or a toJSON method", () => {
  const schema = new ReferenceParameterSchema(
    objectSchema({ value: { type: "string" } }),
  );
  let calls = 0;
  for (const value of [
    {
      get value() {
        calls++;
        return "private";
      },
    },
    {
      value: "private",
      toJSON() {
        calls++;
        return { value: "public" };
      },
    },
    Object.defineProperty({}, "value", { value: "hidden", enumerable: false }),
    { value: "private", [Symbol("ignored")]: "hidden" },
  ])
    expect(schema.inspect(value).issues).toEqual([
      { path: [], code: "invalid_json" },
    ]);
  expect(calls).toBe(0);
});

it("rejects cyclic, deep, sparse and oversized arrays before schema traversal", () => {
  const schema = new ReferenceParameterSchema(
    objectSchema({ value: { type: "array", items: { type: "null" } } }),
  );
  const cyclic: Record<string, unknown> = {};
  cyclic.self = cyclic;
  let deep: unknown = null;
  for (let index = 0; index < 9; index++) deep = { child: deep };
  for (const raw of [
    cyclic,
    deep,
    { value: Array(1) },
    { value: Array(513).fill(null) },
  ])
    expect(schema.inspect(raw).issues).toEqual([
      { path: [], code: "invalid_json" },
    ]);
  expect(schema.validate({ value: Array(512).fill(null) }).value).toHaveLength(
    512,
  );
});

it("counts serialized UTF-8 bytes including structure, without trimming values", () => {
  const schema = new ReferenceParameterSchema(
    objectSchema({ text: { type: "string" } }),
  );
  const exact = { text: "a".repeat(16373) }; // {"text":""} costs 11 bytes.
  expect(schema.validate(exact)).toEqual(exact);
  expect(() => schema.validate({ text: "a".repeat(16374) })).toThrow();
  expect(() => schema.validate({ text: "中".repeat(5500) })).toThrow();
  expect(schema.validate({ text: " \n\t" })).toEqual({ text: " \n\t" });
});

it("bounds schema width, depth, node count and bytes with otherwise valid inputs", () => {
  const fields = (length: number) =>
    Object.fromEntries(
      Array.from({ length }, (_, index) => [`f${index}`, { type: "boolean" }]),
    );
  const exactNodes = objectSchema({
    one: objectSchema(fields(63)),
    two: objectSchema(fields(62)),
  });
  expect(new ReferenceParameterSchema(exactNodes).validate({})).toEqual({});
  expect(
    () =>
      new ReferenceParameterSchema(
        objectSchema({
          one: objectSchema(fields(63)),
          two: objectSchema(fields(63)),
        }),
      ),
  ).toThrow();
  expect(
    new ReferenceParameterSchema(objectSchema(fields(64))).validate({}),
  ).toEqual({});
  expect(
    () => new ReferenceParameterSchema(objectSchema(fields(65))),
  ).toThrow();
  const nested = (depth: number) => {
    let value: unknown = { type: "boolean" };
    for (let index = 0; index < depth; index++)
      value = objectSchema({ child: value });
    return value;
  };
  expect(new ReferenceParameterSchema(nested(6)).validate({})).toEqual({});
  expect(() => new ReferenceParameterSchema(nested(7))).toThrow();
  const described = (count: number) =>
    objectSchema(
      Object.fromEntries(
        Array.from({ length: count }, (_, index) => [
          `field${index}`,
          { type: "string", description: "a".repeat(2048) },
        ]),
      ),
    );
  expect(new ReferenceParameterSchema(described(7)).validate({})).toEqual({});
  expect(() => new ReferenceParameterSchema(described(8))).toThrow();
});

it("bounds schema display text and enum values using Unicode code points", () => {
  for (const [key, exact, excessive] of [
    ["title", "😀".repeat(256), "😀".repeat(257)],
    ["description", "x".repeat(2048), "x".repeat(2049)],
    [
      "enum",
      Array.from({ length: 64 }, (_, index) => `v${index}`),
      Array.from({ length: 65 }, (_, index) => `v${index}`),
    ],
  ] as const) {
    new ReferenceParameterSchema(
      objectSchema({ text: { type: "string", [key]: exact } }),
    );
    expect(
      () =>
        new ReferenceParameterSchema(
          objectSchema({ text: { type: "string", [key]: excessive } }),
        ),
    ).toThrow();
  }
  new ReferenceParameterSchema(
    objectSchema({ ["😀".repeat(128)]: { type: "string" } }),
  );
  expect(
    () =>
      new ReferenceParameterSchema(
        objectSchema({ ["😀".repeat(129)]: { type: "string" } }),
      ),
  ).toThrow();
});

it("marks a bounded error list as incomplete instead of hiding further failures", () => {
  const fields = Object.fromEntries(
    Array.from({ length: 64 }, (_, index) => [
      `field${index}`,
      { type: "boolean" },
    ]),
  );
  const schema = new ReferenceParameterSchema(
    objectSchema(fields, Object.keys(fields)),
  );
  const missing = schema.inspect({});
  expect(missing.issues).toHaveLength(64);
  expect(missing.truncated).toBe(false);
  const more = schema.inspect({ "private-unexpected-key": "private-value" });
  expect(more.issues).toHaveLength(64);
  expect(more.truncated).toBe(true);
  expect(JSON.stringify(more.issues)).not.toContain("private");
});

it("accepts null-prototype JSON dictionaries and returns a detached normal JSON copy", () => {
  const source = Object.assign(Object.create(null), { period: "2026-09-13" });
  const schema = new ReferenceParameterSchema(declaration.parameter_schema);
  const result = schema.validate(source);
  result.period = "2026-09-14";
  expect(source.period).toBe("2026-09-13");
  expect(Object.getPrototypeOf(result)).toBe(Object.prototype);
});
