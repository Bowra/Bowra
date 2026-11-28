// @vitest-environment node
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { AgentChangeProposal } from "@/lib/agents-api";
import {
  aiOptimizeSourceRef,
  buildOptimizeGoal,
  factList,
  isAiOptimizeProposal,
  isShortOriginal,
  newestOpenAiOptimizeProposal,
  optimizeFailureKind,
  parseOptimizeRationale,
  proposedSystemPrompt,
  systemPromptGroupIds,
  taggedSuggestionLines,
} from "./agent-optimize";

const WORDS = { none: "无", more: (count: number) => `以及另外 ${count} 项`, separator: "、" };
const LABELS = { kept: "保留：", added: "新增：", unconfigured: "未配置：", none: "无" };

function proposal(overrides: Partial<AgentChangeProposal> = {}): AgentChangeProposal {
  return {
    kind: "agent_change_proposal",
    schema_version: 1,
    proposal_id: "acp_1",
    agent_id: "osa_1",
    source_type: "coach",
    source_ref: aiOptimizeSourceRef("u1"),
    rationale: "",
    status: "open",
    row_version: 1,
    created_at: "2026-11-25T08:00:00Z",
    decided_at: null,
    operations: [{ op: "replace", path: "/definition/system_prompt", value: "新的工作说明" }],
    atomic_groups: [{ id: "g1", operation_indexes: [0], depends_on: [] }],
    suggested_cases: [],
    base: { release_id: "arl_1", working_copy_id: "awc_1", working_copy_row_version: 2 },
    ...overrides,
  };
}

describe("factList", () => {
  it.each([
    [[], "无"],
    [["数据查询", "数据查询", " "], "数据查询"],
    [["A", "B"], "A、B"],
  ])("lists %j as %s", (names, expected) => {
    expect(factList(names, WORDS)).toBe(expected);
  });

  it("caps the names at 40 and says how many more", () => {
    const names = Array.from({ length: 45 }, (_, index) => `工具${index + 1}`);
    const text = factList(names, WORDS);
    expect(text.split("、")).toHaveLength(41);
    expect(text.endsWith("以及另外 5 项")).toBe(true);
  });
});

describe("buildOptimizeGoal", () => {
  const render = (values: { tools: string; plugins: string; skills: string; knowledge: string }) =>
    `Tools: ${values.tools}\nPlugins: ${values.plugins}\nSkills: ${values.skills}\nKnowledge: ${values.knowledge}`;

  it("fills every fact list from the configured names", () => {
    expect(buildOptimizeGoal(render, { tools: ["数据查询"], plugins: [], skills: ["周报"], knowledge: ["当前上下文中能读到的知识"] }, WORDS))
      .toBe("Tools: 数据查询\nPlugins: 无\nSkills: 周报\nKnowledge: 当前上下文中能读到的知识");
  });

  it("shrinks the lists until the goal fits the endpoint's limit", () => {
    const long = Array.from({ length: 40 }, (_, index) => `${"很长的名字".repeat(12)}${index}`);
    const goal = buildOptimizeGoal(render, { tools: long, plugins: long, skills: long, knowledge: long }, WORDS);
    expect(goal.length).toBeLessThanOrEqual(8000);
    expect(goal).toContain("以及另外");
  });
});

describe("parseOptimizeRationale", () => {
  it.each<[string, string, ReturnType<typeof parseOptimizeRationale>]>([
    [
      "labelled lines",
      "保留：3 条规则都在。\n新增：「查不到就如实说明。」 | 「先写变化和原因，再附表格。」。\n未配置：无。",
      { kept: "3 条规则都在。", added: ["查不到就如实说明。", "先写变化和原因，再附表格。"], unconfigured: [] },
    ],
    [
      "ASCII colons, bullets and an unconfigured tool",
      "- 保留: 门店口径\n- 新增: 无\n- 未配置: web_search | 外部天气数据",
      { kept: "门店口径", added: [], unconfigured: ["web_search", "外部天气数据"] },
    ],
    ["a missing label", "保留：都在\n新增：无", null],
    ["free text", "分成 5 节，原来的规则都在。", null],
  ])("reads %s", (_name, rationale, expected) => {
    expect(parseOptimizeRationale(rationale, LABELS)).toEqual(expected);
  });
});

describe("taggedSuggestionLines", () => {
  it("tags only the lines that hold a quoted added sentence verbatim", () => {
    const lines = taggedSuggestionLines("# 边界\n只用查到的数字。\n查不到就如实说明。\n# 输出格式", ["查不到就如实说明", "x"]);
    expect(lines.map((line) => line.added)).toEqual([false, false, true, false]);
  });

  it("tags nothing when the model quoted nothing", () => {
    expect(taggedSuggestionLines("a\nb", []).some((line) => line.added)).toBe(false);
  });
});

