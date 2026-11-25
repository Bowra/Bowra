// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { AgentVersionWorkbench } from "@/lib/agents-api";
import type {
  ReleaseCardKind,
  TestChangeState,
} from "./agent-version-validation";
import {
  releaseBarState,
  type ReleaseBarInput,
  type ReleaseBarState,
} from "./release-bar-state";

const WORKBENCH: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: {
    release_id: "arl_2",
    release_sequence: 2,
    revision_id: "arv_2",
  },
  candidate_identity: null,
  next_draft_identity: {
    base_release_id: "arl_2",
    id: "awc_1",
    row_version: 3,
  },
  active_plan_identity: null,
  active_snapshot_identity: null,
  active_override_identity: null,
  active_execution_summary: null,
  control_row_version: null,
  display_state: "draft",
  draft_rebase_state: "current",
  readiness: null,
  reason_codes: [],
  stale_subject: null,
  primary_action: {
    kind: "prepare_candidate",
    enabled: true,
    disabled_reason_codes: [],
  },
  secondary_actions: [],
  private_release: {
    available: true,
    blocked_reason_code: null,
    candidate_covers_draft: false,
    release_sequence: 3,
    test_state: "not_tested",
    warning: false,
    reason_codes: [],
  },
};

function input(
  test: TestChangeState,
  overrides: Omit<Partial<ReleaseBarInput>, "workbench"> & {
    workbench?: Partial<AgentVersionWorkbench>;
  } = {},
): ReleaseBarInput {
  const { workbench, ...rest } = overrides;
  return {
    workbench: { ...WORKBENCH, ...workbench },
    kind: "prepare" as ReleaseCardKind,
    test,
    hasOwnerCases: true,
    blockedByRegression: true,
    dirty: false,
    archived: false,
    shared: false,
    ...rest,
  };
}

function summary(state: ReleaseBarState | null) {
  if (!state) return null;
  return {
    id: state.id,
    tone: state.tone,
    sticky: state.sticky,
    primary: state.primary,
    secondary: state.secondary.join(","),
    quiet: state.quiet.join(","),
    steps: state.steps
      .map((step) => `${step.key}:${step.tone}${step.current ? "*" : ""}`)
      .join(" "),
  };
}

