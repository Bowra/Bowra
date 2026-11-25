/**
 * The Agent workbench release bar (docs/sprints/agent-page-redesign-2026-11-24/design.md §3 and
 * tech-design.md §3–§4): one status, one primary action, the steps of the release path. A pure reduction of
 * facts the page already has, so every state is a table row in its test. Copy lives in message keys
 * (`agent_release_bar.status_<id>`, `agent_release_bar.meta_<id>`); this module returns ids, not text.
 */

import type { AgentVersionWorkbench } from "@/lib/agents-api";
import type {
  ReleaseCardKind,
  TestChangeState,
} from "./agent-version-validation";

export type ReleaseBarStateId =
  | "archived"
  | "unsaved"
  | "no_change"
  | "released_shared"
  | "testing"
  | "in_review"
  | "rebase"
  | "declined"
  | "private_not_tested"
  | "private_no_cases"
  | "private_passed"
  | "private_safety_only"
  | "private_risks"
  | "private_worse"
  | "private_stale"
  | "private_could_not_run"
  | "shared_not_tested"
  | "shared_no_cases"
  | "shared_passed"
  | "shared_safety_only"
  | "shared_risks"
  | "shared_worse"
  | "shared_blocked"
  | "shared_stale"
  | "shared_could_not_run";

/** `calm` is the one-line bar with nothing to do; amber (`warn`) is kept for states that need a judgment. */
export type ReleaseBarTone = "calm" | "neutral" | "warn" | "bad";
export type ReleaseBarStepKey = "draft" | "test" | "review" | "release";
/** `info` marks a test that only ran the safety check; `off` a step the state does not involve. */
export type ReleaseBarStepTone =
  "done" | "now" | "todo" | "warn" | "bad" | "wait" | "info" | "off";

export type ReleaseBarAction =
  | "save"
  | "discard"
  | "release"
  | "submit"
  | "test"
  | "retest"
  | "test_safety_only"
  | "add_cases"
  | "edit_config"
  | "accept_risks"
  | "view_results"
  | "view_regressions"
  | "view_reason"
  | "ask_coach_fix"
  | "stop_test"
  | "withdraw"
  | "view_decision"
  | "rebase"
  | "restore"
  | "copy_link"
  | "copy_diagnostics";

export interface ReleaseBarStep {
  key: ReleaseBarStepKey;
  tone: ReleaseBarStepTone;
  current: boolean;
}

export interface ReleaseBarState {
  id: ReleaseBarStateId;
  tone: ReleaseBarTone;
  /** Sticks to the top while scrolling; a calm bar has nothing to do and stays in the flow. */
  sticky: boolean;
  shared: boolean;
  steps: ReleaseBarStep[];
  primary: ReleaseBarAction | null;
  secondary: ReleaseBarAction[];
  /** Shown as quiet buttons after the secondary ones (e.g. a safety-only test when there are no cases). */
  quiet: ReleaseBarAction[];
  /** The meta line links to the test result on the 版本 tab. */
  resultsLink: boolean;
}

export interface ReleaseBarInput {
  /** Null while loading, and for archived Agents (the workbench answers 404 for them). */
  workbench: AgentVersionWorkbench | null;
  kind: ReleaseCardKind | null;
  test: TestChangeState | null;
  hasOwnerCases: boolean;
  /** A blocked result has cases that got worse (`blockedByRegression`); otherwise a required point failed. */
  blockedByRegression: boolean;
  /** Unsaved Configure edits (`dirty`, not `needsSave`). */
  dirty: boolean;
  archived: boolean;
  /** The draft's audience is not private; with no draft, the live audience. */
  shared: boolean;
}

type StepSpec = readonly [
  ReleaseBarStepTone,
  ReleaseBarStepTone,
  ReleaseBarStepTone,
  ReleaseBarStepTone,
];

function steps(
  shared: boolean,
  spec: StepSpec,
  current: ReleaseBarStepKey,
): ReleaseBarStep[] {
  const keys: ReleaseBarStepKey[] = ["draft", "test", "review", "release"];
  return keys
    .map((key, index) => ({ key, tone: spec[index], current: key === current }))
    .filter((step) => shared || step.key !== "review");
}

function make(
  id: ReleaseBarStateId,
  tone: ReleaseBarTone,
  shared: boolean,
  options: {
    steps?: ReleaseBarStep[];
    primary?: ReleaseBarAction | null;
    secondary?: ReleaseBarAction[];
    quiet?: ReleaseBarAction[];
    resultsLink?: boolean;
  } = {},
): ReleaseBarState {
  return {
    id,
    tone,
    sticky: tone !== "calm",
    shared,
    steps: options.steps ?? [],
    primary: options.primary ?? null,
    secondary: options.secondary ?? [],
    quiet: options.quiet ?? [],
    resultsLink: options.resultsLink ?? false,
  };
}

/** The reviewer reason that is fixed with test cases; any other reason sends the Owner back to Configure. */
const EVIDENCE_REASON = "insufficient_evidence";

