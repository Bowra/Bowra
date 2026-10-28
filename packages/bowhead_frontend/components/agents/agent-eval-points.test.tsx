import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { markAgentEvalPointGoodAnswer, previewAgentEvalPoints, type AgentEvalPoint } from "@/lib/agents-api";
import { EvalCasePoints } from "./agent-eval-points";

const modelAccess = vi.hoisted(() => ({ canConfigure: false }));
vi.mock("@/lib/use-can-configure-models", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-can-configure-models")>(),
  useCanConfigureModels: () => modelAccess.canConfigure,
}));

vi.mock("@/lib/agents-api", async (importOriginal) => ({
  agentEvalAttemptAnswerRef: (await importOriginal<typeof import("@/lib/agents-api")>()).agentEvalAttemptAnswerRef,
  previewAgentEvalPoints: vi.fn(),
  markAgentEvalPointGoodAnswer: vi.fn(),
}));

const SUGGESTED: AgentEvalPoint = {
  id: "p_0001",
  kind: "judgment",
  text: "Compares with the same month last year",
  source: "coach_live_answer",
  state: "suggested",
};
const CONFIRMED: AgentEvalPoint = { id: "p_0002", kind: "data_fact", text: "States East China revenue", source: "owner", state: "confirmed" };

const ANSWER = {
  run_id: "aer_1",
  case_revision_id: "aec_1",
  attempt: 1,
  answer_ref: "attempt:aer_1:aec_1:1",
  answered_at: "2026-10-18T00:00:00Z",
  answer: "Revenue grew 12% on the same month last year.",
  answer_truncated: false,
};

/** The Live-answer preview runs once, when the Owner asks for it (PRD R27). */
function openPreview() {
  const button = screen.queryByRole("button", { name: "See how they're judged on the released version's answer" });
  if (button) fireEvent.click(button);
}

function renderPoints(props: Partial<Parameters<typeof EvalCasePoints>[0]> = {}) {
  const onChange = vi.fn();
  renderIntl(
    <EvalCasePoints
      agentId="osa_1"
      caseNumber={1}
      task="How did revenue change?"
      points={[SUGGESTED, CONFIRMED]}
      saved={null}
      disabled={false}
      onChange={onChange}
      {...props}
    />,
    { locale: "en-US" },
  );
  return onChange;
}

