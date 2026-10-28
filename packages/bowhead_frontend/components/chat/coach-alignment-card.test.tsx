import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import {
  getAgentEvalAlignment,
  labelAgentEvalAlignment,
  repinAgentEvalAlignment,
  rewordAgentEvalAlignmentPoint,
  type AlignmentLoop,
  type AlignmentQuestion,
  type AlignmentReport,
} from "@/lib/agent-eval-alignment-api";
import { ApiError } from "@/lib/api-client";
import {
  projectCoachAlignmentCard,
  resetCoachAlignmentCardsForTest,
  subscribeCoachAlignmentContinue,
} from "@/lib/coach-alignment-cards";
import { dispatchToTurnStore } from "@/lib/turn-store-dispatch";
import { setAgentEvalAlignmentCapabilityForTest } from "@/lib/use-agent-eval-alignment-capability";
import { CoachAlignmentCards } from "./coach-alignment-card";

const modelAccess = vi.hoisted(() => ({ canConfigure: false }));
vi.mock("@/lib/use-can-configure-models", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-can-configure-models")>(),
  useCanConfigureModels: () => modelAccess.canConfigure,
}));

vi.mock("@/lib/agent-eval-alignment-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agent-eval-alignment-api")>()),
  getAgentEvalAlignment: vi.fn(),
  labelAgentEvalAlignment: vi.fn(),
  rewordAgentEvalAlignmentPoint: vi.fn(),
  repinAgentEvalAlignment: vi.fn(),
}));
vi.mock("@/lib/agents-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agents-api")>()),
  getAgentEvalManager: vi.fn().mockResolvedValue({ suites: [{ revision: { cases: [{ case_key: "quality.2", task: "Top 5 stores by return rate", expected_contract: {} }] } }] }),
}));

const LOOP_ID = `aeal_${"a".repeat(32)}`;
const TURN = "t_1";
const SESSION = "s_1";

function question(index: number, change: Partial<AlignmentQuestion> = {}): AlignmentQuestion {
  return {
    question_id: `aeaq_${String(index).repeat(32)}`,
    point_id: "p_0001",
    card_seq: 1,
    row_index: index,
    answer_ref: `turn:t_${index}`,
    generated: false,
    question: `Question ${index}`,
    answer_excerpt: `East China sales were 48.1 million in September (answer ${index}).`,
    verdict: { met: true, quote: "in September", reason: null },
    pick_reason: "recent",
    replacement: false,
    label: null,
    label_via: null,
    labelled_at: null,
    ...change,
  };
}

function loop(change: Partial<AlignmentLoop> = {}, point: Partial<AlignmentLoop["points"][number]> = {}): AlignmentLoop {
  return {
    kind: "agent_eval_alignment_loop",
    schema_version: 1,
    loop_id: LOOP_ID,
    agent_id: "osa_1",
    suite_id: "aes_1",
    state: "aligning",
    owner_words: "It never says which period the numbers cover",
    words_by: "u_owner",
    origin_turn_id: null,
    points: [{
      point_id: "p_0001",
      kind: "judgment",
      text: "States the period a number covers",
      previous_text: null,
      origin: { words: "It never says which period", by: "u_owner", at: "2026-10-19T10:00:00Z", loop_id: LOOP_ID },
      status: "aligning",
      rewrites: 0,
      outcome: "continue",
      next_step: "wait_for_labels",
      recheck_question_ids: [],
      labels: { right: 0, wrong: 0, unclear: 0 },
      case_key: null,
      case_revision_id: null,
      ...point,
    }],
    questions: [
      question(1, { verdict: { met: false, quote: null, reason: "no period given" }, pick_reason: "marked_bad" }),
      question(2),
      question(3, { generated: true, pick_reason: "counterexample", verdict: { met: false, quote: null, reason: "no period given" } }),
    ],
    report: null,
    ...change,
  };
}

function project(pointer: Record<string, unknown>, notice: string | null = null, requestId = "1".repeat(32)) {
  act(() => {
    expect(dispatchToTurnStore("coach.alignment_card", {
      request_id: requestId, turn_id: TURN, user_id: "u_owner", team_id: "team_1", workspace_id: "ws_1", session_id: SESSION,
      pointer, notice,
    })).toBe(true);
  });
}