export function releaseBarState(
  input: ReleaseBarInput,
): ReleaseBarState | null {
  const { workbench, kind, test, shared } = input;
  if (input.archived)
    return make("archived", "calm", shared, { primary: "restore" });
  if (input.dirty) {
    return make("unsaved", "neutral", shared, {
      steps: steps(shared, ["now", "todo", "todo", "todo"], "draft"),
      primary: "save",
      secondary: ["discard"],
    });
  }
  if (!workbench || !kind || !test) return null;

  if (test === "testing") {
    return make("testing", "neutral", shared, {
      steps: steps(shared, ["done", "now", "todo", "todo"], "test"),
      secondary: workbench.active_execution_summary?.can_cancel
        ? ["stop_test"]
        : [],
    });
  }
  if (test === "review") {
    return make("in_review", "neutral", shared, {
      steps: steps(
        shared,
        ["done", input.hasOwnerCases ? "done" : "info", "wait", "todo"],
        "review",
      ),
      secondary: ["withdraw"],
    });
  }
  if (test === "rebase") {
    return make("rebase", "warn", shared, {
      steps: steps(shared, ["warn", "todo", "todo", "todo"], "draft"),
      primary: "rebase",
    });
  }
  const decision = workbench.latest_decision;
  if (
    shared &&
    decision?.state === "rejected" &&
    !workbench.candidate_identity
  ) {
    return make("declined", "bad", shared, {
      steps: steps(shared, ["done", "now", "bad", "todo"], "test"),
      primary:
        decision.reason_code === EVIDENCE_REASON ? "add_cases" : "edit_config",
      secondary: ["view_decision"],
    });
  }
  if (test === "nothing")
    return make(shared ? "released_shared" : "no_change", "calm", shared, {
      secondary: shared ? ["copy_link"] : [],
    });

  if (!shared) {
    const release: ReleaseBarAction | null = workbench.private_release
      ?.available
      ? "release"
      : null;
    const privateState = (
      id: ReleaseBarStateId,
      tone: ReleaseBarTone,
      testTone: ReleaseBarStepTone,
      secondary: ReleaseBarAction[],
      extra: { quiet?: ReleaseBarAction[]; resultsLink?: boolean } = {},
    ) =>
      make(id, tone, false, {
        steps: steps(false, ["done", testTone, "off", "now"], "release"),
        primary: release,
        secondary,
        ...extra,
      });
    switch (test) {
      case "not_tested":
        return privateState("private_not_tested", "neutral", "todo", ["test"]);
      case "no_cases":
        return privateState(
          "private_no_cases",
          "neutral",
          "todo",
          ["add_cases"],
          { quiet: ["test_safety_only"] },
        );
      case "passed":
        return input.hasOwnerCases
          ? privateState("private_passed", "neutral", "done", [], {
              resultsLink: true,
            })
          : privateState(
              "private_safety_only",
              "neutral",
              "info",
              ["add_cases"],
              { resultsLink: true },
            );
      case "risks":
        return privateState("private_risks", "warn", "warn", ["view_results"], {
          resultsLink: true,
        });
      case "regressions":
        return privateState(
          "private_worse",
          "warn",
          "warn",
          ["view_regressions"],
          { resultsLink: true },
        );
      case "stale":
        return privateState("private_stale", "warn", "warn", ["retest"]);
      case "could_not_run":
        return privateState("private_could_not_run", "warn", "warn", [
          "retest",
        ]);
      default:
        return null;
    }
  }

  const sharedState = (
    id: ReleaseBarStateId,
    tone: ReleaseBarTone,
    spec: StepSpec,
    current: ReleaseBarStepKey,
    primary: ReleaseBarAction | null,
    secondary: ReleaseBarAction[] = [],
    resultsLink = false,
  ) =>
    make(id, tone, true, {
      steps: steps(true, spec, current),
      primary,
      secondary,
      resultsLink,
    });
  switch (test) {
    case "not_tested":
    case "no_cases":
      return input.hasOwnerCases
        ? sharedState(
            "shared_not_tested",
            "neutral",
            ["done", "now", "todo", "todo"],
            "test",
            "test",
          )
        : sharedState(
            "shared_no_cases",
            "warn",
            ["done", "now", "todo", "todo"],
            "test",
            "test",
            ["add_cases"],
          );
    case "passed": {
      const primary: ReleaseBarAction =
        workbench.primary_action.kind === "activate_candidate"
          ? "release"
          : "submit";
      return input.hasOwnerCases
        ? sharedState(
            "shared_passed",
            "neutral",
            ["done", "done", "now", "todo"],
            "review",
            primary,
            [],
            true,
          )
        : sharedState(
            "shared_safety_only",
            "neutral",
            ["done", "info", "now", "todo"],
            "review",
            primary,
            [],
            true,
          );
    }
    case "risks":
      return sharedState(
        "shared_risks",
        "warn",
        ["done", "warn", "todo", "todo"],
        "test",
        "accept_risks",
        [],
        true,
      );
    case "regressions":
      return input.blockedByRegression
        ? sharedState(
            "shared_worse",
            "bad",
            ["done", "bad", "todo", "todo"],
            "test",
            "view_regressions",
            ["ask_coach_fix"],
            true,
          )
        : sharedState(
            "shared_blocked",
            "bad",
            ["done", "bad", "todo", "todo"],
            "test",
            "view_reason",
            ["ask_coach_fix"],
            true,
          );
    case "stale":
      return sharedState(
        "shared_stale",
        "warn",
        ["done", "warn", "todo", "todo"],
        "test",
        "retest",
      );
    case "could_not_run":
      return sharedState(
        "shared_could_not_run",
        "bad",
        ["done", "bad", "todo", "todo"],
        "test",
        "retest",
        ["copy_diagnostics"],
      );
    default:
      return null;
  }
}