describe("isShortOriginal", () => {
  it.each([
    ["查门店销售，出个周报。别瞎编。", true],
    ["字".repeat(99), true],
    ["字".repeat(100), false],
  ])("%s → %s", (text, expected) => {
    expect(isShortOriginal(text)).toBe(expected);
  });
});

describe("systemPromptGroupIds", () => {
  it("chooses the groups that rewrite only 工作说明, with their dependencies", () => {
    expect(systemPromptGroupIds(proposal({
      operations: [
        { op: "replace", path: "/definition/system_prompt", value: "a" },
        { op: "replace", path: "/listing/description", value: "b" },
        { op: "replace", path: "/definition/system_prompt", value: "c" },
      ],
      atomic_groups: [
        { id: "prompt", operation_indexes: [0], depends_on: [] },
        { id: "listing", operation_indexes: [1], depends_on: [] },
        { id: "prompt-2", operation_indexes: [2], depends_on: ["prompt"] },
        { id: "mixed", operation_indexes: [2], depends_on: ["listing"] },
      ],
    }))).toEqual(["prompt", "prompt-2"]);
  });

  it("returns nothing when no group is 工作说明 only", () => {
    expect(systemPromptGroupIds(proposal({
      operations: [{ op: "replace", path: "/definition/output_schema", value: {} }],
      atomic_groups: [{ id: "g1", operation_indexes: [0], depends_on: [] }],
    }))).toEqual([]);
  });
});

describe("proposal facts", () => {
  it("reads the rewritten text and recognizes its own tag", () => {
    expect(proposedSystemPrompt(proposal())).toBe("新的工作说明");
    expect(proposedSystemPrompt(proposal({ operations: [{ op: "remove", path: "/definition/system_prompt" }] }))).toBeNull();
    expect(isAiOptimizeProposal(proposal())).toBe(true);
    expect(isAiOptimizeProposal(proposal({ source_ref: "owner:key" }))).toBe(false);
    expect(isAiOptimizeProposal(proposal({ source_type: "feedback" }))).toBe(false);
  });

  it("finds the newest open AI 优化 proposal", () => {
    const older = proposal({ proposal_id: "acp_old", created_at: "2026-11-25T07:00:00Z" });
    const newer = proposal({ proposal_id: "acp_new", created_at: "2026-11-25T09:00:00Z" });
    const dismissed = proposal({ proposal_id: "acp_gone", status: "dismissed", created_at: "2026-11-25T10:00:00Z" });
    const other = proposal({ proposal_id: "acp_coach", source_ref: "owner:k", created_at: "2026-11-25T11:00:00Z" });
    expect(newestOpenAiOptimizeProposal([older, newer, dismissed, other])?.proposal_id).toBe("acp_new");
    expect(newestOpenAiOptimizeProposal(undefined)).toBeNull();
  });
});

describe("optimizeFailureKind", () => {
  it.each<[unknown, string]>([
    [new ApiError(422, "x", { code: "team_model_unconfigured" }), "model_unconfigured"],
    [new ApiError(409, "x", { code: "agent_coach_generation_unknown" }), "unknown"],
    [new ApiError(409, "x", { code: "agent_coach_generation_running" }), "running"],
    [new ApiError(422, "x", { code: "no_change" }), "no_change"],
    [new ApiError(422, "x", { code: "parse_failed" }), "bad_format"],
    [new ApiError(422, "x", { code: "marker_echoed" }), "bad_format"],
    [new ApiError(422, "x", { code: "url_guard" }), "guarded"],
    [new ApiError(422, "x", { code: "agent_change_proposal_path_forbidden" }), "guarded"],
    [new ApiError(404, "x", { code: "agent_owner_required" }), "owner_required"],
    [new ApiError(409, "x", { code: "agent_working_copy_version_conflict" }), "version_conflict"],
    [new ApiError(409, "x", { code: "agent_change_merge_conflict" }), "merge_conflict"],
    [new ApiError(409, "x", { code: "agent_change_proposal_stale" }), "stale"],
    [new ApiError(504, "x"), "detached"],
    [new ApiError(502, "x", { code: "bad_gateway", nonApiResponse: true }), "detached"],
    [new TypeError("Failed to fetch"), "detached"],
    [new ApiError(500, "x", { code: "internal_error" }), "other"],
  ])("maps %o to %s", (cause, kind) => {
    expect(optimizeFailureKind(cause)).toBe(kind);
  });
});
