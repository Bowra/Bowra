import { describe, expect, it } from "vitest";

import type { AgentVersionCaseChange, AgentVersionWorkbench } from "@/lib/agents-api";
import { makeCiFormat } from "./agent-ci-shared";
import { answerText, attemptsSentence, blockedByRegression, candidateIsOutdated, candidateReleaseSequence, orderCaseChanges, releaseCardKind, releaseTestLine, testChangeState, unknownResultCause, unstableCases, versionRailModel, type TestChangeState } from "./agent-version-validation";

const BASE: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: { release_id: "arl_7", release_sequence: 7, revision_id: "arv_7" },
  candidate_identity: { audience_mode: "private", candidate_hash: "h", candidate_id: "arc_8", revision_id: "arv_8", row_version: 1, state: "prepared" },
  next_draft_identity: { base_release_id: "arl_7", id: "awc_1", row_version: 2 },
  active_plan_identity: { id: "aep_1", hash: "p" },
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: 1,
  display_state: "prepared",
  draft_rebase_state: "current",
  readiness: "missing",
  reason_codes: [],
  stale_subject: null,
  primary_action: { kind: "run_release_comparison", enabled: true, disabled_reason_codes: [] },
  secondary_actions: [],
};

const t = (key: string, values?: Record<string, string | number>) => (values ? `${key}(${Object.values(values).join(",")})` : key);
const format = makeCiFormat("en-US");

function rail(workbench: AgentVersionWorkbench, state: TestChangeState, draftSavedAt: string | null = null) {
  return versionRailModel(workbench, { state, hasDraft: true, draftSavedAt, liveActivatedAt: "2026-09-28T02:00:00Z", format, t });
}

function stateOf(workbench: AgentVersionWorkbench, options: { preparing?: boolean; hasOwnerCases?: boolean; startFailed?: boolean } = {}) {
  return testChangeState(workbench, {
    kind: releaseCardKind(workbench, options.preparing ?? false),
    hasOwnerCases: options.hasOwnerCases ?? true,
    startFailed: options.startFailed ?? false,
  });
}

describe("releaseCardKind", () => {
  it.each([
    ["prepare_candidate", null, "missing", "prepare"],
    ["rebase_draft", null, "missing", "rebase"],
    ["wait_for_review", null, "ready", "review"],
    ["view_comparison", "running", "missing", "comparing"],
    ["run_release_comparison", null, "missing", "compare"],
    ["run_release_comparison", "cancelled", "missing", "cancelled"],
    ["run_release_comparison", "failed", "missing", "failed"],
    ["retry_after_environment_fix", "completed", "unknown", "environment"],
    ["resolve_risks", "completed", "ready_with_risks", "risks"],
    ["fix_next_draft", "completed", "blocked", "blocked"],
    ["activate_candidate", "completed", "ready", "ready"],
    ["submit_for_review", "completed", "ready", "ready"],
  ] as const)("%s with execution %s and readiness %s shows %s", (action, status, readiness, kind) => {
    const workbench = {
      ...BASE,
      readiness,
      primary_action: { kind: action, enabled: true, disabled_reason_codes: [] },
      active_execution_summary: status ? { can_cancel: false, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: null, status } : null,
    } as AgentVersionWorkbench;
    expect(releaseCardKind(workbench, false)).toBe(kind);
  });

  it("shows preparing while a prepare request is in flight, whatever the server says", () => {
    expect(releaseCardKind({ ...BASE, primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] } }, true)).toBe("preparing");
  });
});

describe("unknownResultCause", () => {
  it.each([
    [["attempt_time_limit_exceeded"], "time_limit"],
    [["comparison_duration_budget_exceeded"], "time_limit"],
    [["attempt_infrastructure_incomplete", "attempt_time_limit_exceeded"], "environment"],
    [["provider_identity_incomparable"], "inconclusive"],
    // A case that can't be tested in evaluation is not an environment fault.
    [["tool_not_run_in_eval"], "inconclusive"],
  ] as const)("%j is %s", (reasons, cause) => {
    expect(unknownResultCause(reasons)).toBe(cause);
  });

});

