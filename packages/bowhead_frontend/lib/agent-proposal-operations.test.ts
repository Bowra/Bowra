import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { applyOperations, MISSING, previewAtomicGroup, ProposalOperationError, type ProposalOperation } from "./agent-proposal-operations";

interface FixtureCase {
  name: string;
  document: Record<string, unknown>;
  operations: ProposalOperation[];
  expected?: Record<string, unknown>;
  error?: string;
}

// The same file runs against `agent_change_proposal_store.apply_operations` in
// packages/bowhead_core/tests/test_agent_change_apply_operations_fixture.py.
const fixture = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../eval/fixtures/agent_change/apply_operations_v1.json"), "utf-8"),
) as { contract: string; cases: FixtureCase[] };

describe("applyOperations", () => {
  it("reads the shared fixture contract", () => {
    expect(fixture.contract).toBe("agent-change-apply-operations/v1");
    expect(fixture.cases.length).toBeGreaterThan(10);
  });

  it.each(fixture.cases.map((item) => [item.name, item] as const))("matches the Python patch semantics: %s", (_name, item) => {
    const original = structuredClone(item.document);
    if (item.error) {
      let thrown: unknown = null;
      try {
        applyOperations(item.document, item.operations);
      } catch (error) {
        thrown = error;
      }
      expect(thrown).toBeInstanceOf(ProposalOperationError);
      expect((thrown as ProposalOperationError).code).toBe(item.error);
    } else {
      expect(applyOperations(item.document, item.operations)).toEqual(item.expected);
    }
    expect(item.document).toEqual(original);
  });
});

describe("previewAtomicGroup", () => {
  const proposal = {
    operations: [
      { op: "replace" as const, path: "/definition/system_prompt", value: "Lead with the answer.\nCite sources." },
      { op: "add" as const, path: "/definition/tool_refs", value: ["bi.query", "web.search"] },
      { op: "remove" as const, path: "/definition/output_schema" },
    ],
    atomic_groups: [
      { id: "prompt", operation_indexes: [0], depends_on: [] },
      { id: "tools", operation_indexes: [1, 2], depends_on: [] },
    ],
  };
  const document = { definition: { system_prompt: "Answer.\nCite sources.", tool_refs: ["bi.query", "kb.search"], output_schema: { type: "object" } } };

  it("diffs text against the draft and keys list items", () => {
    expect(previewAtomicGroup(document, proposal, "prompt")).toEqual({
      ok: true,
      rows: [{ kind: "text", path: "/definition/system_prompt", before: "Answer.\nCite sources.", after: "Lead with the answer.\nCite sources." }],
    });
    const tools = previewAtomicGroup(document, proposal, "tools");
    expect(tools.ok).toBe(true);
    expect(tools.rows[0]).toEqual({ kind: "list", path: "/definition/tool_refs", added: ["web.search"], removed: ["kb.search"], kept: 1 });
    expect(tools.rows[1]).toEqual({ kind: "value", path: "/definition/output_schema", before: { type: "object" }, after: MISSING });
  });

  it("falls back to the proposed values when the group no longer applies", () => {
    const preview = previewAtomicGroup({ definition: {} }, proposal, "prompt");
    expect(preview.ok).toBe(false);
    expect(preview.rows).toEqual([{ kind: "text", path: "/definition/system_prompt", before: null, after: "Lead with the answer.\nCite sources." }]);
  });
});
