import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { _resetAllTurnsForTesting, applyPeerAgentAttribution } from "@/lib/turn-store";
import { clearTurnTrialAttributionCache } from "@/lib/turn-trial-attribution";
import { getAgentEvalManager } from "@/lib/agents-api";
import {
  AgentTrialBand,
  AgentTrialChatContext,
  TrialAnswerActions,
  TrialAnswerLabel,
  TrialConfirmationLine,
  TrialDraftDivider,
  TrialFirstQuestions,
  useTrialSendHint,
  type AgentTrialChatValue,
} from "./agent-trial-chat";

const apiFetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  apiFetch,
}));
vi.mock("@/lib/agents-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agents-api")>()),
  getAgentEvalManager: vi.fn(),
}));

function suites(tasks: string[]) {
  return {
    suites: [
      { suite_kind: "platform_contract", state: "active", revision: { cases: [{ case_key: "p", task: "平台用例" }] } },
      { suite_kind: "owner_quality", state: "active", revision: { cases: tasks.map((task, index) => ({ case_key: `quality.${index + 1}`, task })) } },
    ],
  } as never;
}

function trial(overrides: Partial<AgentTrialChatValue> = {}): AgentTrialChatValue {
  return {
    agentId: "osa_1",
    agentName: "门店周报",
    reference: { agentId: "osa_1", label: "门店周报", candidateId: "arc_1", revisionRef: "arv_1" },
    candidateKind: "draft",
    liveVersion: 2,
    endedVersion: null,
    unavailable: false,
    notice: null,
    preparing: false,
    prepareSend: vi.fn(async () => null),
    backToAgent: vi.fn(),
    openEditor: vi.fn(),
    startReleased: vi.fn(),
    askReleased: vi.fn(),
    saveAsCase: vi.fn(async () => undefined),
    dismissNotice: vi.fn(),
    unsavedFields: [],
    ...overrides,
  };
}

function withTrial(value: AgentTrialChatValue | null, ui: React.ReactElement) {
  return renderIntl(<AgentTrialChatContext.Provider value={value}>{ui}</AgentTrialChatContext.Provider>);
}

function attribute(turnId: string, candidateId: string, frozenAt: string, candidateKind: "draft" | "submitted" = "draft") {
  act(() => {
    applyPeerAgentAttribution({ turnId, peerId: `p_${turnId}`, agentId: "osa_1", trial: { candidateId, candidateKind, frozenAt } });
  });
}

beforeEach(() => {
  apiFetch.mockResolvedValue(new Response(JSON.stringify({ executions: [] }), { status: 200 }));
});

afterEach(() => {
  cleanup();
  _resetAllTurnsForTesting();
  clearTurnTrialAttributionCache();
  vi.clearAllMocks();
});