describe("testChangeState (Test this change, §4)", () => {
  const action = (kind: string) => ({ kind, enabled: true, disabled_reason_codes: [] });
  const running = { can_cancel: true, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: null, status: "running" };

  it("a saved draft without a candidate, or a prepared candidate never compared, is simply not tested", () => {
    expect(stateOf({ ...BASE, candidate_identity: null, primary_action: action("prepare_candidate") })).toBe("not_tested");
    // A candidate left prepared by a failed start is invisible: the next press reuses it.
    expect(stateOf(BASE)).toBe("not_tested");
  });

  it("testing covers the request in flight and the running comparison, Coach's included", () => {
    expect(stateOf({ ...BASE, candidate_identity: null, primary_action: action("prepare_candidate") }, { preparing: true })).toBe("testing");
    expect(stateOf({ ...BASE, primary_action: action("view_comparison"), active_execution_summary: running } as AgentVersionWorkbench)).toBe("testing");
  });

  it("results map to passed, advisory risks and regressions", () => {
    expect(stateOf({ ...BASE, readiness: "ready", primary_action: action("activate_candidate") })).toBe("passed");
    expect(stateOf({ ...BASE, readiness: "ready", primary_action: action("submit_for_review") })).toBe("passed");
    expect(stateOf({ ...BASE, readiness: "ready_with_risks", primary_action: action("resolve_risks") })).toBe("risks");
    expect(stateOf({ ...BASE, readiness: "blocked", primary_action: action("fix_next_draft") })).toBe("regressions");
  });

  it("a test that could not run says so, including a start the server refused", () => {
    expect(stateOf({ ...BASE, readiness: "unknown", primary_action: action("retry_after_environment_fix") })).toBe("could_not_run");
    const stopped = { ...BASE, primary_action: action("run_release_comparison"), active_execution_summary: { ...running, status: "cancelled" } } as AgentVersionWorkbench;
    expect(stateOf(stopped)).toBe("could_not_run");
    expect(stateOf(BASE, { startFailed: true })).toBe("could_not_run");
  });

  it("a result that no longer covers the saved draft is stale and offers only Test again", () => {
    const ready = { ...BASE, readiness: "ready" as const, primary_action: action("activate_candidate") };
    expect(stateOf({ ...ready, draft_ahead_of_candidate: true })).toBe("stale");
    expect(stateOf({ ...ready, display_state: "candidate_stale", primary_action: action("prepare_candidate") })).toBe("stale");
    const moved = { ...ready, live_identity: { release_id: "arl_8", release_sequence: 8, revision_id: "arv_8" }, candidate_identity: { ...BASE.candidate_identity!, base_release_id: "arl_7" } };
    expect(stateOf(moved)).toBe("stale");
    // Cases saved after a finished test: the server asks for a comparison; the card says the result is out of date.
    const behind = { ...BASE, evaluation_plan_behind: true, active_snapshot_identity: { id: "s", hash: "h", created_at: "2026-10-05T06:26:00Z" } };
    expect(stateOf(behind)).toBe("stale");
  });

  it("nothing to test, no cases, review and rebase", () => {
    expect(stateOf({ ...BASE, candidate_identity: null, next_draft_identity: null, primary_action: action("prepare_candidate") })).toBe("nothing");
    expect(stateOf({ ...BASE, candidate_identity: null, primary_action: action("prepare_candidate") }, { hasOwnerCases: false })).toBe("no_cases");
    expect(stateOf({ ...BASE, candidate_identity: { ...BASE.candidate_identity!, state: "pending" }, primary_action: action("wait_for_review") })).toBe("review");
    expect(stateOf({ ...BASE, candidate_identity: null, draft_rebase_state: "required", primary_action: action("rebase_draft") })).toBe("rebase");
  });
});

