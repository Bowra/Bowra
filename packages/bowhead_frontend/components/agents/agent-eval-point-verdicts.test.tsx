import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { AgentEvalCasePointDefinition, AgentEvalGradedPoint } from "@/lib/agents-api";

const api = vi.hoisted(() => ({
  reportAgentEvalPointJudgedWrongly: vi.fn(),
  getAgentVersionUnmetPoints: vi.fn(),
}));

vi.mock("@/lib/agents-api", async (importOriginal) => ({ ...(await importOriginal<object>()), ...api }));

import { AttemptPointVerdicts, PointCalibrationNotices, UnmetPointsList } from "./agent-eval-point-verdicts";

const DEFINITIONS: AgentEvalCasePointDefinition[] = [
  { id: "p_0a1b", kind: "judgment", text: "Compares with the same month last year", state: "confirmed", calibration_failure: null },
  { id: "p_0c1d", kind: "data_fact", text: "States East China revenue", state: "confirmed", calibration_failure: null },
  { id: "p_0e1f", kind: "judgment", text: "Names the main driver", state: "confirmed", calibration_failure: null },
];
const POINTS: AgentEvalGradedPoint[] = [
  { id: "p_0a1b", met: false, quote: "Revenue was flat.", applicable: true, blocking: false, reason: null },
  { id: "p_0c1d", met: true, quote: "East China made 12.4m.", applicable: true, blocking: false, reason: null },
  { id: "p_0e1f", met: null, quote: null, applicable: true, blocking: false, reason: "quote_missing" },
];
const RESULT = { run_id: "aer_1", case_revision_id: "aec_1", attempt: 1 };

afterEach(() => vi.clearAllMocks());

describe("AttemptPointVerdicts", () => {
  it("lists unmet and ungraded points first and folds met ones, each with its evidence", () => {
    renderIntl(<AttemptPointVerdicts agentId="osa_1" definitions={DEFINITIONS} points={POINTS} caseResult={RESULT} />, { locale: "en-US" });

    const open = screen.getByRole("list", { name: "Expected points" });
    expect(within(open).getByText("Compares with the same month last year")).toBeTruthy();
    expect(within(open).getByText("Revenue was flat.")).toBeTruthy();
    expect(within(open).getByText(/Not graded:/)).toBeTruthy();
    // A met point is still listed, folded, so a wrong pass can be reported (R26).
    expect(screen.getByText("1 point met")).toBeTruthy();
    expect(screen.getByText("East China made 12.4m.")).toBeTruthy();
    // Only a graded verdict can be disputed: one unmet, one met.
    expect(screen.getAllByRole("button", { name: "Judged wrongly" })).toHaveLength(2);
  });

  it("records Judged wrongly and shows Coach's rewording without claiming the result changed", async () => {
    api.reportAgentEvalPointJudgedWrongly.mockResolvedValue({ rewording: "Gives the change against the same month last year" });
    renderIntl(<AttemptPointVerdicts agentId="osa_1" definitions={DEFINITIONS} points={POINTS} caseResult={RESULT} />, { locale: "en-US" });

    fireEvent.click(screen.getAllByRole("button", { name: "Judged wrongly" })[0]);

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("This comparison's result doesn't change"));
    expect(screen.getByRole("status").textContent).toContain("Gives the change against the same month last year");
    expect(api.reportAgentEvalPointJudgedWrongly).toHaveBeenCalledWith("osa_1", "p_0a1b", RESULT);
  });

  it("lists an unmet required action with the Tool it didn't call, and offers no dispute for a rule check", () => {
    const definitions: AgentEvalCasePointDefinition[] = [
      ...DEFINITIONS,
      { id: "p_1a2b", kind: "required_action", text: "Computes the totals with code", state: "confirmed", calibration_failure: null, tools: ["python_sandbox"] },
    ];
    const points: AgentEvalGradedPoint[] = [
      { id: "p_0c1d", met: true, quote: "East China made 12.4m.", applicable: true, blocking: false, reason: null },
      { id: "p_1a2b", met: false, quote: null, applicable: true, blocking: true, reason: "tool_not_called" },
    ];
    renderIntl(<AttemptPointVerdicts agentId="osa_1" definitions={definitions} points={points} caseResult={RESULT} />, { locale: "en-US" });

    const open = screen.getByRole("list", { name: "Expected points" });
    expect(within(open).getByText("Computes the totals with code")).toBeTruthy();
    expect(open.textContent).toMatch(/It didn't call “[^”]+”\./);
    // Only the graded data fact can be disputed.
    expect(screen.getAllByRole("button", { name: "Judged wrongly" })).toHaveLength(1);
  });

  it("offers no dispute when the server didn't say which run the answer came from", () => {
    renderIntl(<AttemptPointVerdicts agentId="osa_1" definitions={DEFINITIONS} points={POINTS} caseResult={null} />, { locale: "en-US" });

    expect(screen.queryByRole("button", { name: "Judged wrongly" })).toBeNull();
  });
});

describe("PointCalibrationNotices", () => {
  it("says why a point is for reference only and offers the rewording", () => {
    renderIntl(
      <PointCalibrationNotices
        definitions={[{ ...DEFINITIONS[0], calibration_failure: { reason: "good_judged_unmet", rewording: "Gives the year-on-year change" } }, DEFINITIONS[1]]}
      />,
      { locale: "zh-CN" },
    );

    const list = screen.getByRole("list", { name: "只作参考的要点" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(list.textContent).toContain("满足这条要点的回答被判成了不满足");
    expect(list.textContent).toContain("Gives the year-on-year change");
  });
});

describe("UnmetPointsList", () => {
  it("lists each unmet point with its quote and Judged wrongly beside it (R28)", async () => {
    api.getAgentVersionUnmetPoints.mockResolvedValue({
      items: [
        {
          case_revision_id: "aec_1",
          case_key: "east-china",
          case_label: "How did East China do?",
          point_id: "p_0a1b",
          kind: "judgment",
          text: "Compares with the same month last year",
          run_id: "aer_1",
          attempt: 2,
          quote: "Revenue was flat.",
          attempts: 2,
        },
      ],
    });
    renderIntl(<UnmetPointsList agentId="osa_1" candidateId="arc_1" executionId="aex_1" />, { locale: "en-US" });

    const list = await screen.findByRole("list", { name: "Points not met" });
    expect(list.textContent).toContain("How did East China do?");
    expect(list.textContent).toContain("Revenue was flat.");
    api.reportAgentEvalPointJudgedWrongly.mockResolvedValue({ rewording: null });
    fireEvent.click(within(list).getByRole("button", { name: "Judged wrongly" }));
    await waitFor(() => expect(api.reportAgentEvalPointJudgedWrongly).toHaveBeenCalledWith("osa_1", "p_0a1b", { run_id: "aer_1", case_revision_id: "aec_1", attempt: 2 }));
  });

  it("points to the cases when the list can't be loaded", async () => {
    api.getAgentVersionUnmetPoints.mockRejectedValue(new Error("offline"));
    renderIntl(<UnmetPointsList agentId="osa_1" candidateId="arc_1" executionId="aex_1" />, { locale: "en-US" });

    expect(await screen.findByText("The points not met couldn't be loaded. Open a case to see them.")).toBeTruthy();
  });
});
