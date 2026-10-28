import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import type { CanvasDraftSuggestion, ManualSubagentDraftDetail } from "@/lib/subagent-drafts-api";
import type { AgentDraftSuggestionsModel } from "./agent-draft-suggestions";

const mocks = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), apply: vi.fn(), dismiss: vi.fn(), flush: vi.fn(), generate: vi.fn() }));
vi.mock("@/lib/subagent-drafts-api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/subagent-drafts-api")>()),
  listCanvasDraftSuggestions: mocks.list,
  getManualSubagentDraft: mocks.get,
  applyCanvasDraftSuggestion: mocks.apply,
  dismissCanvasDraftSuggestion: mocks.dismiss,
  generateCanvasDraftSuggestion: mocks.generate,
}));
const LABELS: Record<string, string> = { name: "标识名", description: "用途说明", skill_grants: "技能", capability_ids: "插件" };
// The real form is replaced; the AI suggestions section (D8) renders inside it exactly as in Canvas.
vi.mock("./create-subagent-card", async () => {
  const { AgentDraftSuggestions } = await import("./agent-draft-suggestions");
  return {
    CreateSubagentCard: ({ onDraftChange, chatDraftEditEnabled, controllerRef, manualDraft, aiSuggestions, externalBusy }: {
      manualDraft: ManualSubagentDraftDetail;
      onDraftChange: (binding: { draftId: string; version: number; dirty: boolean }) => void;
      chatDraftEditEnabled: boolean;
      controllerRef: { current: unknown };
      aiSuggestions: AgentDraftSuggestionsModel | null;
      externalBusy: boolean;
    }) => {
      controllerRef.current = { read: () => ({ draft: manualDraft, dirty: false }), flush: mocks.flush,
        mutate: async (operation: (draft: ManualSubagentDraftDetail) => Promise<ManualSubagentDraftDetail>) => operation(await mocks.get(manualDraft.draft_id)), requestClose: vi.fn() };
      return <>
        {aiSuggestions ? <AgentDraftSuggestions model={aiSuggestions} idBase="test" label={(field) => LABELS[field] ?? field}
          current={() => ""} format={(_field, before, after) => ({ before: { kind: "text", text: String(before ?? "") }, after: { kind: "text", text: JSON.stringify(after) } })} /> : null}
        <button data-chat-connected={chatDraftEditEnabled} disabled={externalBusy}
          onClick={() => onDraftChange({ draftId: "sad_canvas", version: 1, dirty: true })}>Edit draft</button>
      </>;
    },
  };
});

import { SubagentCanvasDraftEditor } from "./subagent-canvas-draft-editor";
import { SubagentDraftApiError } from "@/lib/subagent-drafts-api";

const draft = {
  schema_version: 2, kind: "team_subagent_manual_draft_detail", draft_id: "sad_canvas",
  draft_status: "editing", source_mode: "intent", entry_mode: "canvas", workspace_id: "ws_a",
  purpose: "Analyze sales", definition: { name: "my-name", description: "My description", system_prompt: "Work" },
  validation: { status: "unknown", issues: [] }, warnings: [],
  mutation_id: "11111111-1111-4111-8111-111111111111", version: 1, etag: 'W/"sad_canvas:1"',
  expires_at: "2026-10-01T00:00:00Z", result: null,
  actions: { edit: true, validate: true, create: false, discard: true },
} satisfies ManualSubagentDraftDetail;
const suggestion = {
  schema_version: 2, kind: "team_subagent_canvas_suggestion", suggestion_id: "sas_1",
  draft_id: draft.draft_id, base_version: 1, suggestion_kind: "definition", status: "pending",
  payload: { definition: { name: "ai-name", description: "AI description" } },
  created_at: "2026-09-27T00:00:00Z", expires_at: draft.expires_at,
} satisfies CanvasDraftSuggestion;

afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