describe("versionRailModel", () => {
  it("shows the released version and this change; only the released version has a number", () => {
    const [live, change] = rail(BASE, "not_tested", "2026-10-20T12:15:00Z");
    expect(live).toMatchObject({ key: "live", value: "v7", tone: "plain" });
    expect(live.sub).toMatch(/^version_rail_live_released\(Sep 2[78]\)$/);
    expect(change).toMatchObject({ key: "change", tone: "focus", sub: "version_test_state_not_tested" });
    expect(change.value).toMatch(/^version_rail_change_saved\(\d{1,2}:\d{2}( [AP]M)?\)$/);
    expect(JSON.stringify([live, change])).not.toContain("v8");
  });

  it("testing carries determinate progress when run insights report it", () => {
    const workbench = {
      ...BASE,
      active_execution_summary: { can_cancel: true, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: null, status: "running", progress: { cases: { completed: 3, total: 8 } } },
    } as unknown as AgentVersionWorkbench;
    expect(rail(workbench, "testing")[1]).toMatchObject({ spinner: true, progress: { completed: 3, total: 8 }, sub: "version_test_state_testing" });
    expect(rail({ ...workbench, active_execution_summary: { ...workbench.active_execution_summary!, progress: undefined } }, "testing")[1].progress).toBeUndefined();
  });

  it.each([
    ["passed", "focus", "check"],
    ["regressions", "bad", "x"],
    ["could_not_run", "bad", "alert"],
    ["stale", "bad", "alert"],
  ] as const)("%s reads as a %s change card", (state, tone, icon) => {
    expect(rail(BASE, state)[1]).toMatchObject({ tone, icon, sub: `version_test_state_${state}` });
  });

  it("a draft that must be rebased names the released version", () => {
    expect(rail({ ...BASE, draft_rebase_state: "required" }, "rebase")[1].sub).toBe("version_rail_rebase(v7)");
  });

  it("nothing to test puts the released version in focus", () => {
    const [live, change] = rail({ ...BASE, candidate_identity: null, next_draft_identity: null }, "nothing");
    expect(live.tone).toBe("focus");
    expect(change).toMatchObject({ value: "version_test_state_none", valueMuted: true });
  });

  it("the release action's number comes from the server, not live + 1", () => {
    expect(candidateReleaseSequence({ ...BASE, candidate_identity: { ...BASE.candidate_identity!, release_sequence_if_activated: 12 } })).toBe(12);
    expect(candidateReleaseSequence(BASE)).toBe(8);
    expect(candidateIsOutdated(BASE)).toBe(false);
  });
});

describe("orderCaseChanges", () => {
  const item = (change: AgentVersionCaseChange["change"], label: string, key: string): AgentVersionCaseChange => ({
    suite_revision_id: "aev", suite_name: null, case_revision_id: `aec_${key}`, case_key: key, case_label: label,
    change, classification: "x", reason_codes: [], key_case: false, baseline: { passed: 1, runs: 3 }, candidate: { passed: 2, runs: 3 },
  });

  it("puts worse first, then inconsistent, then better, with a stable second key", () => {
    const ordered = orderCaseChanges([
      item("better", "B", "q.1"),
      item("worse", "Z", "q.2"),
      item("unstable", "A", "q.3"),
      item("worse", "A", "q.5"),
      item("worse", "A", "q.4"),
    ]);
    expect(ordered.map((entry) => `${entry.change}:${entry.case_label}:${entry.case_key}`)).toEqual([
      "worse:A:q.4", "worse:A:q.5", "worse:Z:q.2", "unstable:A:q.3", "better:B:q.1",
    ]);
  });
});

describe("makeCiFormat", () => {
  it("formats usage as tokens, counts and minutes", () => {
    const zh = makeCiFormat("zh-CN");
    expect(zh.tokenRange({ low: 12_000, high: 18_000 })).toBe("1.2万–1.8万");
    expect(format.tokenRange({ low: 12_000, high: 18_000 })).toBe("12K–18K");
    expect(format.count(9800)).toBe("9,800");
    expect(format.minuteRange({ low: 300_000, high: 540_000 })).toBe("5–9 min");
    expect(format.elapsed(252_000)).toBe("4 min 12 sec");
  });
});

describe("answerText", () => {
  it("reads a structured answer's Chat text and leaves any other answer as stored", () => {
    expect(answerText('{"delivery_surface":"chat","exec_summary":"## Report\\n\\n| a | b |"}')).toBe("## Report\n\n| a | b |");
    expect(answerText("Plain answer")).toBe("Plain answer");
    expect(answerText('{"exec_summary": ')).toBe('{"exec_summary": ');
    expect(answerText('{"other": 1}')).toBe('{"other": 1}');
  });
});

