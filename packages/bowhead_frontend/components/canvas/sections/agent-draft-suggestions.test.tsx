import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { CanvasDraftSuggestion } from "@/lib/subagent-drafts-api";
import { AgentDraftSuggestions, type AgentDraftSuggestionsModel } from "./agent-draft-suggestions";

afterEach(cleanup);

const clarification: CanvasDraftSuggestion = {
  schema_version: 2,
  kind: "team_subagent_canvas_suggestion",
  suggestion_id: "sas_000000000001",
  draft_id: "sad_a",
  base_version: 1,
  suggestion_kind: "clarification",
  status: "pending",
  payload: { question: "它主要分析哪些业务线？" },
  created_at: "2026-10-05T06:00:00Z",
  expires_at: "2026-10-06T06:00:00Z",
};

function model(overrides: Partial<AgentDraftSuggestionsModel> = {}): AgentDraftSuggestionsModel {
  return {
    items: [clarification],
    fieldsOf: () => [],
    busy: false,
    error: null,
    generation: null,
    onCommit: vi.fn(async () => true),
    onDismiss: vi.fn(async () => true),
    ...overrides,
  };
}

function view(value: AgentDraftSuggestionsModel) {
  return renderIntl(
    <AgentDraftSuggestions
      model={value}
      idBase="draft"
      label={(field) => field}
      current={() => null}
      format={() => ({ before: { kind: "empty" }, after: { kind: "empty" } })}
    />,
  );
}

it("asks the AI clarification as an input card that can be dismissed or answered in chat", () => {
  const value = model({ onAnswerInChat: vi.fn() });
  view(value);
  const card = screen.getByTestId("agent-draft-clarification");
  expect(card.getAttribute("data-intent")).toBe("input");
  expect(card.textContent).toContain("需要你补充");
  expect(card.textContent).toContain("它主要分析哪些业务线？");
  fireEvent.click(screen.getByRole("button", { name: "去对话回答" }));
  expect(value.onAnswerInChat).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole("button", { name: "忽略" }));
  expect(value.onDismiss).toHaveBeenCalledWith(clarification);
});

it("offers only Dismiss when the chat is not connected to the draft", () => {
  view(model());
  expect(screen.getByRole("button", { name: "忽略" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "去对话回答" })).toBeNull();
});

it("offers a first AI draft on a blank draft, and regeneration only once there is a description", () => {
  const generation = {
    open: false, purpose: "", generating: false, initial: false,
    onPurpose: vi.fn(), onOpen: vi.fn(), onGenerate: vi.fn(), onStop: vi.fn(), onCancel: vi.fn(),
  };
  view(model({ items: [], generation }));
  expect(screen.getByRole("button", { name: "描述需求，让 AI 起草…" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "根据新的描述重新生成…" })).toBeNull();
  cleanup();
  view(model({ items: [], generation: { ...generation, purpose: "每周汇总销售" } }));
  expect(screen.getByRole("button", { name: "根据新的描述重新生成…" })).toBeTruthy();
});
