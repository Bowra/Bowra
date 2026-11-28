import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import zhMessages from "@/messages/zh-CN.json";
import {
  getAgentVersionWorkbench,
  getAgentWorkingCopy,
  publishAgentWorkingCopy,
  type AgentVersionWorkbench,
} from "@/lib/agents-api";
import { publishAgentTrialSendResponse } from "@/lib/agent-trial-notice";
import { publishAgentReleased } from "@/lib/agent-release-events";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import { reportTrialEditorDirtyFields } from "@/lib/agent-trial-editor-state";
import type { CoachEditorPort } from "@/lib/coach-canvas-target";
import { useAgentTrialChatController, type AgentTrialChatControllerOptions } from "./use-agent-trial-chat-controller";

vi.mock("@/lib/agents-api", () => ({
  getAgentVersionWorkbench: vi.fn(),
  getAgentWorkingCopy: vi.fn(),
  publishAgentWorkingCopy: vi.fn(),
}));
vi.mock("@/lib/composer-handoff", () => ({ handoffAgentToNewChat: vi.fn() }));

const WORKBENCH: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: { release_id: "arl_2", release_sequence: 2, revision_id: "arv_2" },
  candidate_identity: { audience_mode: "private", candidate_hash: "h1", candidate_id: "arc_1", revision_id: "arv_3", row_version: 4, state: "prepared" },
  next_draft_identity: { base_release_id: "arl_2", id: "awc_1", row_version: 3 },
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
  primary_action: { kind: "prepare_candidate", enabled: true, disabled_reason_codes: [] },
  secondary_actions: [],
};

const DETAIL = {
  aggregate: { name: "store_weekly", status: "enabled", lifecycle_state: "active" },
  live: { release_sequence: 2, listing: { display_name: "门店周报" } },
  working_copy: { id: "awc_1", row_version: 5, listing: { display_name: "门店周报" } },
};

const HANDOFF = { agentId: "osa_1", label: "门店周报", candidateId: "arc_1", revisionRef: "arv_3" };

function wrapper({ children }: { children: ReactNode }) {
  return <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{children}</NextIntlClientProvider>;
}

