import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ApiError } from "@/lib/api-client";
import { changeAgentCoachFields, type AgentDraftChange, type AgentWorkingCopyValue } from "@/lib/agents-api";
import { CoachChangesSection } from "./coach-changes-section";

vi.mock("@/lib/agents-api", () => ({
  changeAgentCoachFields: vi.fn(),
}));

const PROMPT = "/definition/system_prompt";
const TOOLS = "/definition/tool_refs";

const WORKING: AgentWorkingCopyValue = {
  id: "awc_1",
  base_release_id: "arl_1",
  definition: { system_prompt: "Mine.", tool_refs: ["bi_query", "powerbi"] },
  listing: { display_name: "Finance Helper" },
  audience: { schema_version: 1, effective_mode: "private", user_ids: [], group_ids: [] },
  validation: {},
  change_kind: "edit",
  rollback_source_release_id: null,
  row_version: 9,
  updated_at: "2026-10-17T10:00:00Z",
};

function coachRow(path: string, overrides: Partial<NonNullable<AgentDraftChange["coach"]>> & { draft?: unknown } = {}): AgentDraftChange {
  const { draft, ...coach } = overrides;
  return {
    path,
    live: "Base.",
    draft: draft ?? "Coach.",
    source: "coach",
    coach: { state: "applied", proposal_id: "acp_1", group_ids: ["g0"], before: "Base.", value: "Coach.", conflict: false, ...coach },
  };
}

function renderSection(changes: AgentDraftChange[], props: Partial<Parameters<typeof CoachChangesSection>[0]> = {}) {
  const onChanged = vi.fn().mockResolvedValue(undefined);
  const onOpenSection = vi.fn();
  renderIntl(<CoachChangesSection agentId="osa_1" changes={changes} workingCopy={WORKING} editorDirty={false} onChanged={onChanged} onOpenSection={onOpenSection} {...props} />);
  return { onChanged, onOpenSection };
}

