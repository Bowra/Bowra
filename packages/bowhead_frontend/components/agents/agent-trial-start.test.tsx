import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import {
  getAgentVersionWorkbench,
  getAgentWorkingCopy,
  publishAgentWorkingCopy,
  type AgentRecentTrial,
  type AgentVersionWorkbench,
} from "@/lib/agents-api";
import type { DraftFieldKey } from "@/lib/agent-trial";
import { AgentTrialList } from "./agent-trial-list";
import { useAgentTrialStart, type AgentTrialStartOptions } from "./agent-trial-start";

vi.mock("@/lib/agents-api", () => ({
  getAgentVersionWorkbench: vi.fn(),
  getAgentWorkingCopy: vi.fn(),
  publishAgentWorkingCopy: vi.fn(),
}));

const WORKBENCH: AgentVersionWorkbench = {
  schema_version: 1,
  current_eval_policy_revision_id: null,
  live_identity: { release_id: "arl_2", release_sequence: 2, revision_id: "arv_2" },
  candidate_identity: null,
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

function Harness(props: Partial<AgentTrialStartOptions> & { dirtyFields?: DraftFieldKey[] }) {
  const { start, busy, dialogs } = useAgentTrialStart({
    agentId: "osa_1",
    agentName: "门店周报",
    live: "v2",
    workspaceId: "ws_1",
    dirtyFields: [],
    savedDraftEqualsRelease: false,
    save: vi.fn(async () => true),
    ...props,
  });
  return <>
    <button type="button" disabled={busy} onClick={() => void start()}>试用草稿</button>
    {dialogs}
  </>;
}

function arrange(workbench: Partial<AgentVersionWorkbench> = {}) {
  vi.mocked(getAgentVersionWorkbench).mockResolvedValue({ ...WORKBENCH, ...workbench });
  vi.mocked(getAgentWorkingCopy).mockResolvedValue({ working_copy: { id: "awc_1", row_version: 5 } } as never);
  vi.mocked(publishAgentWorkingCopy).mockResolvedValue({ candidate_id: "arc_9", revision_id: "arv_9" } as never);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("starting a trial", () => {
  it("saves only 工作说明 without asking, freezes the draft and opens a trial chat", async () => {
    arrange();
    const save = vi.fn(async () => true);
    const openChat = vi.fn();
    renderIntl(<Harness dirtyFields={["instructions"]} save={save} openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    await waitFor(() => expect(openChat).toHaveBeenCalledWith({ agentId: "osa_1", label: "门店周报", trial: { candidateId: "arc_9", revisionRef: "arv_9", candidateKind: "draft" } }));
    expect(save).toHaveBeenCalledOnce();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(publishAgentWorkingCopy).toHaveBeenCalledWith(
      "osa_1",
      { working_copy_id: "awc_1", working_copy_row_version: 5, expected_eval_policy_revision_id: null, release_note: null, workspace_id: "ws_1" },
      expect.any(String),
    );
  });

  it("freezes with the Agent's current evaluation Policy (F21)", async () => {
    arrange({ current_eval_policy_revision_id: "aepr_3" });
    const openChat = vi.fn();
    renderIntl(<Harness openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    await waitFor(() => expect(openChat).toHaveBeenCalledOnce());
    expect(publishAgentWorkingCopy).toHaveBeenCalledWith("osa_1", expect.objectContaining({ expected_eval_policy_revision_id: "aepr_3" }), expect.any(String));
  });

  it("names a Policy that changed meanwhile instead of the generic sentence", async () => {
    arrange();
    vi.mocked(publishAgentWorkingCopy).mockRejectedValue(new ApiError(409, "agent_eval_policy_version_conflict", {
      code: "agent_eval_policy_version_conflict",
      details: { current_policy_revision_id: "aepr_4" },
    }));
    renderIntl(<Harness openChat={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("测试用例刚刚有更新，这次没能冻结草稿。再试一次");
    expect(dialog.textContent).not.toContain("草稿没能冻结成可以试用的版本");
  });

  it("names the save problem instead of offering 保存并试用 when the draft can't be saved (F8)", async () => {
    arrange();
    const save = vi.fn(async () => true);
    const onFix = vi.fn();
    renderIntl(<Harness
      dirtyFields={["audience"]}
      save={save}
      saveBlocked={{ problem: "指定分享至少需要选择一名成员或一个群组。", fixLabel: "去「谁能用」修改", onFix }}
      openChat={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("草稿还不能试用")).toBeTruthy();
    expect(dialog.textContent).toContain("草稿还不能保存，所以还没开始试用：指定分享至少需要选择一名成员或一个群组。");
    expect(within(dialog).queryByRole("button", { name: "保存并试用" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "去「谁能用」修改" }));
    expect(onFix).toHaveBeenCalledOnce();
    expect(save).not.toHaveBeenCalled();
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
  });

  it("doesn't say 另外 when 工作说明 is not among the unsaved fields", async () => {
    arrange();
    renderIntl(<Harness dirtyFields={["audience"]} openChat={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("先保存 1 项修改？")).toBeTruthy();
    expect(dialog.textContent).toContain("「谁能用」有没保存的修改");
    expect(dialog.textContent).not.toContain("也有");
  });

  it("asks before saving other fields, naming them", async () => {
    arrange();
    const save = vi.fn(async () => true);
    const openChat = vi.fn();
    renderIntl(<Harness dirtyFields={["instructions", "audience", "description"]} save={save} openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("先保存另外 2 项修改？")).toBeTruthy();
    expect(dialog.textContent).toContain("「一句话介绍」和「谁能用」也有没保存的修改");
    expect(within(dialog).getByRole("button", { name: "只用已保存的草稿" })).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "保存并试用" }));
    await waitFor(() => expect(openChat).toHaveBeenCalledOnce());
    expect(save).toHaveBeenCalledOnce();
  });

  it("hides 只用已保存的草稿 when the saved draft equals the release, and cancel stops the flow", async () => {
    arrange();
    const openChat = vi.fn();
    renderIntl(<Harness dirtyFields={["name"]} savedDraftEqualsRelease openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).queryByRole("button", { name: "只用已保存的草稿" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(getAgentVersionWorkbench).not.toHaveBeenCalled();
    expect(openChat).not.toHaveBeenCalled();
  });

  it("reuses the candidate in review instead of preparing another", async () => {
    arrange({
      candidate_identity: { audience_mode: "custom", candidate_hash: "h", candidate_id: "arc_1", revision_id: "arv_3", row_version: 2, state: "pending" },
      draft_ahead_of_candidate: true,
    });
    const openChat = vi.fn();
    renderIntl(<Harness openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    await waitFor(() => expect(openChat).toHaveBeenCalledWith(expect.objectContaining({ trial: { candidateId: "arc_1", revisionRef: "arv_3", candidateKind: "submitted" } })));
    expect(publishAgentWorkingCopy).not.toHaveBeenCalled();
  });

  it("names the failing check and offers the field when the draft can't be frozen", async () => {
    arrange();
    vi.mocked(publishAgentWorkingCopy).mockRejectedValue(new ApiError(409, "agent_release_blocked", {
      code: "agent_release_blocked",
      details: { reason_codes: ["credential_or_secret"] },
    }));
    const onOpenSection = vi.fn();
    const openChat = vi.fn();
    renderIntl(<Harness onOpenSection={onOpenSection} openChat={openChat} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("草稿还不能试用")).toBeTruthy();
    expect(dialog.textContent).toContain("没通过的检查：");
    fireEvent.click(within(dialog).getByRole("button", { name: "去修改工作说明" }));
    expect(onOpenSection).toHaveBeenCalledWith("does");
    expect(openChat).not.toHaveBeenCalled();
  });

  it("says why a failed save stopped the trial", async () => {
    arrange();
    renderIntl(<Harness dirtyFields={["instructions"]} save={vi.fn(async () => false)} openChat={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "试用草稿" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog.textContent).toContain("草稿没能保存，所以还没开始试用");
  });
});

describe("试用记录", () => {
  const trials: AgentRecentTrial[] = [
    {
      session_id: "s_1",
      first_question: "上周哪个品类销售额最高？",
      last_message_at: "2026-11-25T06:41:00Z",
      turn_count: 2,
      drafts_used: [
        { candidate_id: "arc_1", candidate_kind: "draft", frozen_at: "x", released_as: null },
        { candidate_id: "arc_2", candidate_kind: "draft", frozen_at: "y", released_as: null },
      ],
      released_as: null,
    },
    {
      session_id: "s_2",
      first_question: "华东区门店上周哪个品类卖得最好？",
      last_message_at: "2026-11-24T10:05:00Z",
      turn_count: 1,
      drafts_used: [{ candidate_id: "arc_0", candidate_kind: "draft", frozen_at: "x", released_as: { release_id: "arl_2", release_sequence: 2 } }],
      released_as: { release_id: "arl_2", release_sequence: 2 },
    },
  ];

  it("lists the Owner's trials with turns, drafts used and the release that ended one", () => {
    renderIntl(<AgentTrialList trials={trials} onNewTrial={vi.fn()} />);
    expect(screen.getByRole("heading", { name: /试用记录/ }).textContent).toContain("只有你能看到 · 显示最近 10 次");
    const first = screen.getByRole("link", { name: "打开试用：上周哪个品类销售额最高？" });
    expect(first.getAttribute("href")).toContain("/?session=s_1");
    const rows = screen.getAllByRole("listitem");
    expect(rows[0].textContent).toContain("2 轮 · 用过 2 份草稿");
    expect(rows[1].textContent).toContain("1 轮 · 那份草稿已发布为 v2");
  });

  it("starts a new trial with the same action", () => {
    const onNewTrial = vi.fn();
    renderIntl(<AgentTrialList trials={[]} onNewTrial={onNewTrial} />);
    expect(screen.getByText(/还没有试用过/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "新的试用" }));
    expect(onNewTrial).toHaveBeenCalledOnce();
  });
});
