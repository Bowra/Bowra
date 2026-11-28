// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { AgentChangeProposal, AgentDraftChange } from "./agents-api";
import {
  draftChangeSection,
  draftFieldChip,
  pendingCaseProposals,
  pendingFieldProposals,
  pendingSuggestedCaseCount,
  summarizeDraftChanges,
} from "./agent-draft-changes";

describe("draftChangeSection", () => {
  it.each([
    ["/definition/knowledge_access", "who", "uses"],
    ["/definition/runtime_extensions", "who", "uses"],
    ["/definition/output_schema", "who", "does"],
    ["/definition/tool_refs", "uses", "uses"],
    ["/audience/effective_mode", "who", "who"],
    ["/definition/system_prompt", "does", "does"],
  ] as const)("puts %s in %s today and %s in the redesigned layout", (path, legacy, v2) => {
    expect(draftChangeSection(path)).toBe(legacy);
    expect(draftChangeSection(path, "v2")).toBe(v2);
  });

  it("names the redesigned sections in the release summary", () => {
    const changes: AgentDraftChange[] = [{ path: "/definition/knowledge_access", live: null, draft: { mode: "context_readable" }, source: "manual" }];
    expect(summarizeDraftChanges(changes).map((item) => item.section)).toEqual(["who"]);
    expect(summarizeDraftChanges(changes, "v2").map((item) => item.section)).toEqual(["uses"]);
  });
});

describe("draftFieldChip", () => {
  const coach = { state: "applied" as const, proposal_id: "acp_1", group_ids: [], before: "a", value: "b", conflict: false };
  it.each<[string, AgentDraftChange | null, ReadonlySet<string>, ReturnType<typeof draftFieldChip>]>([
    ["an unchanged field", { path: "/definition/system_prompt", live: "a", draft: "a", source: "manual" }, new Set(), null],
    ["the Owner's edit", { path: "/definition/system_prompt", live: "a", draft: "b", source: "manual" }, new Set(), "manual"],
    ["Coach's value", { path: "/definition/system_prompt", live: "a", draft: "b", source: "coach", coach }, new Set(), "coach"],
    ["AI 优化's value", { path: "/definition/system_prompt", live: "a", draft: "b", source: "coach", coach }, new Set(["acp_1"]), "ai_optimize"],
    ["an edit after Coach", { path: "/definition/system_prompt", live: "a", draft: "c", source: "coach", coach: { ...coach, conflict: true } }, new Set(), "manual"],
    ["a pipeline's value", { path: "/definition/system_prompt", live: "a", draft: "b", source: "ci" }, new Set(), null],
    ["a field with no change", null, new Set(), null],
  ])("shows %s", (_name, change, ai, expected) => {
    expect(draftFieldChip(change ? [change] : [], "/definition/system_prompt", ai)).toBe(expected);
  });
});

describe("pending proposals by part", () => {
  const base = {
    kind: "agent_change_proposal" as const, schema_version: 1 as const, agent_id: "osa_1", rationale: "", row_version: 1, created_at: "2026-11-25T08:00:00Z",
    decided_at: null, operations: [], atomic_groups: [], base: { release_id: "arl_1", working_copy_id: null, working_copy_row_version: null },
  };
  const pending = { id: "c1", task: "q", covers_group_ids: [], failure_mode: "f", decision_status: "pending" as const };
  const field = { ...base, proposal_id: "field", source_type: "feedback", source_ref: "fb", status: "open", suggested_cases: [] } as AgentChangeProposal;
  const mixed = { ...base, proposal_id: "mixed", source_type: "coach", source_ref: "owner:k", status: "open", suggested_cases: [pending, { ...pending, id: "c2" }] } as AgentChangeProposal;
  const cases = { ...base, proposal_id: "cases", source_type: "coach", source_ref: "owner:k", status: "open", suggested_cases: [pending], base: { ...base.base, suite_revision_id: "aev_1" } } as AgentChangeProposal;
  const ai = { ...base, proposal_id: "ai", source_type: "coach", source_ref: "ai_optimize:system_prompt:u", status: "open", suggested_cases: [pending] } as AgentChangeProposal;

  it("lists field changes under 更多设置 without AI 优化's own result or case-only suggestions", () => {
    expect(pendingFieldProposals([field, mixed, cases, ai]).map((item) => item.proposal_id)).toEqual(["field", "mixed"]);
  });

  it("lists every proposal with pending cases on 测试用例, AI 优化's included", () => {
    expect(pendingCaseProposals([field, mixed, cases, ai]).map((item) => item.proposal_id)).toEqual(["mixed", "cases", "ai"]);
    expect(pendingSuggestedCaseCount([field, mixed, cases, ai])).toBe(4);
  });
});