describe("blocked results (B6)", () => {
  const summary = (worse: number, both: number) => ({ summary: { case_count: 1, worse, unstable: 0, better: 0, not_comparable: 0, unchanged_required_failures: both } });

  it("is a regression only when a case got worse", () => {
    expect(blockedByRegression(["critical_regression"], null)).toBe(true);
    expect(blockedByRegression(["required_quality_failed"], summary(1, 0))).toBe(true);
    // The walkthrough: the required case fails on v1 too, so nothing got worse.
    expect(blockedByRegression(["required_quality_failed"], summary(0, 1))).toBe(false);
  });

  it("the rail says a required case failed instead of 'cases got worse' when both versions fail", () => {
    const blocked = { ...BASE, primary_action: { kind: "fix_next_draft", enabled: true, disabled_reason_codes: [] } } as AgentVersionWorkbench;
    const [, both] = versionRailModel(blocked, { state: "regressions", regressed: false, hasDraft: true, draftSavedAt: null, liveActivatedAt: null, format, t });
    const [, worse] = versionRailModel(blocked, { state: "regressions", hasDraft: true, draftSavedAt: null, liveActivatedAt: null, format, t });
    expect(both.sub).toBe("version_test_state_blocked");
    expect(worse.sub).toBe("version_test_state_regressions");
  });
});

describe("unstable cases", () => {
  const item = (id: string, reasons: string[], change: AgentVersionCaseChange["change"] = "unstable"): AgentVersionCaseChange => ({
    suite_revision_id: "aev_1", suite_name: null, case_revision_id: id, case_key: id, case_label: id, change, classification: "inconclusive",
    reason_codes: reasons, key_case: false, baseline: { passed: 3, failed: 0, runs: 3 }, candidate: { passed: 2, failed: 1, runs: 3 },
  });

  it("lists every case behind the reason, including one also left without a result", () => {
    const listed = unstableCases({
      items: [item("a", ["flaky_limit_exceeded"]), item("b", ["minimum_delta_not_met"]), item("c", [], "worse")],
      unfinished: [{ suite_revision_id: "aev_1", suite_name: null, case_revision_id: "d", case_key: "d", case_label: "d", reason_codes: ["attempt_tool_failed", "flaky_limit_exceeded"], tools: [], candidate: { passed: 1, failed: 1, runs: 3 }, baseline: null }],
    });
    expect(listed.map((entry) => entry.item.case_revision_id)).toEqual(["a", "d"]);
    expect(listed[1].candidate).toEqual({ passed: 1, failed: 1, runs: 3 });
  });

  it("says how a side's attempts split, naming attempts without a verdict", () => {
    expect(attemptsSentence("change", { passed: 3, failed: 0, runs: 3 }, t)).toBe("version_attempts_all_passed(change,3)");
    expect(attemptsSentence("change", { passed: 0, failed: 3, runs: 3 }, t)).toBe("version_attempts_all_failed(change,3)");
    expect(attemptsSentence("change", { passed: 1, failed: 1, runs: 3 }, t)).toBe("version_attempts_split(change,3,1,1)version_attempts_undecided(1)");
    // An older server sends no failed count: the rest count as failed.
    expect(attemptsSentence("change", { passed: 2, runs: 3 }, t)).toBe("version_attempts_split(change,3,2,1)");
  });

  it("gives the release dialog one line per test state, and none when the test passed", () => {
    const changes = { items: [item("a", ["flaky_limit_exceeded"])], unfinished: [], summary: { case_count: 2, worse: 0, unstable: 1, better: 0, not_comparable: 0, unchanged_required_failures: 0 } };
    expect(releaseTestLine("passed", [], changes, t)).toBeNull();
    expect(releaseTestLine("no_conclusion", ["flaky_limit_exceeded"], changes, t)).toBe("version_release_test_no_conclusion_unstable(1)");
    expect(releaseTestLine("no_conclusion", ["attempt_tool_failed"], changes, t)).toBe("version_release_test_no_conclusion");
    expect(releaseTestLine("not_tested", [], null, t)).toBe("version_release_test_not_tested");
    expect(releaseTestLine("safety_failed", ["agent_safety_blocked"], null, t)).toBe("version_release_test_safety_failed");
  });
});