function row(label: string) {
  return screen.getByText(label, { selector: "span *, span" }).closest("[data-state]") as HTMLElement;
}
function accept(label: string) {
  fireEvent.click(within(row(label)).getByRole("button", { name: "采用" }));
}
function dismiss(label: string) {
  fireEvent.click(within(row(label)).getByRole("button", { name: "忽略" }));
}

it("shows suggestions arriving after a conversational Canvas draft opens", async () => {
  vi.useFakeTimers();
  mocks.list.mockResolvedValueOnce([]).mockResolvedValue([suggestion]);
  renderIntl(<SubagentCanvasDraftEditor manualDraft={{ ...draft, source_mode: "turn" }} onRequestAiDraft={vi.fn()}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  await act(async () => {});
  expect(screen.queryByText("AI 建议")).not.toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getByText("AI 建议")).toBeInTheDocument();
  expect(screen.getByText("2 项待处理")).toBeInTheDocument();
});

it("refreshes a connected manual draft without applying suggestions or clearing dirty input", async () => {
  vi.useFakeTimers();
  mocks.list.mockResolvedValueOnce([]).mockResolvedValue([suggestion, { ...suggestion, draft_id: "sad_other" }]);
  const onDraftChange = vi.fn();
  const view = renderIntl(<SubagentCanvasDraftEditor manualDraft={{ ...draft, source_mode: "manual" }}
    onDraftChange={onDraftChange} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.getAllByText("AI 建议")).toHaveLength(1);
  expect(onDraftChange).toHaveBeenLastCalledWith({ draftId: draft.draft_id, version: 1, dirty: true });
  expect(mocks.apply).not.toHaveBeenCalled();
  expect(mocks.get).not.toHaveBeenCalled();
  view.unmount();
  const reads = mocks.list.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(mocks.list).toHaveBeenCalledTimes(reads);
});

it("does not poll a manual draft without admitted Chat editing", async () => {
  vi.useFakeTimers();
  mocks.list.mockResolvedValue([]);
  renderIntl(<SubagentCanvasDraftEditor manualDraft={{ ...draft, source_mode: "manual" }}
    chatDraftEditEnabled={false} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(mocks.list).toHaveBeenCalledOnce();
});

it("does not restore an accepted suggestion from an older poll response", async () => {
  vi.useFakeTimers();
  let finishOldRead!: (items: CanvasDraftSuggestion[]) => void;
  mocks.list.mockResolvedValueOnce([suggestion])
    .mockImplementationOnce(() => new Promise((resolve) => { finishOldRead = resolve; }))
    .mockResolvedValue([]);
  mocks.get.mockResolvedValue(draft);
  mocks.apply.mockResolvedValue({ ...draft, version: 2 });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={{ ...draft, source_mode: "manual" }}
    chatDraftEditEnabled onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await act(async () => {});
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  accept("用途说明");
  await act(async () => { dismiss("标识名"); });
  await act(async () => {});
  expect(mocks.apply).toHaveBeenCalledOnce();
  expect(screen.queryByRole("button", { name: "采用" })).not.toBeInTheDocument();
  await act(async () => { finishOldRead([suggestion]); });
  expect(screen.queryByRole("button", { name: "采用" })).not.toBeInTheDocument();
  expect(screen.getByText("已采用 1 · 已忽略 1")).toBeInTheDocument();
});

it.each([false, true])("locks editing during suggestion acceptance and ignores a late response after closing (%s)", async (close) => {
  mocks.list.mockResolvedValue([suggestion]);
  mocks.get.mockResolvedValue(draft);
  let finishApply!: (result: ManualSubagentDraftDetail) => void;
  mocks.apply.mockReturnValueOnce(new Promise((resolve) => { finishApply = resolve; }));
  const onManualDraftSaved = vi.fn();
  const view = renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onManualDraftSaved={onManualDraftSaved} onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledOnce());
  expect(screen.getByRole("button", { name: "Edit draft" })).toBeDisabled();
  if (close) view.unmount();
  await act(async () => { finishApply({ ...draft, version: 2 }); });
  if (close) expect(onManualDraftSaved).not.toHaveBeenCalled();
  else {
    expect(onManualDraftSaved).toHaveBeenCalledWith(draft.draft_id);
    expect(screen.getByRole("button", { name: "Edit draft" })).toBeEnabled();
  }
});