describe("CoachChangesSection", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("shows nothing when Coach changed nothing in this draft", () => {
    renderSection([{ path: PROMPT, live: "a", draft: "b", source: "manual" }]);
    expect(screen.queryByRole("heading", { name: "Coach 的修改" })).toBeNull();
  });

  it("undoes one field with the draft's row version and refreshes", async () => {
    vi.mocked(changeAgentCoachFields).mockResolvedValue({} as never);
    const tools = { ...coachRow(TOOLS, { before: ["bi_query"], value: ["bi_query", "powerbi"] }), draft: ["bi_query", "powerbi"] };
    const { onChanged } = renderSection([coachRow(PROMPT), tools]);
    expect(screen.getByText("2 处，已写入草稿")).toBeTruthy();
    expect(screen.getByText("+ powerbi")).toBeTruthy();
    const prompt = screen.getByText("智能体指令").closest("li")!;
    fireEvent.click(within(prompt).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(changeAgentCoachFields).toHaveBeenCalledWith("osa_1", "undo", [PROMPT], 9));
    // The status line is written to a live region and repeated visibly.
    expect((await screen.findAllByText("已撤销「智能体指令」")).length).toBeGreaterThan(0);
    expect(onChanged).toHaveBeenCalled();
  });

  it("offers Reapply on an undone row and says why a row changed after undo can't be reapplied", async () => {
    vi.mocked(changeAgentCoachFields).mockResolvedValue({} as never);
    renderSection([
      coachRow(PROMPT, { state: "undone", draft: "Base." }),
      coachRow(TOOLS, { state: "undone", conflict: true, before: [], value: ["powerbi"], draft: ["other"] }),
    ]);
    expect(screen.getByText("撤销了 2 处")).toBeTruthy();
    expect(screen.getByText("撤销后你又改过，不能重新应用")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "重新应用" }));
    await waitFor(() => expect(changeAgentCoachFields).toHaveBeenCalledWith("osa_1", "reapply", [PROMPT], 9));
  });

  it("blocks Undo all while a field is in conflict and offers to undo only the others", async () => {
    vi.mocked(changeAgentCoachFields).mockResolvedValue({} as never);
    renderSection([
      coachRow(PROMPT, { conflict: true, draft: "Mine." }),
      coachRow(TOOLS, { before: ["bi_query"], value: ["bi_query", "powerbi"], draft: ["bi_query", "powerbi"] }),
    ]);
    expect(screen.getByRole("button", { name: "全部撤销" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("没有撤销：「智能体指令」你后来也改过。先处理这一处，或只撤销其他的。")).toBeTruthy();
    expect(within(screen.getByText("智能体指令").closest("li")!).getByText("你也改过")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "只撤销其他 1 处" }));
    await waitFor(() => expect(changeAgentCoachFields).toHaveBeenCalledWith("osa_1", "undo", [TOOLS], 9));
  });

  it("shows the three values of a conflict and keeps the Owner's value without saving the draft", async () => {
    vi.mocked(changeAgentCoachFields).mockResolvedValue({} as never);
    renderSection([coachRow(PROMPT, { conflict: true, draft: "Mine." })]);
    fireEvent.click(screen.getByRole("button", { name: "查看冲突" }));
    const dialog = await screen.findByRole("dialog", { name: "「智能体指令」被改过两次" });
    expect(within(dialog).getByText("Coach 改之前").nextElementSibling!.textContent).toBe("Base.");
    expect(within(dialog).getByText("Coach 写的").nextElementSibling!.textContent).toBe("Coach.");
    expect(within(dialog).getByText("现在（你改的）").nextElementSibling!.textContent).toBe("Mine.");
    fireEvent.click(within(dialog).getByRole("button", { name: "保留现在的" }));
    await waitFor(() => expect(changeAgentCoachFields).toHaveBeenCalledWith("osa_1", "keep", [PROMPT], 9));
  });

  it("restores the value before Coach in one confirmed write", async () => {
    vi.mocked(changeAgentCoachFields).mockResolvedValue({} as never);
    renderSection([coachRow(PROMPT, { conflict: true, draft: "Mine." })]);
    fireEvent.click(screen.getByRole("button", { name: "查看冲突" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "还原到 Coach 改之前" }));
    expect(changeAgentCoachFields).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "覆盖并还原" }));
    await waitFor(() => expect(changeAgentCoachFields).toHaveBeenCalledWith("osa_1", "restore", [PROMPT], 9));
    expect(changeAgentCoachFields).toHaveBeenCalledTimes(1);
    // The status line is written to a live region and repeated visibly.
    expect((await screen.findAllByText("已把「智能体指令」还原到 Coach 改之前")).length).toBeGreaterThan(0);
  });

  it("opens the Configure section that holds a conflicted field", async () => {
    const { onOpenSection } = renderSection([coachRow(TOOLS, { conflict: true, before: [], value: ["powerbi"], draft: ["other"] })]);
    fireEvent.click(screen.getByRole("button", { name: "查看冲突" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "去配置里手动改" }));
    expect(onOpenSection).toHaveBeenCalledWith("uses");
  });

  it("waits for unsaved canvas edits before undoing", () => {
    renderSection([coachRow(PROMPT)], { editorDirty: true });
    expect(screen.getByRole("button", { name: "撤销" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("画布里有没保存的修改。先保存或放弃，再撤销 Coach 的修改。")).toBeTruthy();
  });

  it("refreshes and explains when the draft changed underneath the list", async () => {
    vi.mocked(changeAgentCoachFields).mockRejectedValue(new ApiError(409, "Conflict", { code: "agent_change_undo_conflict" }));
    const { onChanged } = renderSection([coachRow(PROMPT)]);
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    // The status line is written to a live region and repeated visibly.
    expect((await screen.findAllByText("这处在你打开列表后又被改过，没有撤销。列表已刷新，请点「查看冲突」。")).length).toBeGreaterThan(0);
    expect(onChanged).toHaveBeenCalled();
  });
});