function options(overrides: Partial<AgentTrialChatControllerOptions> = {}): AgentTrialChatControllerOptions {
  return {
    enabled: true,
    handoff: HANDOFF,
    activeSessionId: null,
    sessionTrial: null,
    workspaceId: "ws_1",
    editorPort: () => null,
    openEditor: null,
    navigate: vi.fn(),
    resetKey: 0,
    startUseAvailable: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(getAgentVersionWorkbench).mockResolvedValue(WORKBENCH);
  vi.mocked(getAgentWorkingCopy).mockResolvedValue(DETAIL as never);
  vi.mocked(publishAgentWorkingCopy).mockResolvedValue({ candidate_id: "arc_9", revision_id: "arv_9" } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("useAgentTrialChatController", () => {
  it("is off for ordinary chats and while the capability is off", () => {
    const off = renderHook(() => useAgentTrialChatController(options({ enabled: false })), { wrapper });
    expect(off.result.current.value).toBeNull();
    const ordinary = renderHook(() => useAgentTrialChatController(options({ handoff: null })), { wrapper });
    expect(ordinary.result.current.value).toBeNull();
  });

  it("starts a new trial from the hand-off and binds it to the session its first message creates", async () => {
    const view = renderHook((props: AgentTrialChatControllerOptions) => useAgentTrialChatController(props), { wrapper, initialProps: options() });
    await waitFor(() => expect(view.result.current.value?.agentName).toBe("门店周报"));
    expect(view.result.current.value?.reference).toMatchObject({ candidateId: "arc_1", revisionRef: "arv_3" });
    let sent: unknown;
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(sent).toMatchObject({ agentId: "osa_1", candidateId: "arc_1", revisionRef: "arv_3", label: "门店周报" });
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
    view.rerender(options({ activeSessionId: "s_1" }));
    await waitFor(() => expect(view.result.current.value?.agentId).toBe("osa_1"));
    // "New work" afterwards opens an ordinary chat: the hand-off stays with s_1.
    view.rerender(options({ activeSessionId: null, resetKey: 1 }));
    expect(view.result.current.value).toBeNull();
  });

  it("saves only 工作说明 without asking, and freezes a draft saved after the trial's candidate (C5)", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, draft_ahead_of_candidate: true });
    const port: CoachEditorPort = {
      requestClose: vi.fn(),
      capture: () => ({ target: { kind: "agent", id: "osa_1", version: 1, live_release_id: "arl_2", working_copy: null }, dirty: true, pending_write: false }),
      dirtyFields: () => ["instructions"],
      save: vi.fn(async () => true),
    };
    const view = renderHook(() => useAgentTrialChatController(options({ editorPort: () => port })), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    let sent: unknown;
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(port.save).toHaveBeenCalledOnce();
    expect(publishAgentWorkingCopy).toHaveBeenCalledWith("osa_1", {
      working_copy_id: "awc_1",
      working_copy_row_version: 5,
      expected_eval_policy_revision_id: null,
      replace_candidate: { candidate_id: "arc_1", candidate_row_version: 4, candidate_hash: "h1" },
      release_note: null,
      workspace_id: "ws_1",
    }, expect.any(String));
    expect(sent).toMatchObject({ candidateId: "arc_9", revisionRef: "arv_9" });
    expect(view.result.current.value?.reference).toMatchObject({ candidateId: "arc_9" });
  });

  it("keeps the candidate under test (TD1) and shows the server's notice", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      draft_ahead_of_candidate: true,
      active_execution_summary: { can_cancel: true, execution_id: "aex_1", failure_code: null, finished_at: null, row_version: 1, started_at: null, status: "running" },
    });
    const view = renderHook(() => useAgentTrialChatController(options()), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    let sent: unknown;
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
    expect(sent).toMatchObject({ candidateId: "arc_1" });
    act(() => publishAgentTrialSendResponse({ agent_trial_notice: "agent_trial_candidate_under_test" }));
    expect(view.result.current.value?.notice).toEqual({ tone: "info", text: "测试进行中，这条消息用的是正在测试的草稿。" });
  });

  it("keeps the question and names the failing condition when the draft can't be saved", async () => {
    const port: CoachEditorPort = {
      requestClose: vi.fn(),
      capture: () => ({ target: { kind: "agent", id: "osa_1", version: 1, live_release_id: "arl_2", working_copy: null }, dirty: true, pending_write: false }),
      dirtyFields: () => ["instructions"],
      save: vi.fn(async () => false),
    };
    const view = renderHook(() => useAgentTrialChatController(options({ editorPort: () => port })), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    let sent: unknown = "unset";
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(sent).toBeNull();
    expect(view.result.current.value?.notice?.tone).toBe("error");
    expect(view.result.current.value?.notice?.text).toContain("没发出去：草稿没能保存");
  });

  it("keeps one Canvas scope from the first screen through the session its first message creates", async () => {
    const view = renderHook((props: AgentTrialChatControllerOptions) => useAgentTrialChatController(props), { wrapper, initialProps: options() });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    const scope = view.result.current.scopeKey;
    expect(scope).toMatch(/^agent_trial:/);
    expect(view.result.current.origin).toBe("handoff");
    await act(async () => { await view.result.current.value!.prepareSend(); });
    view.rerender(options({ activeSessionId: "s_1" }));
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    expect(view.result.current.scopeKey).toBe(scope);
  });

  it("follows the Canvas editor's unsaved fields for the composer hint", async () => {
    const view = renderHook(() => useAgentTrialChatController(options()), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    expect(view.result.current.value?.unsavedFields).toEqual([]);
    act(() => reportTrialEditorDirtyFields("osa_1", ["instructions"]));
    expect(view.result.current.value?.unsavedFields).toEqual(["instructions"]);
    act(() => reportTrialEditorDirtyFields("osa_1", []));
    expect(view.result.current.value?.unsavedFields).toEqual([]);
  });

  it("freezes an edit made inside the trial with the Agent's current evaluation Policy (F21)", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, draft_ahead_of_candidate: true, current_eval_policy_revision_id: "aepr_2" });
    const view = renderHook(() => useAgentTrialChatController(options()), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    let sent: unknown;
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(publishAgentWorkingCopy).toHaveBeenCalledWith("osa_1", expect.objectContaining({ expected_eval_policy_revision_id: "aepr_2" }), expect.any(String));
    expect(sent).toMatchObject({ candidateId: "arc_9" });
  });

  it("offers 开始使用 and 用已发布 only when the new chat admits the Agent (F22)", async () => {
    const off = renderHook(() => useAgentTrialChatController(options({ startUseAvailable: false })), { wrapper });
    await waitFor(() => expect(off.result.current.value).not.toBeNull());
    expect(off.result.current.value?.startReleased).toBeNull();
    expect(off.result.current.value?.askReleased).toBeNull();
    const on = renderHook(() => useAgentTrialChatController(options()), { wrapper });
    await waitFor(() => expect(on.result.current.value).not.toBeNull());
    on.result.current.value?.askReleased?.("本周销售怎么样？");
    expect(handoffAgentToNewChat).toHaveBeenCalledWith({ agentId: "osa_1", label: "门店周报", text: "本周销售怎么样？" });
  });

  it("ends at once when this page releases the Agent's draft (F25)", async () => {
    const view = renderHook(() => useAgentTrialChatController(options({ handoff: null, activeSessionId: "s_7", sessionTrial: { sessionId: "s_7", agentId: "osa_1" } })), { wrapper });
    await waitFor(() => expect(view.result.current.value?.liveVersion).toBe(2));
    expect(view.result.current.value?.endedVersion).toBeNull();
    const reads = vi.mocked(getAgentVersionWorkbench).mock.calls.length;
    // Another Agent's release changes nothing here.
    act(() => publishAgentReleased("osa_other"));
    expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(reads);
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      live_identity: { release_id: "arl_3", release_sequence: 3, revision_id: "arv_3" },
      candidate_identity: null,
      next_draft_identity: null,
      recent_trials: [{ session_id: "s_7", first_question: "q", last_message_at: "t", turn_count: 1, drafts_used: [], released_as: { release_id: "arl_3", release_sequence: 3 } }],
    });
    vi.mocked(getAgentWorkingCopy).mockResolvedValue({ ...DETAIL, live: { ...DETAIL.live, release_sequence: 3 }, working_copy: null } as never);
    // Mutation: drop the release subscription → the band stays live until a reload.
    act(() => publishAgentReleased("osa_1"));
    await waitFor(() => expect(view.result.current.value?.endedVersion).toBe(3));
    expect(view.result.current.value?.liveVersion).toBe(3);
  });

  it("reopens a trial session and ends it once its draft was released", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      candidate_identity: null,
      next_draft_identity: null,
      recent_trials: [{ session_id: "s_7", first_question: "q", last_message_at: "t", turn_count: 2, drafts_used: [], released_as: { release_id: "arl_3", release_sequence: 3 } }],
    });
    const view = renderHook(() => useAgentTrialChatController(options({ handoff: null, activeSessionId: "s_7", sessionTrial: { sessionId: "s_7", agentId: "osa_1" } })), { wrapper });
    await waitFor(() => expect(view.result.current.value?.endedVersion).toBe(3));
    expect(view.result.current.value?.agentName).toBe("门店周报");
    let sent: unknown = "unset";
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(sent).toBeNull();
    expect(view.result.current.value?.notice).toBeNull();
    // Released is not "gone": the ended state says so, not the withdrawn one.
    expect(view.result.current.value?.unavailable).toBe(false);
  });

  it("names the version in review from the first screen, from the hand-off (F27)", async () => {
    // Mutation: ignore the hand-off's kind → 「试用草稿」 and 「· 草稿」 until the first answer.
    // One hand-off object, as the page keeps it in state (a new object each render is a new hand-off).
    const handoff = { ...HANDOFF, candidateKind: "submitted" as const };
    const view = renderHook(() => useAgentTrialChatController(options({ handoff })), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    expect(view.result.current.value?.candidateKind).toBe("submitted");
  });

  it("names the version in review when a trial session on it is reopened, and keeps each trial's own kind", async () => {
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, candidate_identity: { ...WORKBENCH.candidate_identity!, state: "pending" } });
    const view = renderHook((props: AgentTrialChatControllerOptions) => useAgentTrialChatController(props), {
      wrapper, initialProps: options({ handoff: null, activeSessionId: "s_8", sessionTrial: { sessionId: "s_8", agentId: "osa_1" } }),
    });
    await waitFor(() => expect(view.result.current.value?.candidateKind).toBe("submitted"));
    // Another trial session of the same Agent whose next message would freeze the saved draft reads as the draft.
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, candidate_identity: null });
    act(() => publishAgentReleased("osa_1"));
    view.rerender(options({ handoff: null, activeSessionId: "s_9", sessionTrial: { sessionId: "s_9", agentId: "osa_1" } }));
    await waitFor(() => expect(view.result.current.value?.candidateKind).toBe("draft"));
  });

  it("says up front that a reopened trial whose draft was withdrawn can send nothing (F34)", async () => {
    // Withdrawn, and a different version released since: no candidate, and the saved draft equals the release.
    vi.mocked(getAgentVersionWorkbench).mockResolvedValue({
      ...WORKBENCH,
      candidate_identity: null,
      next_draft_identity: null,
      recent_trials: [{ session_id: "s_5", first_question: "q", last_message_at: "t", turn_count: 1, drafts_used: [], released_as: null }],
    });
    const reopened = renderHook(() => useAgentTrialChatController(options({ handoff: null, activeSessionId: "s_5", sessionTrial: { sessionId: "s_5", agentId: "osa_1" } })), { wrapper });
    await waitFor(() => expect(reopened.result.current.value?.unavailable).toBe(true));
    expect(reopened.result.current.value?.endedVersion).toBeNull();
    // The first screen of a new trial is never in that state, whatever the workbench says.
    const fresh = renderHook(() => useAgentTrialChatController(options()), { wrapper });
    await waitFor(() => expect(fresh.result.current.value).not.toBeNull());
    await waitFor(() => expect(getAgentVersionWorkbench).toHaveBeenCalledTimes(2));
    expect(fresh.result.current.value?.unavailable).toBe(false);
  });

  it("names why the editor's input can't be saved instead of asking to save it before sending (F8)", async () => {
    const port: CoachEditorPort = {
      requestClose: vi.fn(),
      capture: () => ({ target: { kind: "agent", id: "osa_1", version: 1, live_release_id: "arl_2", working_copy: null }, dirty: true, pending_write: false }),
      dirtyFields: () => ["audience"],
      saveBlockedProblem: () => "指定分享至少需要选择一名成员或一个群组。",
      save: vi.fn(async () => true),
    };
    const view = renderHook(() => useAgentTrialChatController(options({ editorPort: () => port })), { wrapper });
    await waitFor(() => expect(view.result.current.value).not.toBeNull());
    let sent: unknown = "unset";
    await act(async () => { sent = await view.result.current.value!.prepareSend(); });
    expect(sent).toBeNull();
    expect(port.save).not.toHaveBeenCalled();
    expect(view.result.current.dialogs).toBeNull();
    expect(view.result.current.value?.notice).toEqual({
      tone: "error",
      text: "没发出去：草稿还不能保存：指定分享至少需要选择一名成员或一个群组。你输入的问题还在。",
    });
  });
});
