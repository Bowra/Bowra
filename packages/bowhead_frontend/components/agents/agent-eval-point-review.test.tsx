import { createHash } from "node:crypto";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { getAgentEvalAlignment, getAgentEvalPointCalibration, reviewAgentEvalPoints, type AlignmentLoop, type PointCalibration } from "@/lib/agent-eval-alignment-api";
import { previewAgentEvalPoints, type AgentEvalPoint } from "@/lib/agents-api";
import { setAgentEvalAlignmentCapabilityForTest } from "@/lib/use-agent-eval-alignment-capability";
import { EvalCasePoints, pointsToReview, resetLoopReadsForTest } from "./agent-eval-points";

vi.mock("@/lib/agents-api", async (importOriginal) => ({
  agentEvalAttemptAnswerRef: (await importOriginal<typeof import("@/lib/agents-api")>()).agentEvalAttemptAnswerRef,
  previewAgentEvalPoints: vi.fn(),
  markAgentEvalPointGoodAnswer: vi.fn(),
}));
vi.mock("@/lib/agent-eval-alignment-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agent-eval-alignment-api")>()),
  reviewAgentEvalPoints: vi.fn(),
  getAgentEvalAlignment: vi.fn(),
  getAgentEvalPointCalibration: vi.fn(),
}));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => ({ me: { id: "u_owner" } }) }));

const LOOP_ID = `aeal_${"b".repeat(32)}`;
const ALIGNED: AgentEvalPoint = {
  id: "p_00aa",
  kind: "judgment",
  text: "States the period a number covers",
  source: "coach_alignment",
  state: "confirmed",
  origin: { words: "It never says which period", by: "u_owner", at: "2026-10-19T10:00:00Z", loop_id: LOOP_ID },
};

function Harness({ initial, onPoints, saved = null, onCheck }: {
  initial: AgentEvalPoint[];
  onPoints: (points: AgentEvalPoint[]) => void;
  saved?: { caseRevisionId: string; pointTexts: Record<string, string> } | null;
  onCheck?: (point: AgentEvalPoint) => void;
}) {
  const [points, setPoints] = useState(initial);
  return (
    <EvalCasePoints
      agentId="osa_1"
      caseNumber={1}
      task="How did East China sales change in September?"
      points={points}
      saved={saved}
      suiteId="aes_owner"
      onCheckOnAnswers={onCheck}
      disabled={false}
      onChange={(next) => { setPoints(next); onPoints(next); }}
    />
  );
}