describe("trial band", () => {
  it("says whose draft is tried, who sees it, that Tools run and credits are charged", () => {
    const value = trial();
    withTrial(value, <AgentTrialBand />);
    const band = screen.getByRole("region", { name: "试用说明" });
    expect(band.textContent).toContain("试用草稿 · 门店周报");
    expect(band.textContent).toContain("只有你能看到 · 工具会真实运行 · 按普通对话扣额度");
    fireEvent.click(screen.getByRole("button", { name: "回到智能体" }));
    expect(value.backToAgent).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "编辑草稿" }));
    expect(value.openEditor).toHaveBeenCalledOnce();
  });

  it("names the submitted version while the draft is in review", () => {
    withTrial(trial({ candidateKind: "submitted" }), <AgentTrialBand />);
    expect(screen.getByRole("region", { name: "试用说明" }).textContent).toContain("试用提交审批的版本 · 门店周报");
  });

  it("ends the trial once the draft was released, with 开始使用 vN", () => {
    const value = trial({ endedVersion: 3 });
    withTrial(value, <AgentTrialBand />);
    expect(screen.getByRole("region", { name: "试用说明" }).textContent).toContain("这份草稿已发布为 v3，这段试用不能再发消息");
    fireEvent.click(screen.getByRole("button", { name: "开始使用 v3" }));
    expect(value.startReleased).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "回到智能体" })).toBeNull();
  });

  it("keeps the ended sentence but offers no 开始使用 when the new chat can't take the Agent (F22)", () => {
    withTrial(trial({ endedVersion: 3, startReleased: null }), <AgentTrialBand />);
    expect(screen.getByRole("region", { name: "试用说明" }).textContent).toContain("这份草稿已发布为 v3，这段试用不能再发消息");
    expect(screen.queryByRole("button", { name: "开始使用 v3" })).toBeNull();
  });

  it("says up front why a trial whose draft is gone can't continue, and what to do (F34)", () => {
    const value = trial({ unavailable: true, liveVersion: 4 });
    withTrial(value, <><AgentTrialBand /><TrialFirstQuestions onPick={vi.fn()} /></>);
    const band = screen.getByRole("region", { name: "试用说明" });
    expect(band.getAttribute("data-state")).toBe("ended");
    expect(band.textContent).toContain("这段试用的草稿已撤回或被替换，现在的草稿和正在用的 v4 一样，不能再发消息。先在「配置」里改草稿，再从智能体页开始新的试用。");
    expect(band.textContent).not.toContain("试用草稿 · 门店周报");
    fireEvent.click(screen.getByRole("button", { name: "回到智能体" }));
    expect(value.backToAgent).toHaveBeenCalledOnce();
    expect(screen.queryByText("像成员一样问它一个问题")).toBeNull();
  });

  it("shows a refusal above the composer and lets the Owner dismiss it", () => {
    const value = trial({ notice: { tone: "error", text: "没发出去：草稿没能保存。" } });
    withTrial(value, <AgentTrialBand />);
    expect(screen.getByRole("alert").textContent).toContain("没发出去：草稿没能保存。");
    fireEvent.click(screen.getByRole("button", { name: "知道了" }));
    expect(value.dismissNotice).toHaveBeenCalledOnce();
  });

  it("renders nothing in an ordinary chat", () => {
    const { container } = withTrial(null, <><AgentTrialBand /><TrialAnswerLabel turnId="t_1" settled /><TrialAnswerActions turnId="t_1" question="q" /></>);
    expect(container.textContent).toBe("");
  });
});

describe("answer label and divider", () => {
  it("labels each answer with the draft it used, from the live attribution", () => {
    attribute("t_1", "arc_1", "2026-11-25T06:32:00Z");
    withTrial(trial(), <TrialAnswerLabel turnId="t_1" settled />);
    expect(screen.getByText(/^草稿 · \d{2}:\d{2} 的版本$/)).toBeTruthy();
  });

  it("labels an in-review answer as the submitted version", () => {
    attribute("t_1", "arc_1", "2026-11-25T09:47:00Z", "submitted");
    withTrial(trial(), <TrialAnswerLabel turnId="t_1" settled />);
    expect(screen.getByText(/^提交的版本 · \d{2}:\d{2}$/)).toBeTruthy();
  });

  it("reads a restored Turn's draft from its stored binding", async () => {
    apiFetch.mockResolvedValue(new Response(JSON.stringify({
      turn_id: "t_9",
      executions: [{ trial: { candidate_id: "arc_1", candidate_kind: "draft", frozen_at: "2026-11-25 06:32:00" } }],
    }), { status: 200 }));
    withTrial(trial(), <TrialAnswerLabel turnId="t_9" settled />);
    await screen.findByText(/^草稿 · \d{2}:\d{2} 的版本$/);
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/turns/t_9/agent-binding", expect.anything());
  });

  it("puts a divider between answers from different drafts only", () => {
    attribute("t_1", "arc_1", "2026-11-25T06:32:00Z");
    attribute("t_2", "arc_2", "2026-11-25T06:40:00Z");
    attribute("t_3", "arc_2", "2026-11-25T06:40:00Z");
    withTrial(trial(), <>
      <TrialDraftDivider previousTurnId="t_1" turnId="t_2" settled />
      <TrialDraftDivider previousTurnId="t_2" turnId="t_3" settled />
    </>);
    const dividers = screen.getAllByText(/^草稿已更新 · \d{2}:\d{2} 的版本 · 下面的回答用新草稿$/);
    expect(dividers).toHaveLength(1);
  });
});