describe("releaseBarState: one row per bar state", () => {
  it.each([
    // [label, input, id, tone, primary, secondary, steps]
    [
      "private draft with cases, not tested",
      input("not_tested"),
      "private_not_tested",
      "neutral",
      "release",
      "test",
      "draft:done test:todo release:now*",
    ],
    [
      "private draft without cases",
      input("no_cases", { hasOwnerCases: false }),
      "private_no_cases",
      "neutral",
      "release",
      "add_cases",
      "draft:done test:todo release:now*",
    ],
    [
      "private passed with cases",
      input("passed"),
      "private_passed",
      "neutral",
      "release",
      "",
      "draft:done test:done release:now*",
    ],
    [
      "private passed on the safety check only",
      input("passed", { hasOwnerCases: false }),
      "private_safety_only",
      "neutral",
      "release",
      "add_cases",
      "draft:done test:info release:now*",
    ],
    [
      "private with risks",
      input("risks"),
      "private_risks",
      "warn",
      "release",
      "view_results",
      "draft:done test:warn release:now*",
    ],
    [
      "private with worse cases (publish stays primary)",
      input("regressions"),
      "private_worse",
      "warn",
      "release",
      "view_regressions",
      "draft:done test:warn release:now*",
    ],
    [
      "private stale result",
      input("stale"),
      "private_stale",
      "warn",
      "release",
      "retest",
      "draft:done test:warn release:now*",
    ],
    [
      "private test could not run",
      input("could_not_run"),
      "private_could_not_run",
      "warn",
      "release",
      "retest",
      "draft:done test:warn release:now*",
    ],
    [
      "shared draft with cases",
      input("not_tested", { shared: true }),
      "shared_not_tested",
      "neutral",
      "test",
      "",
      "draft:done test:now* review:todo release:todo",
    ],
    [
      "shared draft without cases warns about a likely decline",
      input("no_cases", { shared: true, hasOwnerCases: false }),
      "shared_no_cases",
      "warn",
      "test",
      "add_cases",
      "draft:done test:now* review:todo release:todo",
    ],
    [
      "shared passed",
      input("passed", {
        shared: true,
        workbench: {
          primary_action: {
            kind: "submit_for_review",
            enabled: true,
            disabled_reason_codes: [],
          },
        },
      }),
      "shared_passed",
      "neutral",
      "submit",
      "",
      "draft:done test:done review:now* release:todo",
    ],
    [
      "shared passed on the safety check only",
      input("passed", {
        shared: true,
        hasOwnerCases: false,
        workbench: {
          primary_action: {
            kind: "submit_for_review",
            enabled: true,
            disabled_reason_codes: [],
          },
        },
      }),
      "shared_safety_only",
      "neutral",
      "submit",
      "",
      "draft:done test:info review:now* release:todo",
    ],
    [
      "shared risks",
      input("risks", { shared: true }),
      "shared_risks",
      "warn",
      "accept_risks",
      "",
      "draft:done test:warn* review:todo release:todo",
    ],
    [
      "shared worse cases",
      input("regressions", { shared: true }),
      "shared_worse",
      "bad",
      "view_regressions",
      "ask_coach_fix",
      "draft:done test:bad* review:todo release:todo",
    ],
    [
      "shared blocked without a regression",
      input("regressions", { shared: true, blockedByRegression: false }),
      "shared_blocked",
      "bad",
      "view_reason",
      "ask_coach_fix",
      "draft:done test:bad* review:todo release:todo",
    ],
    [
      "shared stale result",
      input("stale", { shared: true }),
      "shared_stale",
      "warn",
      "retest",
      "",
      "draft:done test:warn* review:todo release:todo",
    ],
    [
      "shared test could not run",
      input("could_not_run", { shared: true }),
      "shared_could_not_run",
      "bad",
      "retest",
      "copy_diagnostics",
      "draft:done test:bad* review:todo release:todo",
    ],
    [
      "in review",
      input("review", { shared: true }),
      "in_review",
      "neutral",
      null,
      "withdraw",
      "draft:done test:done review:wait* release:todo",
    ],
    [
      "draft behind a newer release",
      input("rebase"),
      "rebase",
      "warn",
      "rebase",
      "",
      "draft:warn* test:todo release:todo",
    ],
  ] as const)("%s", (_label, value, id, tone, primary, secondary, stepLine) => {
    const state = summary(releaseBarState(value));
    expect(state).toMatchObject({
      id,
      tone,
      primary,
      secondary,
      steps: stepLine,
      sticky: true,
    });
  });

  it("is calm and not sticky when nothing changed", () => {
    expect(
      summary(
        releaseBarState(
          input("nothing", { workbench: { next_draft_identity: null } }),
        ),
      ),
    ).toMatchObject({
      id: "no_change",
      tone: "calm",
      sticky: false,
      primary: null,
      steps: "",
    });
    expect(
      summary(releaseBarState(input("nothing", { shared: true }))),
    ).toMatchObject({
      id: "released_shared",
      tone: "calm",
      sticky: false,
      primary: null,
      secondary: "copy_link",
    });
  });

  it("offers a quiet safety-only test when a private draft has no cases", () => {
    expect(
      releaseBarState(input("no_cases", { hasOwnerCases: false }))?.quiet,
    ).toEqual(["test_safety_only"]);
  });

  it("drops the release button while the server does not offer a private release", () => {
    const state = releaseBarState(
      input("not_tested", {
        workbench: {
          private_release: { ...WORKBENCH.private_release!, available: false },
        },
      }),
    );
    expect(state?.primary).toBeNull();
  });

  it("asks to save unsaved edits before anything else, without amber", () => {
    for (const test of [
      "not_tested",
      "review",
      "testing",
      "regressions",
    ] as const) {
      expect(
        summary(releaseBarState(input(test, { dirty: true, shared: true }))),
      ).toMatchObject({
        id: "unsaved",
        tone: "neutral",
        primary: "save",
        secondary: "discard",
        steps: "draft:now* test:todo review:todo release:todo",
      });
    }
  });

  it("shows a running test, with stop only when the server allows it", () => {
    const running = {
      can_cancel: true,
      execution_id: "x",
      failure_code: null,
      finished_at: null,
      row_version: 1,
      started_at: null,
      status: "running",
    };
    expect(
      releaseBarState(
        input("testing", { workbench: { active_execution_summary: running } }),
      )?.secondary,
    ).toEqual(["stop_test"]);
    expect(
      releaseBarState(
        input("testing", {
          workbench: {
            active_execution_summary: { ...running, can_cancel: false },
          },
        }),
      )?.secondary,
    ).toEqual([]);
  });

  it("marks a safety-only submission in review with the info step", () => {
    expect(
      summary(
        releaseBarState(
          input("review", { shared: true, hasOwnerCases: false }),
        ),
      )?.steps,
    ).toBe("draft:done test:info review:wait* release:todo");
  });

  it("keeps a decline visible until a new submission, with the next step chosen by the reason", () => {
    const declined = (reason: string) =>
      input("not_tested", {
        shared: true,
        workbench: {
          latest_decision: {
            candidate_id: "arc_9",
            decided_at: "2026-11-24T10:00:00Z",
            reason_code: reason,
            state: "rejected",
          },
        },
      });
    expect(
      summary(releaseBarState(declined("insufficient_evidence"))),
    ).toMatchObject({
      id: "declined",
      tone: "bad",
      primary: "add_cases",
      secondary: "view_decision",
      steps: "draft:done test:now* review:bad release:todo",
    });
    expect(releaseBarState(declined("audience_too_broad"))?.primary).toBe(
      "edit_config",
    );
    const resubmitted = declined("insufficient_evidence");
    resubmitted.workbench = {
      ...resubmitted.workbench!,
      candidate_identity: {
        audience_mode: "custom",
        candidate_hash: "h",
        candidate_id: "arc_10",
        revision_id: "arv_3",
        row_version: 1,
        state: "prepared",
      },
    };
    expect(releaseBarState(resubmitted)?.id).toBe("shared_not_tested");
  });

  it("shows only the restore action for an archived Agent, whatever else is loaded", () => {
    expect(
      summary(
        releaseBarState({
          ...input("not_tested"),
          workbench: null,
          kind: null,
          test: null,
          archived: true,
        }),
      ),
    ).toMatchObject({
      id: "archived",
      tone: "calm",
      sticky: false,
      primary: "restore",
    });
  });

  it("waits for the workbench before showing anything else", () => {
    expect(
      releaseBarState({ ...input("not_tested"), workbench: null }),
    ).toBeNull();
  });

  it("links the meta line to the results only when there is a result", () => {
    expect(releaseBarState(input("passed"))?.resultsLink).toBe(true);
    expect(releaseBarState(input("not_tested"))?.resultsLink).toBe(false);
    expect(releaseBarState(input("stale", { shared: true }))?.resultsLink).toBe(
      false,
    );
  });

  it("never offers more than one primary action and marks exactly one current step", () => {
    const tests: TestChangeState[] = [
      "not_tested",
      "no_cases",
      "passed",
      "risks",
      "regressions",
      "stale",
      "could_not_run",
      "review",
      "rebase",
      "testing",
    ];
    for (const test of tests) {
      for (const shared of [false, true]) {
        if (test === "review" && !shared) continue; // only a shared release waits for review
        const state = releaseBarState(input(test, { shared }));
        expect(state).not.toBeNull();
        expect(state!.steps.filter((step) => step.current)).toHaveLength(1);
        expect(state!.secondary).not.toContain(state!.primary);
      }
    }
  });
});