describe("EvalCasePoints", () => {
  beforeEach(() => {
    vi.mocked(previewAgentEvalPoints).mockResolvedValue({
      kind: "agent_eval_point_preview",
      schema_version: 1,
      answer: ANSWER,
      points: [{ id: "p_0001", met: true, quote: "Revenue grew 12% on the same month last year.", value: null, reason: null }],
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("previews only unconfirmed graded points, once, when the Owner asks", async () => {
    renderPoints();
    expect(previewAgentEvalPoints).not.toHaveBeenCalled();

    openPreview();

    expect(await screen.findByText("Met · Evidence: “Revenue grew 12% on the same month last year.”")).toBeTruthy();
    expect(previewAgentEvalPoints).toHaveBeenCalledTimes(1);
    expect(previewAgentEvalPoints).toHaveBeenCalledWith("osa_1", {
      task: "How did revenue change?",
      points: [{ id: "p_0001", kind: "judgment", text: "Compares with the same month last year" }],
    });
    expect(screen.getByText("Suggested by Coach from the released version's answer")).toBeTruthy();
    expect(screen.getByText("The released version's answer")).toBeTruthy();
  });

  it("confirms a suggested point and adds an Owner point as confirmed", async () => {
    const onChange = renderPoints();
    openPreview();
    await screen.findByText(/Met · Evidence/);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onChange).toHaveBeenLastCalledWith([{ ...SUGGESTED, state: "confirmed" }, CONFIRMED]);

    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "  Names the region with the largest change " } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    const added = onChange.mock.lastCall?.[0].at(-1) as AgentEvalPoint;
    expect(added).toMatchObject({ kind: "judgment", text: "Names the region with the largest change", source: "owner", state: "confirmed" });
    expect(added.id).toMatch(/^p_[0-9a-f]{8}$/);
  });

  it("shows whether a confirmed point counts, and why not", () => {
    renderPoints({ pointStatus: { p_0002: { counts: false, reason: "kind_not_validated", rewording: null } } });
    openPreview();

    expect(screen.getByText("For reference only: grading for this kind of point is still being validated.")).toBeTruthy();
    cleanup();

    // Until the dry run validates model grading, only a required action blocks a release.
    renderPoints({ pointStatus: { p_0002: { counts: true, reason: null, rewording: null } } });
    expect(screen.getByText("For reference")).toBeTruthy();
    expect(screen.queryByText("Blocks release")).toBeNull();
    cleanup();

    renderPoints({ pointStatus: { p_0002: { counts: false, reason: "judge_unavailable", rewording: null } } });
    openPreview();
    expect(screen.getByText(/the evaluation model is unavailable right now/)).toBeTruthy();
    // Only people who can configure models get the link.
    expect(screen.queryByRole("link", { name: "Check it in model settings" })).toBeNull();
    cleanup();
    modelAccess.canConfigure = true;
    renderPoints({ pointStatus: { p_0002: { counts: false, reason: "judge_unavailable", rewording: null } } });
    openPreview();
    expect(screen.getByRole("link", { name: "Check it in model settings" }).getAttribute("href")).toBe("/team/settings/models/assignments/");
    modelAccess.canConfigure = false;
    cleanup();

    renderPoints({ pointStatus: { p_0002: { counts: false, reason: "calibration_failed", rewording: "States East China revenue for Q3" } } });
    openPreview();
    expect(screen.getByText(/Coach suggests: “States East China revenue for Q3”/)).toBeTruthy();
  });

  it("badges every confirmed point with whether it blocks a release, and says so when adding one", () => {
    renderPoints({ pointStatus: { p_0002: { counts: true, reason: null, rewording: null } } });
    expect(screen.getAllByText("For reference")[0].className).toContain("ui-badge");
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    expect(screen.getByText("If it isn't met, it's shown for reference and doesn't block a release.")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Required action" }));
    expect(screen.getByText("Name a Tool; an answer that doesn't use it fails the point.")).toBeTruthy();
    expect(screen.getAllByText("Blocks release").length).toBeGreaterThan(0);
  });

  it("names the alternatives of a required action that any one Tool meets", () => {
    const either: AgentEvalPoint = {
      id: "p_0003",
      kind: "required_action",
      text: "Reads the sales data",
      source: "owner",
      state: "confirmed",
      action: { any_of: ["bi_query", "powerbi_query"] },
    };
    renderPoints({ points: [either], tools: [{ ref: "bi_query", name: "Query data" }, { ref: "powerbi_query", name: "Query Power BI" }] });
    openPreview();

    // Named the way the picker shows them, not by their identifiers.
    expect(screen.getByText("Any of: Query data, Query Power BI")).toBeTruthy();
    expect(screen.getByText("Blocks release")).toBeTruthy();
  });

  it("shows a data fact's stated value in the preview and never calls it met", async () => {
    const dataFact: AgentEvalPoint = { id: "p_0003", kind: "data_fact", text: "States East China revenue", source: "coach_task", state: "suggested" };
    vi.mocked(previewAgentEvalPoints).mockResolvedValue({
      kind: "agent_eval_point_preview",
      schema_version: 1,
      answer: ANSWER,
      points: [{ id: "p_0003", met: true, quote: "Revenue grew 12%", value: null, reason: null }],
    });
    renderPoints({ points: [dataFact] });
    openPreview();

    expect(await screen.findByText("The released version's answer doesn't state a value for this.")).toBeTruthy();
    expect(screen.queryByText(/^Met ·/)).toBeNull();
  });

  it("asks to save before keeping a good answer, then keeps it for every saved graded point", async () => {
    renderPoints();
    openPreview();
    fireEvent.click(await screen.findByRole("button", { name: "Mark as good answer" }));
    expect(screen.getByText("Save the suite first; a good answer is kept with saved points.")).toBeTruthy();
    expect(markAgentEvalPointGoodAnswer).not.toHaveBeenCalled();
    cleanup();

    vi.mocked(markAgentEvalPointGoodAnswer).mockResolvedValue({ kind: "agent_eval_point_example", schema_version: 1, id: "aepx_1", suite_id: "aes_1", point_id: "p_0001", example_kind: "good" });
    renderPoints({ saved: { caseRevisionId: "aec_saved", pointTexts: { p_0001: SUGGESTED.text, p_0002: CONFIRMED.text } } });
    openPreview();
    fireEvent.click(await screen.findByRole("button", { name: "Mark as good answer" }));

    expect(await screen.findByRole("button", { name: "Marked as a good answer" })).toBeTruthy();
    expect(markAgentEvalPointGoodAnswer).toHaveBeenCalledWith("osa_1", "p_0001", { case_revision_id: "aec_saved", answer_ref: "attempt:aer_1:aec_1:1" });
    expect(markAgentEvalPointGoodAnswer).toHaveBeenCalledWith("osa_1", "p_0002", { case_revision_id: "aec_saved", answer_ref: "attempt:aer_1:aec_1:1" });
  });

  it("says plainly when a good answer couldn't be kept", async () => {
    vi.mocked(markAgentEvalPointGoodAnswer).mockRejectedValue(new ApiError(404, "Not found"));
    renderPoints({ saved: { caseRevisionId: "aec_saved", pointTexts: { p_0002: CONFIRMED.text } } });
    openPreview();
    fireEvent.click(await screen.findByRole("button", { name: "Mark as good answer" }));

    await waitFor(() => expect(screen.getByText("The good answer couldn't be saved. Try again.")).toBeTruthy());
  });

  it("explains how to get a preview when the released version hasn't answered this question", async () => {
    vi.mocked(previewAgentEvalPoints).mockResolvedValue({ kind: "agent_eval_point_preview", schema_version: 1, answer: null, points: [] });
    renderPoints();
    openPreview();

    expect(await screen.findByText(/There's no answer from the released version to this question yet/)).toBeTruthy();
  });

  const TOOLS = [{ ref: "bi_query", name: "Query data" }, { ref: "powerbi_query", name: "Query Power BI" }, { ref: "python_sandbox", name: "Run Code" }];

  it("adds a required action by picking the Agent's own Tools by name, one or several alternatives", () => {
    const onChange = renderPoints({ points: [], tools: TOOLS });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.click(screen.getByRole("radio", { name: "Required action" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Reads the sales data" } });
    expect((screen.getByRole("button", { name: "Add" }) as HTMLButtonElement).disabled).toBe(true);
    // No free-text Tool names: only the Agent's Tools are offered, by their display names.
    expect(screen.queryByRole("textbox", { name: "Tool it must call" })).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Query data" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Query Power BI" }));
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange.mock.lastCall?.[0][0]).toMatchObject({ kind: "required_action", action: { any_of: ["bi_query", "powerbi_query"] } });
  });

  it("says how to add a required action when the Agent has no Tools", () => {
    renderPoints({ points: [], tools: [] });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.click(screen.getByRole("radio", { name: "Required action" }));
    expect(screen.getByText("This Agent has no Tools yet. Add one on Configure, or use a judgment criterion instead.")).toBeTruthy();
  });

  it("changes a point's kind and Tool in place, without deleting it", () => {
    const point: AgentEvalPoint = { id: "p_0005", kind: "required_action", text: "Computes the totals", source: "owner", state: "confirmed", action: { tool: "python_sandbox" } };
    const onChange = renderPoints({ points: [point], tools: TOOLS });
    expect(screen.getByText("Calls Run Code")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "More actions for this point" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit" }));
    fireEvent.click(screen.getByRole("radio", { name: "Judgment criterion" }));
    expect(onChange.mock.lastCall?.[0][0]).toMatchObject({ id: "p_0005", kind: "judgment", text: "Computes the totals", state: "confirmed" });
    expect(onChange.mock.lastCall?.[0][0].action).toBeUndefined();
  });

  it("turns a point into a required action only once its Tool is picked", () => {
    const onChange = renderPoints({ points: [CONFIRMED], tools: TOOLS });
    fireEvent.click(screen.getByRole("button", { name: "More actions for this point" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Edit" }));
    fireEvent.click(screen.getByRole("radio", { name: "Required action" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("Pick a Tool to make this point a required action.")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: "Run Code" }));
    expect(onChange.mock.lastCall?.[0][0]).toMatchObject({ id: "p_0002", kind: "required_action", action: { tool: "python_sandbox" } });
  });
});

