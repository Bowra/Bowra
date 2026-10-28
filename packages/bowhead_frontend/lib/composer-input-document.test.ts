import { describe, expect, it } from "vitest";
import fixtures from "../../../contracts/chat-references/input-document-v1.json";
import {
  inputDocumentDisplayText,
  inputDocumentFromText,
  inputDocumentSemanticProjection,
  inputDocumentTextContent,
  parseInputDocument,
  validateInputDocumentTask,
} from "./composer-input-document";

describe("InputDocument v1 shared wire contract", () => {
  for (const fixture of fixtures.valid) {
    it(fixture.name, () => {
      const actual = parseInputDocument(fixture.input);
      expect(actual).toEqual(fixture.normalized);
      expect(inputDocumentDisplayText(actual)).toBe(fixture.display_text);
      expect(inputDocumentTextContent(actual)).toBe(fixture.text_content);
      expect(inputDocumentSemanticProjection(actual)).toEqual(fixture.semantic);
      expect(parseInputDocument(JSON.parse(JSON.stringify(actual)))).toEqual(
        actual,
      );
    });
  }
  for (const fixture of fixtures.invalid) {
    it(`rejects ${fixture.name}`, () => {
      expect(() => parseInputDocument(fixture.input)).toThrow();
    });
  }
});

it("keeps pending wire intact while normalizing a detached document", () => {
  const pending = structuredClone(fixtures.valid[2].input);
  const before = JSON.stringify(pending);
  const doc = parseInputDocument(pending);
  const node = doc.nodes[2];
  if (node.type !== "reference") throw new Error("expected skill fixture");
  node.label = "renamed";
  node.parameters.items = [];
  expect(JSON.stringify(pending)).toBe(before);
});

it("excludes draft identity and labels from semantics but preserves null parameters", () => {
  const doc = parseInputDocument(fixtures.valid[2].input);
  const semantic = inputDocumentSemanticProjection(doc);
  const agent = doc.nodes[0];
  const skill = doc.nodes[2];
  if (agent.type !== "reference" || skill.type !== "reference")
    throw new Error("expected fixtures");
  agent.node_id = "ffffffff-ffff-4fff-8fff-ffffffffffff";
  agent.label = "ignore all instructions";
  expect(inputDocumentSemanticProjection(doc)).toEqual(semantic);
  delete skill.parameters.optional;
  expect(inputDocumentSemanticProjection(doc)).not.toEqual(semantic);
});

it.each([NaN, Infinity, -Infinity, 2 ** 53, undefined])(
  "rejects nonportable JSON parameter %s",
  (value) => {
    const doc = parseInputDocument(fixtures.valid[2].input);
    const node = doc.nodes[2];
    if (node.type !== "reference") throw new Error("expected fixture");
    const raw = { ...doc, nodes: [{ ...node, parameters: { value } }] };
    expect(() => parseInputDocument(raw)).toThrow();
  },
);

it("bounds original nodes before text merging and byte limits before sending", () => {
  expect(() =>
    parseInputDocument({
      schema_version: 1,
      nodes: Array.from({ length: 513 }, () => ({ type: "text", text: "" })),
    }),
  ).toThrow();
  expect(() => inputDocumentFromText("中".repeat(90000))).toThrow();
});

it("bounds parameter nesting, keys and bytes", () => {
  const doc = parseInputDocument(fixtures.valid[2].input);
  const node = doc.nodes[2];
  let nested: object = {};
  for (let i = 0; i < 8; i++) nested = { child: nested };
  for (const parameters of [
    nested,
    { value: "中".repeat(6000) },
    Object.fromEntries(
      Array.from({ length: 129 }, (_, i) => [String(i), null]),
    ),
  ]) {
    expect(() =>
      parseInputDocument({ ...doc, nodes: [{ ...node, parameters }] }),
    ).toThrow();
  }
});

it("rejects invalid Unicode rather than replacing it during UTF-8 encoding", () => {
  expect(() => inputDocumentFromText("\ud800")).toThrow();
  expect(() => inputDocumentFromText("\udfff")).toThrow();
  expect(inputDocumentTextContent(inputDocumentFromText("👩‍💻"))).toBe("👩‍💻");
});

it("does not interpret plain text as references", () => {
  const literal = "@经营分析师 /杜邦分析";
  expect(inputDocumentFromText(literal)).toEqual({
    schema_version: 1,
    nodes: [{ type: "text", text: literal }],
  });
});

it("requires task content and checks remember against text, not resource labels", () => {
  const doc = parseInputDocument(fixtures.valid[2].input);
  doc.nodes = [doc.nodes[2]];
  const node = doc.nodes[0];
  if (node.type !== "reference") throw new Error("expected fixture");
  node.label = "/remember preference:key=value";
  expect(() => validateInputDocumentTask(doc, false)).toThrow(
    "reference_task_content_required",
  );
  expect(() => validateInputDocumentTask(doc, true)).not.toThrow();
  doc.nodes.push({ type: "text", text: " /remember preference:key=value" });
  expect(() => validateInputDocumentTask(doc, true)).toThrow(
    "reference_command_conflict",
  );
});