it("saves unsaved edits first, then accepts, without asking the user to wait", async () => {
  mocks.list.mockResolvedValue([suggestion]);
  mocks.get.mockResolvedValue(draft);
  const order: string[] = [];
  mocks.flush.mockImplementation(async () => { order.push("flush"); });
  mocks.apply.mockImplementation(async () => { order.push("apply"); return { ...draft, version: 2 }; });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
  const acceptButtons = screen.getAllByRole("button", { name: "采用" });
  expect(acceptButtons.every((button) => !(button as HTMLButtonElement).disabled)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledOnce());
  expect(order).toEqual(["flush", "apply"]);
});

it("keeps its internal draft tracking when Chat editing is unavailable", () => {
  mocks.list.mockResolvedValue([]);
  const onDraftChange = vi.fn();
  const view = renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Edit draft" })).toHaveAttribute("data-chat-connected", "false");
  fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
  view.unmount();

  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onDraftChange={onDraftChange} onClose={vi.fn()} onCreated={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Edit draft" })).toHaveAttribute("data-chat-connected", "true");
  fireEvent.click(screen.getByRole("button", { name: "Edit draft" }));
  expect(onDraftChange).toHaveBeenCalledWith({ draftId: "sad_canvas", version: 1, dirty: true });
});

it("applies only the fields the user accepted once every field has a decision", async () => {
  mocks.list.mockResolvedValue([suggestion]);
  mocks.get.mockResolvedValue(draft);
  mocks.apply.mockResolvedValue({ ...draft, version: 2, definition: { name: "my-name", description: "AI description" } });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  accept("用途说明");
  expect(mocks.apply).not.toHaveBeenCalled();
  // An accepted row can still be undone until the suggestion is applied.
  fireEvent.click(within(row("用途说明")).getByRole("button", { name: "撤销" }));
  accept("用途说明");
  dismiss("标识名");
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith(draft, suggestion.suggestion_id, ["description"], expect.any(String)));
});

it("lets the user dismiss an unwanted suggestion without changing the draft", async () => {
  mocks.list.mockResolvedValueOnce([suggestion]).mockResolvedValueOnce([]);
  mocks.dismiss.mockResolvedValue(undefined);
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  dismiss("用途说明");
  dismiss("标识名");
  await waitFor(() => expect(mocks.dismiss).toHaveBeenCalledWith(draft.draft_id, suggestion.suggestion_id, { teamId: expect.any(String), workspaceId: draft.workspace_id }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "采用" })).not.toBeInTheDocument());
  expect(mocks.apply).not.toHaveBeenCalled();
});

it("reloads the saved draft before switching back to AI generation", async () => {
  mocks.list.mockResolvedValue([]);
  const latest = { ...draft, version: 2, etag: 'W/"sad_canvas:2"' };
  mocks.get.mockResolvedValue(latest);
  const onRequestAiDraft = vi.fn();
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={onRequestAiDraft}
    onClose={vi.fn()} onCreated={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "根据新的描述重新生成…" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
  expect(onRequestAiDraft).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "根据新的描述重新生成…" }));
  await waitFor(() => expect(onRequestAiDraft).toHaveBeenCalledWith(latest));
});

it("lets the Owner accept exact Skill and Plugin proposal fields", async () => {
  const grant = { skill_ref: { kind: "builtin" as const, id: "builtin:method", name: "method" }, revision: "sha256:original", permissions: ["read" as const], distribution: "reference" as const };
  mocks.list.mockResolvedValue([{ ...suggestion, payload: { definition: { skill_grants: [grant], capability_ids: ["tcap_exact"] }, changed_fields: ["skill_grants", "capability_ids"] } }]);
  mocks.get.mockResolvedValue(draft);
  mocks.apply.mockResolvedValue({ ...draft, version: 2 });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("技能");
  accept("技能");
  accept("插件");
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledWith(draft, suggestion.suggestion_id, ["skill_grants", "capability_ids"], expect.any(String)));
});

