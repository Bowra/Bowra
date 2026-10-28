import { describe, expect, it } from "vitest";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import {
  assertComposerTextDraftFormat,
  readComposerReferenceInput,
} from "./composer-draft-format";
import { ComposerPersistenceVersionError } from "./composer-local-persistence-db";

describe("reference draft compatibility reader", () => {
  const fixture = fixtures.valid.find(
    (entry) => entry.name === "exact_agent_and_parameterized_skill",
  )!;
  const envelope = () => ({
    schemaVersion: 2,
    message: fixture.display_text,
    input_document: structuredClone(fixture.normalized),
  });

  it("restores complete nodes and parameters without enabling a text writer", () => {
    const input = envelope();
    const restored = readComposerReferenceInput(input);
    expect(restored).toEqual(fixture.normalized);
    expect(restored).not.toBe(input.input_document);
    expect(() => assertComposerTextDraftFormat(input)).toThrow(
      ComposerPersistenceVersionError,
    );
  });

  it.each([
    (input: ReturnType<typeof envelope>) => ({ ...input, schemaVersion: 99 }),
    (input: ReturnType<typeof envelope>) => ({ ...input, schemaVersion: 1 }),
    (input: ReturnType<typeof envelope>) => ({
      ...input,
      message: "changed label",
    }),
    (input: ReturnType<typeof envelope>) => ({
      ...input,
      input_document: { ...input.input_document, schema_version: 99 },
    }),
    (input: ReturnType<typeof envelope>) => ({
      ...input,
      input_document: { schema_version: 1, nodes: [{ type: "unknown" }] },
    }),
  ])(
    "preserves unsupported or inconsistent input behind the version fence",
    (mutate) => {
      const input = mutate(envelope());
      const before = JSON.stringify(input);
      expect(() => readComposerReferenceInput(input)).toThrow(
        ComposerPersistenceVersionError,
      );
      expect(JSON.stringify(input)).toBe(before);
    },
  );

  it("keeps legacy slash and at-sign text literal", () => {
    expect(readComposerReferenceInput({ message: "@name /path" })).toBeNull();
  });
});