describe("answer actions", () => {
  it("saves the Turn as a case and stays in the chat with a link to write its points", async () => {
    const value = trial();
    withTrial(value, <TrialAnswerActions turnId="t_1" question="本周销售情况怎么样？给我一份周报。" />);
    fireEvent.click(screen.getByRole("button", { name: "存为用例：本周销售情况怎么样？给我一份周报。" }));
    await waitFor(() => expect(value.saveAsCase).toHaveBeenCalledWith("t_1"));
    const saved = await screen.findByRole("status");
    expect(saved.textContent).toContain("已加到测试用例 · 去写要点");
    expect(screen.getByRole("link", { name: "去写要点" }).getAttribute("href")).toContain("/agents/manage/created/?agent_id=osa_1&section=evaluate");
  });

  it("names the failing condition when the case can't be saved", async () => {
    const value = trial({ saveAsCase: vi.fn(async () => { throw new Error("offline"); }) });
    withTrial(value, <TrialAnswerActions turnId="t_1" question="q" />);
    fireEvent.click(screen.getByRole("button", { name: "存为用例：q" }));
    expect((await screen.findByRole("alert")).textContent).toContain("没能存为用例");
  });

  it("asks the released version the same question through the ordinary hand-off", () => {
    const value = trial();
    withTrial(value, <TrialAnswerActions turnId="t_1" question="上周哪个品类销售额最高？" />);
    fireEvent.click(screen.getByRole("button", { name: "用已发布 v2 问同一个问题：上周哪个品类销售额最高？" }));
    expect(value.askReleased).toHaveBeenCalledWith("上周哪个品类销售额最高？");
  });

  it("hides 用已发布 vN when the new chat can't take the Agent (F22)", () => {
    withTrial(trial({ askReleased: null }), <TrialAnswerActions turnId="t_1" question="上周哪个品类销售额最高？" />);
    expect(screen.queryByRole("button", { name: /用已发布 v2 问同一个问题/ })).toBeNull();
    expect(screen.getByRole("button", { name: "存为用例：上周哪个品类销售额最高？" })).toBeTruthy();
  });
});

describe("hint, confirmation line and first questions", () => {
  function Hint() {
    return <p data-testid="hint">{useTrialSendHint() ?? "none"}</p>;
  }

  it("says that unsaved 工作说明 is saved on send, and names other fields that will ask first", () => {
    const view = withTrial(trial({ unsavedFields: ["instructions"] }), <Hint />);
    expect(screen.getByTestId("hint").textContent).toBe("右边「工作说明」有 1 处修改没保存。发送时会先保存，这条消息就用新草稿。");
    view.unmount();
    withTrial(trial({ unsavedFields: ["description", "audience"] }), <Hint />);
    expect(screen.getByTestId("hint").textContent).toBe("右边「一句话介绍」和「谁能用」有修改没保存。发送前会先问你要不要保存这些修改。");
  });

  it("has no hint when everything is saved or the trial ended", () => {
    const view = withTrial(trial(), <Hint />);
    expect(screen.getByTestId("hint").textContent).toBe("none");
    view.unmount();
    withTrial(trial({ unsavedFields: ["instructions"], endedVersion: 3 }), <Hint />);
    expect(screen.getByTestId("hint").textContent).toBe("none");
  });

  it("adds the trial line to a confirmation only inside a trial", () => {
    const view = withTrial(trial(), <TrialConfirmationLine />);
    expect(screen.getByText("这是试用，但确认后会真的执行。")).toBeTruthy();
    view.unmount();
    withTrial(null, <TrialConfirmationLine />);
    expect(screen.queryByText("这是试用，但确认后会真的执行。")).toBeNull();
  });

  it("offers up to two of the Owner's case questions; picking fills the composer", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue(suites(["本周销售情况怎么样？", "品类前 5 是哪些？", "第三个问题"]));
    const onPick = vi.fn();
    withTrial(trial(), <TrialFirstQuestions onPick={onPick} />);
    expect(screen.getByText("像成员一样问它一个问题")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "本周销售情况怎么样？" }));
    expect(onPick).toHaveBeenCalledWith("本周销售情况怎么样？");
    expect(screen.getByText("也可以用你的用例：")).toBeTruthy();
    expect(screen.getAllByRole("button")).toHaveLength(2);
    expect(screen.queryByText("平台用例")).toBeNull();
  });

  it("shows only the first line without cases", async () => {
    vi.mocked(getAgentEvalManager).mockResolvedValue(suites([]));
    withTrial(trial(), <TrialFirstQuestions onPick={vi.fn()} />);
    await waitFor(() => expect(getAgentEvalManager).toHaveBeenCalledWith("osa_1"));
    expect(screen.getByText("像成员一样问它一个问题")).toBeTruthy();
    expect(screen.queryByText("也可以用你的用例：")).toBeNull();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