const CARD = { kind: "agent_eval_alignment_card", schema_version: 1, agent_id: "osa_1", loop_id: LOOP_ID, point_id: "p_0001", card_seq: 1 };
const REPORT_ID = `aealr_${"c".repeat(32)}`;
const REPORT = { kind: "agent_eval_alignment_report", agent_id: "osa_1", loop_id: LOOP_ID, report_id: REPORT_ID };

function render(locale: "en-US" | "zh-CN" = "en-US") {
  return renderIntl(<CoachAlignmentCards turnId={TURN} sessionId={SESSION} />, { locale });
}

describe("Coach alignment question card", () => {
  const sent: string[] = [];
  let unsubscribe: () => void = () => undefined;
  beforeEach(() => {
    resetCoachAlignmentCardsForTest();
    sent.length = 0;
    unsubscribe = subscribeCoachAlignmentContinue((request) => { sent.push(request.text); return true; });
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop());
  });
  afterEach(() => {
    unsubscribe();
    cleanup();
    vi.clearAllMocks();
  });

  it("with the alignment switch off a replayed pointer shows one neutral line and reads nothing", () => {
    setAgentEvalAlignmentCapabilityForTest(false);
    project(CARD, "held_out_once_per_point");
    project(REPORT, null, "2".repeat(32));
    render();
    expect(screen.getAllByText("This check is unavailable")).toHaveLength(1);
    expect(screen.queryByTestId("alignment-card")).toBeNull();
    expect(getAgentEvalAlignment).not.toHaveBeenCalled();
  });

  it("names who gave a shared Turn its thumbs down, and no one for the Owner's own", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({
      questions: [
        question(1, { pick_reason: "marked_bad", shared_by: "陈晓" }),
        question(2, { pick_reason: "feedback_conflict", shared_by: null }),
        question(3, { generated: true, pick_reason: "counterexample", verdict: { met: false, quote: null, reason: "no period given" } }),
      ],
    }));
    project(CARD);
    render("zh-CN");
    const card = await screen.findByTestId("alignment-card");
    expect(within(card).getByText("陈晓点了踩", { exact: false })).toBeTruthy();
    expect(within(card).getByText("被点了踩", { exact: false })).toBeTruthy();
  });

  it("ignores pointers that are not canonical", () => {
    project({ ...CARD, answer_excerpt: "leaked" });
    render();
    expect(screen.queryByTestId("alignment-card")).toBeNull();
    expect(getAgentEvalAlignment).not.toHaveBeenCalled();
  });

  it("sends only the rows the Owner marked plus rest_right, then tells Coach to continue", async () => {
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "rewrite" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [],
      loop: loop({ questions: loop().questions.map((row) => ({ ...row, label: "right", label_via: "rest_right" })) }, { outcome: "rewrite", next_step: "propose_rewrite" }),
    });
    project(CARD, "held_out_once_per_point");
    render();
    const card = await screen.findByTestId("alignment-card");
    expect(within(card).getByText("States the period a number covers")).toBeTruthy();
    expect(within(card).getAllByText("Coach thinks it doesn’t: no period given")).toHaveLength(2);
    expect(within(card).getByText("Edited by Coach", { exact: false })).toBeTruthy();
    expect(within(card).getByText("Got a thumbs down", { exact: false })).toBeTruthy();
    // The quote is highlighted inside the excerpt.
    expect(card.querySelector("mark")?.textContent).toBe("in September");
    expect(within(card).getByText(/One case per point is held out/)).toBeTruthy();

    const row = screen.getByTestId(`alignment-choice-${question(2).question_id}`);
    fireEvent.click(within(row).getByRole("radio", { name: "Wrong" }));
    expect(screen.getByRole("button", { name: "The other 2 are right" })).toBeTruthy();
    fireEvent.click(screen.getByTestId("alignment-rest-right"));

    await waitFor(() => expect(labelAgentEvalAlignment).toHaveBeenCalledWith("osa_1", LOOP_ID, {
      labels: [{ question_id: question(2).question_id, label: "wrong" }],
      rest_right: true,
      card_seq: 1,
    }));
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
    // The message carries the server's outcome, so Coach continues from it rather than guessing.
    expect(sent).toEqual(["I’ve marked the answers. I disagreed with a verdict, so the point needs rewording."]);
    // The collapsed card says where the point stands and the one next step, not just a count.
    expect(screen.getByTestId("alignment-card-step").textContent).toBe(
      "You disagreed with some verdicts. Next: Coach rewrites this point and asks you to check it again.",
    );
  });

  it("says the point still needs a counterexample, and offers to send it again when the send didn't happen", async () => {
    unsubscribe();
    const unsent: Array<() => void> = [];
    unsubscribe = subscribeCoachAlignmentContinue((request) => {
      sent.push(request.text);
      if (request.onUnsent) unsent.push(request.onUnsent);
      return true;
    });
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "need_counterexample" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [],
      loop: loop({ questions: loop().questions.map((row) => ({ ...row, label: "right", label_via: "rest_right" })) }, { outcome: "need_counterexample", next_step: "ask_counterexample" }),
    });
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
    expect(sent).toEqual(["I’ve marked the answers. The point isn’t confirmed yet: it still needs a check against an answer that misses it."]);
    expect(screen.getByTestId("alignment-card-step").textContent).toBe(
      "One answer that misses the point is still needed. Next: Coach brings one for you to check.",
    );
    expect(screen.queryByRole("button", { name: "Send to Coach" })).toBeNull();
    act(() => unsent[0]());
    expect(await screen.findByText(/Not sent to Coach yet; your draft in the message box is kept\./)).toBeTruthy();
    // One action sends the same message again once the Owner is ready.
    fireEvent.click(screen.getByRole("button", { name: "Send to Coach" }));
    expect(sent).toEqual([
      "I’ve marked the answers. The point isn’t confirmed yet: it still needs a check against an answer that misses it.",
      "I’ve marked the answers. The point isn’t confirmed yet: it still needs a check against an answer that misses it.",
    ]);
    expect(screen.queryByRole("button", { name: "Send to Coach" })).toBeNull();
  });

  it("says more answers are needed when every row is labelled but the point can't confirm yet", async () => {
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "continue" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [],
      loop: loop({ questions: loop().questions.map((row) => ({ ...row, label: "right", label_via: "rest_right" })) }, { next_step: "ask_more_answers" }),
    });
    project(CARD);
    render("zh-CN");
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
    // A bare "I've marked the answers" let the Lead call the point confirmed; the message says it isn't, in plain words.
    expect(sent).toEqual(["我标好了。这条要点还没确认：这几条回答还不足以确认它，请再拿几条回答问我。"]);
    expect(screen.getByTestId("alignment-card-step").textContent).toBe("这几条回答还不足以确认要点。下一步：Coach 会再拿几条回答请你核对。");
  });

  it("says the same next step when the settled card is replayed", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(
      loop({ questions: loop().questions.map((row) => ({ ...row, label: "right", label_via: "rest_right" })) }, { status: "confirmed", outcome: "confirmed", next_step: "propose_cases" }),
    );
    project(CARD);
    render();
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
    expect(screen.getByTestId("alignment-card-step").textContent).toBe(
      "Point confirmed. Next: Coach suggests test cases from it, and you decide whether to add them.",
    );
  });

  it("shows an answer's words, not its markdown, and keeps the quote highlighted", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({
      questions: [
        question(1, { answer_excerpt: "**East China** sales were 48.1 million *in September*.", verdict: { met: true, quote: "*in September*", reason: null } }),
        question(2),
        question(3, { generated: true, pick_reason: "counterexample", verdict: { met: false, quote: null, reason: "no period given" } }),
      ],
    }));
    project(CARD, "held_out_once_per_point");
    render();
    const row = await screen.findByTestId(`alignment-row-${question(1).question_id}`);
    expect(row.textContent).toContain("East China sales were 48.1 million in September.");
    expect(row.textContent).not.toContain("**");
    expect(row.querySelector("mark")?.textContent).toBe("in September");
    // The held-out notice and its link read as two sentences.
    expect(screen.getByText(/One case per point is held out/).textContent).toMatch(/the Agent\. About held-out cases$/);
  });

  it("one click when every verdict is right", async () => {
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "confirmed" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [],
      loop: loop({}, { status: "confirmed", outcome: "confirmed" }),
    });
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    await waitFor(() => expect(labelAgentEvalAlignment).toHaveBeenCalledWith("osa_1", LOOP_ID, { labels: [], rest_right: true, card_seq: 1 }));
    expect(screen.queryByText(/One case per point is held out/)).toBeNull();
  });

  it("asks about the counterexample alone when the batch passed it", async () => {
    const counter = question(3, { generated: true, pick_reason: "counterexample", verdict: { met: true, quote: null, reason: null }, label: "right", label_via: "rest_right" });
    const rows = [question(1, { label: "right", label_via: "rest_right" }), question(2, { label: "right", label_via: "rest_right" }), counter];
    vi.mocked(labelAgentEvalAlignment)
      .mockResolvedValueOnce({
        outcomes: { p_0001: "recheck_row" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [counter.question_id],
        loop: loop({ questions: rows }, { outcome: "recheck_row", recheck_question_ids: [counter.question_id] }),
      })
      .mockResolvedValueOnce({
        outcomes: { p_0001: "confirmed" }, confirmed: {}, dropped_question_ids: [], recheck_question_ids: [],
        loop: loop({ questions: rows.map((row) => ({ ...row, label_via: "row" })) }, { status: "confirmed" }),
      });
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    const recheck = await screen.findByTestId("alignment-card-recheck");
    expect(within(recheck).getByText("Could you take another look at this one?")).toBeTruthy();
    fireEvent.click(within(recheck).getByRole("radio", { name: "Right" }));
    fireEvent.click(screen.getByTestId("alignment-recheck-submit"));
    await waitFor(() => expect(labelAgentEvalAlignment).toHaveBeenLastCalledWith("osa_1", LOOP_ID, {
      labels: [{ question_id: counter.question_id, label: "right" }],
      rest_right: false,
      card_seq: 1,
    }));
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
  });

  it("explains a card whose point changed elsewhere and offers to check again", async () => {
    vi.mocked(labelAgentEvalAlignment).mockRejectedValue(new ApiError(409, "changed", {
      code: "agent_eval_alignment_point_changed", details: { question_ids: [question(1).question_id] },
    }));
    project(CARD);
    render("zh-CN");
    fireEvent.click(await screen.findByRole("button", { name: "其余 3 条都判对了" }));
    expect(await screen.findByText("这张卡片已过期，要点改过了。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新核对" }));
    expect(sent).toEqual(["要点改过了，请按现在的说法重新核对。"]);
  });

  it("says when the point is already confirmed", async () => {
    vi.mocked(labelAgentEvalAlignment).mockRejectedValue(new ApiError(409, "resolved", { code: "agent_eval_alignment_point_resolved" }));
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({}, { status: "confirmed" }));
    expect(await screen.findByText("This point is already confirmed.")).toBeTruthy();
  });

  it("keeps the Owner's choices and says what failed on other errors", async () => {
    vi.mocked(labelAgentEvalAlignment).mockRejectedValue(new ApiError(503, "The service is busy"));
    project(CARD);
    render();
    const row = await screen.findByTestId(`alignment-choice-${question(1).question_id}`);
    fireEvent.click(within(row).getByRole("radio", { name: "Not sure" }));
    fireEvent.click(screen.getByTestId("alignment-rest-right"));
    expect(await screen.findByText(/Couldn’t save your answers \(The service is busy\)/)).toBeTruthy();
    expect(within(row).getByRole("radio", { name: "Not sure" }).getAttribute("aria-checked")).toBe("true");
  });

  it("gives actionable copy for a point clash, a missing point and a full card", async () => {
    project(CARD);
    render();
    vi.mocked(labelAgentEvalAlignment).mockRejectedValueOnce(new ApiError(409, "conflict", { code: "agent_eval_alignment_point_conflict" }));
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    expect(await screen.findByText(/clashes with a point already in the suite/)).toBeTruthy();
    vi.mocked(labelAgentEvalAlignment).mockRejectedValueOnce(new ApiError(404, "gone", { code: "agent_eval_alignment_point_not_found" }));
    fireEvent.click(screen.getByTestId("alignment-rest-right"));
    expect(await screen.findByText(/no longer in this check/)).toBeTruthy();
    vi.mocked(labelAgentEvalAlignment).mockRejectedValueOnce(new ApiError(409, "full", { code: "agent_eval_alignment_card_full", details: { limit: 5 } }));
    fireEvent.click(screen.getByTestId("alignment-rest-right"));
    expect(await screen.findByText(/already has 5 answers/)).toBeTruthy();
  });

  it("copes with rows dropped anywhere in the loop by a labels call", async () => {
    const remaining = loop({ questions: [question(2, { label: "right", label_via: "rest_right" }), question(3, { generated: true, pick_reason: "counterexample", label: "right", label_via: "rest_right", verdict: { met: false, quote: null, reason: null } })] });
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "continue" }, confirmed: {}, dropped_question_ids: [question(1).question_id, `aeaq_${"9".repeat(32)}`], recheck_question_ids: [], loop: remaining,
    });
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    const settled = await screen.findByTestId("alignment-card-settled");
    expect(within(settled).getByText(/1 answer can no longer be read/)).toBeTruthy();
  });

  it("reads the loop again before submitting and settles when a newer card exists", async () => {
    project(CARD);
    render();
    const button = await screen.findByTestId("alignment-rest-right");
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ questions: [...loop().questions, question(5, { card_seq: 2, row_index: 1 })] }));
    fireEvent.click(button);
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
    expect(labelAgentEvalAlignment).not.toHaveBeenCalled();
  });

  it("settles on card_stale and points to the newer card", async () => {
    vi.mocked(labelAgentEvalAlignment).mockRejectedValue(new ApiError(409, "stale", { code: "agent_eval_alignment_card_stale", details: { card_seq: 2 } }));
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    expect(await screen.findByText("There’s a newer card for this point below; answer that one.")).toBeTruthy();
  });

  it("shows a reworded card as settled instead of asking about verdicts on the old wording", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ questions: loop().questions.map((row) => ({ ...row, stale: true })) }));
    project(CARD);
    render();
    expect(await screen.findByText(/The point’s wording changed/)).toBeTruthy();
    expect(screen.queryByTestId("alignment-rest-right")).toBeNull();
  });

  it("says when the check has ended or no longer exists, rather than Owner-only", async () => {
    vi.mocked(labelAgentEvalAlignment).mockRejectedValue(new ApiError(409, "closed", { code: "agent_eval_alignment_closed" }));
    project(CARD);
    const view = render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    expect(await screen.findByText("This check has ended. Ask Coach to start again.")).toBeTruthy();
    view.unmount();
    resetCoachAlignmentCardsForTest();
    vi.mocked(getAgentEvalAlignment).mockRejectedValue(new ApiError(404, "missing", { code: "agent_eval_alignment_not_found" }));
    project(CARD, null, "4".repeat(32));
    render();
    expect(await screen.findByText("This check no longer exists. Ask Coach to start again.")).toBeTruthy();
    expect(screen.queryByText("Only the Agent’s owner can answer this card.")).toBeNull();
  });

  it("reads the loop again when a labels call drops rows", async () => {
    vi.mocked(labelAgentEvalAlignment).mockResolvedValue({
      outcomes: { p_0001: "continue" }, confirmed: {}, dropped_question_ids: [`aeaq_${"9".repeat(32)}`], recheck_question_ids: [], loop: loop(),
    });
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-rest-right"));
    await waitFor(() => expect(vi.mocked(getAgentEvalAlignment).mock.calls.length).toBeGreaterThanOrEqual(3));
  });

  it("rewords the point on the card and restarts it", async () => {
    vi.mocked(rewordAgentEvalAlignmentPoint).mockResolvedValue(loop({}, { text: "States the month or date range", previous_text: "States the period a number covers" }));
    project(CARD);
    render();
    fireEvent.click(await screen.findByTestId("alignment-reword"));
    fireEvent.change(screen.getByRole("textbox", { name: "Evaluation point" }), { target: { value: "  States the month   or date range " } });
    fireEvent.click(screen.getByTestId("alignment-reword-save"));
    await waitFor(() => expect(rewordAgentEvalAlignmentPoint).toHaveBeenCalledWith("osa_1", LOOP_ID, "p_0001", "States the month or date range"));
    expect(sent).toEqual(["I reworded the point to: States the month or date range"]);
  });

  it("keeps the original wording by rewording back to previous_text", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({}, { text: "Names both periods it compares", previous_text: "Says which period it compares with", rewrites: 1 }));
    vi.mocked(rewordAgentEvalAlignmentPoint).mockResolvedValue(loop());
    project(CARD);
    render();
    const card = await screen.findByTestId("alignment-card");
    expect(card.querySelector("del")?.textContent).toBe("Says which period it compares with");
    expect(card.querySelector("ins")?.textContent).toBe("Names both periods it compares");
    fireEvent.click(screen.getByTestId("alignment-keep-original"));
    await waitFor(() => expect(rewordAgentEvalAlignmentPoint).toHaveBeenCalledWith("osa_1", LOOP_ID, "p_0001", "Says which period it compares with"));
  });

  it("handles a card that shrank between reads", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ questions: [question(2)] }));
    project(CARD);
    render();
    const card = await screen.findByTestId("alignment-card");
    expect(within(card).getByText("1 answer")).toBeTruthy();
  });

  it("shows a read-only line to anyone who isn't the loop's Owner", async () => {
    vi.mocked(getAgentEvalAlignment).mockRejectedValue(new ApiError(404, "not found", { code: "agent_eval_action_denied" }));
    project(CARD);
    render();
    expect(await screen.findByText("Only the Agent’s owner can answer this card.")).toBeTruthy();
  });

  it("collapses an earlier pointer to the same card", async () => {
    project(CARD, null, "1".repeat(32));
    project(CARD, null, "2".repeat(32));
    render();
    expect(await screen.findByTestId("alignment-card")).toBeTruthy();
    expect(await screen.findByTestId("alignment-card-settled")).toBeTruthy();
  });
});

