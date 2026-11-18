import { describe, expect, it } from "vitest";
import {
  parseSelectedSkills,
  selectedSkillsForStart,
  selectedSkillsMatchDocument,
  skillSelectionFromInputDocument,
  type SelectedSkillRouteContext,
} from "./composer-selected-skills";
import { parseInputDocument, type InputDocument, type ResourceRef } from "./composer-input-document";

const VERSION = `sha256:${"a".repeat(64)}`;
const nodeId = (index: number) => `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;

function skill(id: string, sourceKind: "builtin" | "team" | "user" = "team"): ResourceRef {
  return { kind: "skill", source_kind: sourceKind, id, version: VERSION };
}

function documentOf(
  ...references: { resource: ResourceRef; parameters?: Record<string, unknown> }[]
): InputDocument {
  return parseInputDocument({
    schema_version: 1,
    nodes: [
      ...references.map((reference, index) => ({
        type: "reference",
        node_id: nodeId(index + 1),
        resource: reference.resource,
        label: `label${index}`,
        parameters: reference.parameters ?? {},
      })),
      { type: "text", text: " summarize the quarter" },
    ],
  });
}

const start: SelectedSkillRouteContext = {
  skillViaLead: true,
  submissionKind: "start",
  chatAction: false,
  wireSnapshot: { planningMode: null, draftChatBinding: null },
};

describe("Skill-only route decision", () => {
  it("sends one to three distinct Skills without parameters as exact refs in document order", () => {
    const document = documentOf(
      { resource: skill("sk_dupont", "user") },
      { resource: skill("sk_dupont", "team") },
      { resource: skill("sk_margin", "builtin") },
    );
    expect(selectedSkillsForStart(document, start)).toEqual([
      { kind: "skill", source_kind: "user", id: "sk_dupont", version: VERSION },
      { kind: "skill", source_kind: "team", id: "sk_dupont", version: VERSION },
      { kind: "skill", source_kind: "builtin", id: "sk_margin", version: VERSION },
    ]);
  });

  it.each([
    ["an Agent", { kind: "agent", source_kind: "system", id: "compute", version: VERSION }],
    ["a capability", { kind: "capability", id: "cap_visualize", version: 1 }],
    ["an MCP tool", {
      kind: "mcp_tool",
      source_id: "mcp_crm",
      local_contract_revision: VERSION,
      connection_binding_id: "bind_1",
      connection_subject_fingerprint: VERSION,
      operation_name: "lookup",
      operation_contract_revision: VERSION,
    }],
  ] as const)("keeps the reference path when the input also holds %s", (_label, other) => {
    const document = documentOf({ resource: skill("sk_dupont") }, { resource: other as ResourceRef });
    expect(selectedSkillsForStart(document, start)).toBeNull();
  });

  it.each([
    ["a value", { period: "2026Q4" }],
    ["a null value", { period: null }],
    ["an empty string", { period: "" }],
  ])("keeps the reference path when a Skill carries %s", (_label, parameters) => {
    const document = documentOf({ resource: skill("sk_dupont") }, { resource: skill("sk_margin"), parameters });
    expect(selectedSkillsForStart(document, start)).toBeNull();
  });

  it("treats an absent parameter object like an empty one", () => {
    const document = documentOf({ resource: skill("sk_dupont") });
    const reference = document.nodes[0] as { parameters?: unknown };
    delete reference.parameters;
    expect(skillSelectionFromInputDocument(document)).toHaveLength(1);
  });

  it("keeps the reference path without references, with duplicates or above three Skills", () => {
    expect(skillSelectionFromInputDocument(parseInputDocument({
      schema_version: 1,
      nodes: [{ type: "text", text: "plain" }],
    }))).toBeNull();
    const duplicate = documentOf({ resource: skill("sk_dupont") }, { resource: skill("sk_margin") });
    const second = duplicate.nodes[1] as { resource: ResourceRef };
    second.resource = skill("sk_dupont");
    expect(skillSelectionFromInputDocument(duplicate)).toBeNull();
    const four = documentOf({ resource: skill("sk_a") }, { resource: skill("sk_b") }, { resource: skill("sk_c") });
    four.nodes.splice(3, 0, { ...four.nodes[0], node_id: nodeId(9), resource: skill("sk_d") } as InputDocument["nodes"][number]);
    expect(skillSelectionFromInputDocument(four)).toBeNull();
  });

  it.each<[string, Partial<SelectedSkillRouteContext>]>([
    ["the capability is off", { skillViaLead: false }],
    ["it is a steer", { submissionKind: "steer" }],
    ["it is a replay", { submissionKind: "replay" }],
    ["a chat action owns it", { chatAction: true }],
    ["planning mode is on", { wireSnapshot: { planningMode: "planning" } }],
    ["an Agent draft is bound", { wireSnapshot: { draftChatBinding: { draftId: "sad_1", version: 1, dirty: false } } }],
    ["a settings target is bound", { wireSnapshot: { workspaceSettingsBinding: { clientInfo: "{}" } } }],
    ["a Coach canvas target is bound", { wireSnapshot: { coachCanvasBinding: { reference: "r" } } }],
    ["a document revision is targeted", { wireSnapshot: { documentRevisionTarget: { session_id: "s_1" } } }],
    ["it is context maintenance", { wireSnapshot: { contextMaintenance: { kind: "compact" } } }],
  ])("keeps the reference path when %s", (_label, change) => {
    const document = documentOf({ resource: skill("sk_dupont") });
    expect(selectedSkillsForStart(document, { ...start, ...change })).toBeNull();
  });
});

describe("frozen Skill selection", () => {
  it("accepts only exact, distinct Skill refs and returns them in canonical form", () => {
    expect(parseSelectedSkills([{ version: VERSION, id: "sk_dupont", source_kind: "team", kind: "skill" }]))
      .toEqual([{ kind: "skill", source_kind: "team", id: "sk_dupont", version: VERSION }]);
    for (const invalid of [
      [],
      [skill("sk_a"), skill("sk_b"), skill("sk_c"), skill("sk_d")],
      [skill("sk_a"), skill("sk_a")],
      [{ ...skill("sk_a"), version: "sha256:short" }],
      [{ ...skill("sk_a"), parameters: {} }],
      [{ kind: "agent", source_kind: "team", id: "sk_a", version: VERSION }],
      "sk_a",
    ]) expect(() => parseSelectedSkills(invalid), JSON.stringify(invalid)).toThrow();
  });

  it("matches a selection only to the document it was derived from", () => {
    const document = documentOf({ resource: skill("sk_dupont") }, { resource: skill("sk_margin") });
    const selection = skillSelectionFromInputDocument(document);
    expect(selectedSkillsMatchDocument(selection, document)).toBe(true);
    expect(selectedSkillsMatchDocument(selection!.slice(0, 1), document)).toBe(false);
    expect(selectedSkillsMatchDocument([...selection!].reverse(), document)).toBe(false);
    expect(selectedSkillsMatchDocument(
      [{ ...selection![0], version: `sha256:${"b".repeat(64)}` }, selection![1]],
      document,
    )).toBe(false);
  });
});