describe("Coach point review on the evaluation tab", () => {
  beforeEach(() => {
    vi.mocked(previewAgentEvalPoints).mockResolvedValue({ kind: "agent_eval_point_preview", schema_version: 1, answer: null, points: [] });
  });
  afterEach(() => {
    resetLoopReadsForTest();
    cleanup();
    vi.clearAllMocks();
  });

  it("reviews only points the Owner added or edited, never aligned ones", () => {
    const added: AgentEvalPoint = { id: "p_0001", kind: "judgment", text: "Be professional", source: "owner", state: "confirmed" };
    expect(pointsToReview([added, ALIGNED], { p_0001: "added", p_00aa: "edited" }, {}, null).map((point) => point.id)).toEqual(["p_0001"]);
    expect(pointsToReview([added], { p_0001: "added" }, { p_0001: "Be professional" }, null)).toEqual([]);
    expect(pointsToReview([added], { p_0001: "added" }, {}, "p_0001")).toEqual([]);
  });

  it("shows one quiet line with a rewrite, and Keep as is stores the text hash for that exact text", async () => {
    vi.mocked(reviewAgentEvalPoints).mockImplementation(async (_agent, body) => ({
      status: "reviewed",
      points: body.points.map((point) => ({ id: point.id, problem: "vague", related_point_id: null, reason: "too vague", rewording: "Explains jargon in plain words" })),
    }));
    const onPoints = vi.fn();
    renderIntl(<Harness initial={[]} onPoints={onPoints} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Be professional" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText("This can’t be checked as written. Try: Explains jargon in plain words", {}, { timeout: 3000 })).toBeTruthy();
    expect(vi.mocked(reviewAgentEvalPoints).mock.calls[0][1]).toMatchObject({
      task: "How did East China sales change in September?",
      points: [{ kind: "judgment", text: "Be professional", review_dismissed_hash: null }],
      other_points: [],
    });

    fireEvent.click(screen.getByTestId("point-review-keep"));
    const expected = `sha256:${createHash("sha256").update("Be professional").digest("hex")}`;
    await waitFor(() => expect(onPoints.mock.calls.at(-1)?.[0][0].review_dismissed_hash).toBe(expected));
    expect(screen.queryByText(/can’t be checked as written/)).toBeNull();
  });

  it("applies Coach's rewording", async () => {
    vi.mocked(reviewAgentEvalPoints).mockImplementation(async (_agent, body) => ({
      status: "reviewed",
      points: body.points.map((point) => ({ id: point.id, problem: "copied_value", related_point_id: null, reason: null, rewording: "Gives this year's and last year's September sales" })),
    }));
    const onPoints = vi.fn();
    renderIntl(<Harness initial={[]} onPoints={onPoints} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Gives East China September sales of 48.12 million" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(await screen.findByTestId("point-review-use", {}, { timeout: 3000 }));
    expect(onPoints.mock.calls.at(-1)?.[0][0].text).toBe("Gives this year's and last year's September sales");
  });

  it("shows nothing when the review is unavailable", async () => {
    vi.mocked(reviewAgentEvalPoints).mockResolvedValue({ status: "unavailable", points: [] });
    renderIntl(<Harness initial={[]} onPoints={vi.fn()} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Be professional" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(reviewAgentEvalPoints).toHaveBeenCalled(), { timeout: 3000 });
    await waitFor(() => expect(screen.queryByText("Coach is reviewing this…")).toBeNull());
    expect(screen.queryByTestId("point-review-keep")).toBeNull();
  });

  it("shows where an aligned point came from and what confirmed it, without a member's comment", async () => {
    const loop = {
      loop_id: LOOP_ID,
      points: [],
      questions: [
        { question_id: "aeaq_1", point_id: "p_00aa", card_seq: 1, row_index: 1, question: "Online average order value lately", verdict: { met: true, quote: null, reason: null }, label: "wrong" },
        { question_id: "aeaq_2", point_id: "p_00aa", card_seq: 1, row_index: 2, question: "Q2", verdict: { met: true, quote: null, reason: null }, label: "right" },
        { question_id: "aeaq_3", point_id: "p_00aa", card_seq: 1, row_index: 3, question: "Q3", verdict: { met: false, quote: null, reason: null }, label: "right" },
      ],
    } as unknown as AlignmentLoop;
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop);
    const member: AgentEvalPoint = { ...ALIGNED, id: "p_00bb", text: "Names both periods", origin: { kind: "member_feedback", at: "2026-10-19T10:00:00Z", loop_id: LOOP_ID } };
    const legacy: AgentEvalPoint = { ...ALIGNED, id: "p_00cc", text: "Says where the data came from", origin: { words: "member's private comment", by: "u_member", at: "2026-10-19T10:00:00Z", loop_id: LOOP_ID } };
    renderIntl(<Harness initial={[ALIGNED, member, legacy]} onPoints={vi.fn()} />, { locale: "en-US" });
    expect(screen.getByText(/From what you told Coach on .*: “It never says which period”/)).toBeTruthy();
    expect(screen.getAllByText("From member feedback")).toHaveLength(2);
    expect(screen.queryByText(/member's private comment/)).toBeNull();
    expect(await screen.findByText("Checked on 3 answers; you said Coach judged 1 wrongly")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "View" }));
    expect(screen.getByText("Online average order value lately")).toBeTruthy();
    expect(screen.getByText("Coach: Does this")).toBeTruthy();
    expect(screen.getByText("You: Wrong")).toBeTruthy();
    expect(screen.getByText("Coach: 1 do this · 1 don’t")).toBeTruthy();
    expect(reviewAgentEvalPoints).not.toHaveBeenCalled();
  });

  it("with the alignment switch off shows no origin, confirmation basis or review", async () => {
    setAgentEvalAlignmentCapabilityForTest(false);
    vi.mocked(reviewAgentEvalPoints).mockResolvedValue({ status: "reviewed", points: [] });
    renderIntl(<Harness initial={[ALIGNED]} onPoints={vi.fn()} />, { locale: "en-US" });
    expect(screen.getByText(ALIGNED.text)).toBeTruthy();
    expect(screen.queryByText(/From what you told Coach/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Be professional" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    // Longer than the review delay: no request, no line, and no loop read for the confirmation basis.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    expect(reviewAgentEvalPoints).not.toHaveBeenCalled();
    expect(getAgentEvalAlignment).not.toHaveBeenCalled();
    expect(screen.queryByText(/Checked on/)).toBeNull();
    expect(screen.queryByText("Coach is reviewing this…")).toBeNull();
  });

  it("splits a point that covers two things into two points in its place", async () => {
    vi.mocked(reviewAgentEvalPoints).mockImplementation(async (_agent, body) => ({
      status: "reviewed",
      points: body.points.map((point) => ({
        id: point.id, problem: "multiple", related_point_id: null, reason: null, rewording: null, split: ["Sort by growth amount", "Say whether it is amount or rate"],
      })),
    }));
    const onPoints = vi.fn();
    renderIntl(<Harness initial={[]} onPoints={onPoints} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Sort by growth and say which measure" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/split, each is easier to judge: Sort by growth amount; Say whether it is amount or rate\./, {}, { timeout: 3000 })).toBeTruthy();
    fireEvent.click(screen.getByTestId("point-review-split"));
    const next = onPoints.mock.calls.at(-1)?.[0] as AgentEvalPoint[];
    expect(next.map((point) => [point.kind, point.text, point.state])).toEqual([
      ["judgment", "Sort by growth amount", "confirmed"],
      ["judgment", "Say whether it is amount or rate", "confirmed"],
    ]);
    expect(new Set(next.map((point) => point.id)).size).toBe(2);
    // The new points aren't reviewed again right away.
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(reviewAgentEvalPoints).toHaveBeenCalledTimes(1);
  });

  it("labels Coach's rewording as Use this rewrite", async () => {
    vi.mocked(reviewAgentEvalPoints).mockImplementation(async (_agent, body) => ({
      status: "reviewed",
      points: body.points.map((point) => ({ id: point.id, problem: "vague", related_point_id: null, reason: null, rewording: "Explains jargon" })),
    }));
    renderIntl(<Harness initial={[]} onPoints={vi.fn()} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "Add point" }));
    fireEvent.change(screen.getByLabelText("Point"), { target: { value: "Be professional" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("button", { name: "Use this rewrite" }, { timeout: 3000 })).toBeTruthy();
  });

  it("offers Check on Answers and View Calibration in the point menu", async () => {
    const calibration: PointCalibration = {
      kind: "agent_eval_point_calibration", suite_id: "aes_owner", point_id: ALIGNED.id,
      status: { counts: false, reason: "calibration_failed", rewording: "Names the month and year" },
      calibration: {
        state: "failed", failure_reason: "bad_judged_met", rewording: "Names the month and year", current_text: true,
        judge: { prompt_version: "agent-eval-judge@2", model_fingerprint: `sha256:${"a".repeat(64)}` }, updated_at: "2026-10-19T10:00:00Z",
      },
      judge: { prompt_version: "agent-eval-judge@2", model_fingerprint: `sha256:${"a".repeat(64)}` },
      judge_model_name: "qwen-max",
      examples: [
        { id: "aepx_1", kind: "good", label_source: "marked_good", answer_kind: "attempt", question: null, expected_met: true, graded_met: true, graded_current: true, created_at: "x" },
        { id: "aepx_2", kind: "bad", label_source: "coach_edit", answer_kind: "coach", question: null, expected_met: false, graded_met: true, graded_current: true, created_at: "x" },
      ],
    };
    vi.mocked(getAgentEvalPointCalibration).mockResolvedValue(calibration);
    const onCheck = vi.fn();
    const onPoints = vi.fn();
    renderIntl(<Harness initial={[ALIGNED]} onPoints={onPoints} onCheck={onCheck} saved={{ caseRevisionId: "aec_1", pointTexts: { [ALIGNED.id]: ALIGNED.text } }} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for this point" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Check on Answers" }));
    expect(onCheck).toHaveBeenCalledWith(expect.objectContaining({ id: ALIGNED.id }));

    fireEvent.click(screen.getByRole("button", { name: "More actions for this point" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "View Calibration" }));
    const panel = await screen.findByTestId(`calibration-${ALIGNED.id}`);
    expect(getAgentEvalPointCalibration).toHaveBeenCalledWith("osa_1", "aes_owner", ALIGNED.id);
    expect(panel.textContent).toContain("The grading check disagreed with a labeled example. This does not mean the Agent answered wrongly.");
    expect(panel.textContent).toContain("The answer you marked good");
    expect(panel.textContent).toContain("An answer Coach made worse");
    expect(panel.querySelector("[data-wrong]")?.textContent).toContain("An answer Coach made worse");
    expect(panel.textContent).toContain("Evaluation model: qwen-max");
    // Never a fingerprint or a prompt version.
    expect(panel.textContent).not.toMatch(/aaaaaaaa|agent-eval-judge|sha256/);
    fireEvent.click(screen.getByTestId("calibration-use"));
    expect(onPoints.mock.calls.at(-1)?.[0][0].text).toBe("Names the month and year");
  });

  it("with the alignment switch off hides both menu items and the split", async () => {
    setAgentEvalAlignmentCapabilityForTest(false);
    renderIntl(<Harness initial={[ALIGNED]} onPoints={vi.fn()} onCheck={vi.fn()} saved={{ caseRevisionId: "aec_1", pointTexts: { [ALIGNED.id]: ALIGNED.text } }} />, { locale: "en-US" });
    fireEvent.click(screen.getByRole("button", { name: "More actions for this point" }));
    expect(await screen.findByRole("menuitem", { name: "Edit" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "Check on Answers" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "View Calibration" })).toBeNull();
    expect(getAgentEvalPointCalibration).not.toHaveBeenCalled();
  });

  it("shows no model line when the evaluation model's name is unknown", async () => {
    vi.mocked(getAgentEvalPointCalibration).mockResolvedValue({
      kind: "agent_eval_point_calibration", suite_id: "aes_owner", point_id: ALIGNED.id,
      status: { counts: true, reason: null, rewording: null }, calibration: null,
      judge: { prompt_version: "agent-eval-judge@2", model_fingerprint: `sha256:${"b".repeat(64)}` }, judge_model_name: null, examples: [],
    });
    renderIntl(<Harness initial={[ALIGNED]} onPoints={vi.fn()} saved={{ caseRevisionId: "aec_1", pointTexts: { [ALIGNED.id]: ALIGNED.text } }} />, { locale: "zh-CN" });
    fireEvent.click(screen.getByRole("button", { name: "这条要点的更多操作" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "查看校准" }));
    const panel = await screen.findByTestId(`calibration-${ALIGNED.id}`);
    expect(panel.textContent).not.toContain("评测模型");
    expect(panel.textContent).not.toMatch(/bbbbbbbb|agent-eval-judge|sha256/);
  });
});