function measured(change: Partial<AlignmentReport> = {}): AlignmentReport {
  return {
    report_id: REPORT_ID,
    status: "measured",
    target_points: [{ point_id: "p_0001", text: "States the period", before: { met: 0, cases: 2 }, after: { met: 2, cases: 2 }, independent_check: true }],
    held_out: { cases: 1, before: { passing: 0, cases: 1 }, after: { passing: 1, cases: 1 }, regressions: 0 },
    regressions: ["quality.2"],
    to_read: [{ case_key: "quality.2", excerpt: "September 2026 East China sales were 48.1 million." }],
    ...change,
  };
}

describe("Coach alignment report card", () => {
  const sent: string[] = [];
  let unsubscribe: () => void = () => undefined;
  beforeEach(() => {
    resetCoachAlignmentCardsForTest();
    sent.length = 0;
    unsubscribe = subscribeCoachAlignmentContinue((request) => { sent.push(request.text); return true; });
  });
  afterEach(() => {
    unsubscribe();
    cleanup();
    vi.clearAllMocks();
  });

  it("shows counts, never a percentage, below three cases", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: measured() }));
    project(REPORT);
    render();
    const card = await screen.findByTestId("alignment-report");
    expect(within(card).getByText(/What you raised improved, but 1 other question got worse/)).toBeTruthy();
    const raised = screen.getByTestId("alignment-stat-p_0001");
    expect(within(raised).getByText("2/2")).toBeTruthy();
    expect(within(raised).getByText("Before: 0/2")).toBeTruthy();
    expect(within(screen.getByTestId("alignment-stat-held-out")).getByText("1/1")).toBeTruthy();
    expect(card.textContent).not.toMatch(/%/);
    expect(await within(card).findAllByText("Top 5 stores by return rate")).toHaveLength(2);
    fireEvent.click(screen.getByTestId("alignment-fix"));
    expect(sent).toEqual(["Please fix the case that got worse."]);
  });

  it("shows the API's rate from three cases", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: measured({
      target_points: [{ point_id: "p_0001", text: "States the period", before: { met: 1, cases: 4, rate: 0.25 }, after: { met: 3, cases: 4, rate: 0.75 }, independent_check: false }],
      held_out: { cases: 0, before: { passing: 0, cases: 0 }, after: { passing: 0, cases: 0 }, regressions: 0 },
      regressions: [],
      to_read: [],
    }) }));
    project(REPORT);
    render();
    const raised = await screen.findByTestId("alignment-stat-p_0001");
    expect(within(raised).getByText("75%")).toBeTruthy();
    expect(within(raised).getByText("Before: 25%")).toBeTruthy();
    expect(screen.queryByTestId("alignment-stat-held-out")).toBeNull();
    expect(screen.getByText("This point has no held-out case, so the change couldn’t be checked independently.")).toBeTruthy();
    expect(screen.queryByTestId("alignment-fix")).toBeNull();
  });

  it("offers a rerun with the current model when the judge changed, and says it needs a new check", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: { report_id: REPORT_ID, status: "judge_changed", next_action: "repin" } }));
    vi.mocked(repinAgentEvalAlignment).mockResolvedValue({ ...loop(), next_action: "request_check" });
    project(REPORT);
    render("zh-CN");
    expect(await screen.findByText("这次结果不算：评测模型在检查期间换了。")).toBeTruthy();
    expect(screen.getByText(/需要再做一次检查/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "用当前评测模型重跑" }));
    await waitFor(() => expect(repinAgentEvalAlignment).toHaveBeenCalledWith("osa_1", LOOP_ID));
    expect(sent).toEqual(["已改用当前评测模型，请重新检查。"]);
  });

  it("collapses a card for an older report instead of showing the newer numbers", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: { ...measured(), report_id: `aealr_${"b".repeat(32)}` } }));
    project({ ...REPORT, report_id: `aealr_${"a".repeat(32)}` });
    render();
    expect(await screen.findByText("These results were replaced by a newer check.")).toBeTruthy();
    expect(screen.queryByTestId("alignment-stat-p_0001")).toBeNull();
    cleanup();
    resetCoachAlignmentCardsForTest();
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: null }));
    project({ ...REPORT, report_id: `aealr_${"a".repeat(32)}` }, null, "5".repeat(32));
    render();
    expect(await screen.findByText("These results were replaced by a newer check.")).toBeTruthy();
  });

  it("explains a check without an evaluation model, and a loop with no check", async () => {
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: { report_id: REPORT_ID, status: "judge_unavailable" } }));
    project(REPORT);
    const view = render();
    expect(await screen.findByText(/no evaluation model was available/)).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Open model settings" })).toBeNull();
    view.unmount();
    resetCoachAlignmentCardsForTest();
    modelAccess.canConfigure = true;
    project(REPORT);
    const admin = render();
    expect((await screen.findByRole("link", { name: "Open model settings" })).getAttribute("href")).toBe("/team/settings/models/assignments/");
    modelAccess.canConfigure = false;
    admin.unmount();
    resetCoachAlignmentCardsForTest();
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: null }));
    const noCheck = { kind: "agent_eval_alignment_report", agent_id: "osa_1", loop_id: LOOP_ID };
    project(noCheck, null, "3".repeat(32));
    const second = render();
    expect(await screen.findByText("There’s no check to report yet.")).toBeTruthy();
    second.unmount();
    // A pointer without an id is current only while the loop has no measured report.
    resetCoachAlignmentCardsForTest();
    vi.mocked(getAgentEvalAlignment).mockResolvedValue(loop({ report: measured() }));
    project(noCheck, null, "6".repeat(32));
    render();
    expect(await screen.findByText("These results were replaced by a newer check.")).toBeTruthy();
  });

  it("ignores a report pointer whose id isn't a report id", () => {
    project({ ...REPORT, report_id: "not-a-report" });
    render();
    expect(getAgentEvalAlignment).not.toHaveBeenCalled();
  });
});