it("returns a failed commit to pending with a retry and counts only the applied suggestion", async () => {
  mocks.list.mockResolvedValue([suggestion]);
  mocks.get.mockResolvedValue(draft);
  mocks.flush.mockResolvedValue(undefined);
  mocks.apply.mockRejectedValueOnce(new Error("temporary failure"))
    .mockResolvedValueOnce({ ...draft, version: 2, definition: { name: "ai-name", description: "AI description" } });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  accept("用途说明");
  accept("标识名");
  const failure = await screen.findByRole("alert");
  expect(failure).toHaveTextContent("建议未能应用。请检查最新草稿并重试。");
  // The rows are pending again; nothing is counted as adopted.
  expect(within(row("用途说明")).getByRole("button", { name: "采用" })).toBeEnabled();
  expect(within(row("标识名")).getByRole("button", { name: "采用" })).toBeEnabled();
  expect(screen.queryByText(/已采用 \d/)).not.toBeInTheDocument();
  mocks.list.mockResolvedValue([]);
  fireEvent.click(within(failure).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(2));
  expect(mocks.apply).toHaveBeenLastCalledWith(draft, suggestion.suggestion_id, ["description", "name"], expect.any(String));
  expect(await screen.findByText("已采用 2 · 已忽略 0")).toBeInTheDocument();
});

it("applies suggestions one at a time when several are decided together", async () => {
  const second = { ...suggestion, suggestion_id: "sas_2", payload: { definition: { system_prompt: "AI work" } } } satisfies CanvasDraftSuggestion;
  mocks.list.mockResolvedValue([suggestion, second]);
  mocks.get.mockResolvedValue(draft);
  mocks.flush.mockResolvedValue(undefined);
  let finishFirst!: (value: ManualSubagentDraftDetail) => void;
  mocks.apply.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }))
    .mockResolvedValueOnce({ ...draft, version: 3 });
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(1));
  // The second commit waits for the first instead of being dropped as "busy".
  await act(async () => { await Promise.resolve(); });
  expect(mocks.apply).toHaveBeenCalledTimes(1);
  await act(async () => { finishFirst({ ...draft, version: 2 }); });
  await waitFor(() => expect(mocks.apply).toHaveBeenCalledTimes(2));
  expect(mocks.apply.mock.calls[1][1]).toBe("sas_2");
});

it("asks before closing while suggestion decisions are not yet applied", async () => {
  mocks.list.mockResolvedValue([suggestion]);
  const viewRef = { current: null as { requestClose: () => void } | null };
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} viewRef={viewRef} onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  await screen.findByText("AI 建议");
  accept("用途说明");
  act(() => viewRef.current!.requestClose());
  const guard = await screen.findByRole("dialog");
  expect(guard).toHaveTextContent("放弃未保存的修改？");
  expect(mocks.apply).not.toHaveBeenCalled();
});

it("explains an AI drafting timeout instead of asking for a refresh", async () => {
  mocks.list.mockResolvedValue([]);
  mocks.get.mockResolvedValue(draft);
  mocks.flush.mockResolvedValue(undefined);
  mocks.generate.mockRejectedValue(new SubagentDraftApiError(503, "subagent_draft_model_timeout", "Timed out"));
  renderIntl(<SubagentCanvasDraftEditor manualDraft={draft} inlineGeneration onRequestAiDraft={vi.fn()} onClose={vi.fn()} onCreated={vi.fn()} />);
  const reopen = screen.queryByRole("button", { name: "根据新的描述重新生成…" });
  if (reopen) fireEvent.click(reopen);
  fireEvent.click(await screen.findByRole("button", { name: "生成建议" }));
  expect(await screen.findByText(/AI 起草超过 2 分半还没完成/)).toBeTruthy();
  expect(screen.queryByText("建议暂时无法读取，请刷新后重试。")).toBeNull();
});
