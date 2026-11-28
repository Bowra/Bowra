// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { getAgentEvalManager, saveAgentEvalSuite, suggestAgentEvalCasesFromTurns } from "@/lib/agents-api";
import { saveTrialTurnAsCase, TrialCaseError } from "./agent-trial-case";

vi.mock("@/lib/agents-api", () => ({
  getAgentEvalManager: vi.fn(),
  saveAgentEvalSuite: vi.fn(),
  suggestAgentEvalCasesFromTurns: vi.fn(),
}));

const CASE = {
  expected_contract: { allowed_terminal_statuses: ["completed"] },
  scorer_config: { max_cost_microusd: 5_000_000 },
  effect_policy: { mode: "no_effects" },
};

function manager(cases: Array<{ case_key: string; task: string }> | null) {
  return {
    suites: cases === null ? [] : [{
      suite_id: "aes_1",
      suite_kind: "owner_quality",
      name: "门店周报",
      visibility: "owner",
      state: "active",
      revision: { cases: cases.map((item) => ({ ...CASE, ...item, case_revision_id: "acr_x" })) },
    }],
  } as never;
}

afterEach(() => vi.clearAllMocks());

describe("saveTrialTurnAsCase", () => {
  it("drafts a case from the trial Turn and appends it to the Owner's suite", async () => {
    vi.mocked(suggestAgentEvalCasesFromTurns).mockResolvedValue({ kind: "agent_eval_case_suggestions", schema_version: 1, cases: [{ ...CASE, case_key: "conversation.1", task: "本周销售怎么样？" }] });
    vi.mocked(getAgentEvalManager).mockResolvedValue(manager([{ case_key: "quality.1", task: "已有的问题" }, { case_key: "align.p1", task: "对齐的问题" }]));
    await expect(saveTrialTurnAsCase("osa_1", "t_1", "默认")).resolves.toBe(3);
    expect(suggestAgentEvalCasesFromTurns).toHaveBeenCalledWith("osa_1", ["t_1"]);
    const body = vi.mocked(saveAgentEvalSuite).mock.calls[0][1];
    expect(body).toMatchObject({ suite_id: "aes_1", name: "门店周报", visibility: "owner" });
    expect(body.cases.map((item) => [item.case_key, item.task])).toEqual([
      ["quality.1", "已有的问题"],
      ["align.p1", "对齐的问题"],
      ["quality.3", "本周销售怎么样？"],
    ]);
    expect(body.cases.every((item) => !("case_revision_id" in item))).toBe(true);
  });

  it("creates the Owner's suite when there is none yet", async () => {
    vi.mocked(suggestAgentEvalCasesFromTurns).mockResolvedValue({ kind: "agent_eval_case_suggestions", schema_version: 1, cases: [{ ...CASE, case_key: "conversation.1", task: "q" }] });
    vi.mocked(getAgentEvalManager).mockResolvedValue(manager(null));
    await saveTrialTurnAsCase("osa_1", "t_1", "我的用例");
    expect(vi.mocked(saveAgentEvalSuite).mock.calls[0][1]).toMatchObject({ suite_id: null, name: "我的用例", visibility: "owner" });
  });

  it("refuses an empty draft and a full suite without saving", async () => {
    vi.mocked(suggestAgentEvalCasesFromTurns).mockResolvedValue({ kind: "agent_eval_case_suggestions", schema_version: 1, cases: [] });
    await expect(saveTrialTurnAsCase("osa_1", "t_1", "x")).rejects.toBeInstanceOf(TrialCaseError);
    vi.mocked(suggestAgentEvalCasesFromTurns).mockResolvedValue({ kind: "agent_eval_case_suggestions", schema_version: 1, cases: [{ ...CASE, case_key: "c", task: "q" }] });
    vi.mocked(getAgentEvalManager).mockResolvedValue(manager(Array.from({ length: 200 }, (_, index) => ({ case_key: `quality.${index + 1}`, task: `q${index}` }))));
    await expect(saveTrialTurnAsCase("osa_1", "t_1", "x")).rejects.toMatchObject({ code: "agent_trial_suite_full" });
    expect(saveAgentEvalSuite).not.toHaveBeenCalled();
  });
});
