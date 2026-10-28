/** Create Agent card: validation and save failures show no error/status codes and explain how to fix them; Copy Diagnostics carries the status codes. */
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import de from "@/messages/de-DE.json";
import en from "@/messages/en-US.json";
import zh from "@/messages/zh-CN.json";

const mocks = vi.hoisted(() => ({
  getToolsCatalog: vi.fn(),
  getSkillsCatalog: vi.fn(),
  createSubagent: vi.fn(),
  saveManualDraft: vi.fn(),
  validateManualDraft: vi.fn(),
}));
vi.mock("@/lib/agents-api", () => ({ saveAgentWorkingCopy: vi.fn(), getAgentAuthoringCatalog: () => Promise.resolve({ plugins: undefined }) }));
vi.mock("@/lib/use-app-router", () => ({
  BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation",
  useAppRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/lib/team-subagents-api", () => ({
  getToolsCatalog: (...a: unknown[]) => mocks.getToolsCatalog(...a),
  getSkillsCatalog: (...a: unknown[]) => mocks.getSkillsCatalog(...a),
  createSubagent: (...a: unknown[]) => mocks.createSubagent(...a),
  patchSubagent: vi.fn(),
}));
vi.mock("@/lib/subagent-drafts-api", () => ({
  SubagentDraftApiError: class extends Error {
    constructor(public status: number, public code: string) { super(code); }
  },
  createManualSubagentDraft: vi.fn(),
  saveManualSubagentDraft: (...a: unknown[]) => mocks.saveManualDraft(...a),
  getManualSubagentDraft: vi.fn(),
  validateManualSubagentDraft: (...a: unknown[]) => mocks.validateManualDraft(...a),
  createSubagentFromManualDraft: vi.fn(),
}));

import { CreateSubagentCard } from "./create-subagent-card";
import { createAgentErrorKey } from "./create-subagent-errors";

const REQUEST_ID = "0123456789abcdef0123456789abcdef";
const LEAK = /subagent_[a-z_]+|\b[45]\d\d\b|HTTP/;

beforeEach(() => {
  mocks.getSkillsCatalog.mockResolvedValue({ builtin: [], team_skills: [] });
  mocks.getToolsCatalog.mockResolvedValue({ team_tools: [], native_tools: ["bi_query"], text_only_allowed: false });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function goToConfig() {
  fireEvent.change(screen.getByPlaceholderText("sales-analysis"), { target: { value: "sales-bot" } });
  fireEvent.change(screen.getByPlaceholderText(/分析销售数据并生成图表/), { target: { value: "分析销售" } });
  fireEvent.change(screen.getByPlaceholderText(/你是一名严谨的销售数据分析师/), { target: { value: "分析销售数据并说明结论" } });
  fireEvent.click(screen.getByText(/下一步 · 选择工具/));
}

describe("create agent card errors", () => {
  it("a rejected create shows how to fix the name, not the code or status, with Copy diagnostics", async () => {
    mocks.createSubagent.mockRejectedValue(
      Object.assign(new Error("subagent_name_invalid"), { status: 422, code: "subagent_name_invalid", requestId: REQUEST_ID }),
    );
    renderIntl(<CreateSubagentCard onClose={() => {}} onCreated={() => {}} />);
    goToConfig();
    fireEvent.click(await screen.findByRole("checkbox"));
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("创建智能体"));

    const reason = await screen.findByText(zh.create_subagent.slug_invalid);
    expect(reason.textContent).not.toMatch(LEAK);
    const copy = screen.getByRole("button", { name: "复制诊断" });
    expect(reason.contains(copy)).toBe(false);
  });

  it("validation issues are explained per field, never as a list of codes", async () => {
    const draft = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_1", draft_status: "editing" as const, source_mode: "manual" as const, entry_mode: "canvas" as const,
      workspace_id: "ws_a", etag: 'W/"sad_1:1"', version: 1, purpose: "分析销售",
      definition: { name: "sales-bot", display_name: "销售助手", description: "分析销售", system_prompt: "分析证据", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] },
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    mocks.saveManualDraft.mockImplementation(async (_d, purpose, definition) => ({ ...draft, version: 2, purpose, definition }));
    mocks.validateManualDraft.mockResolvedValue({
      ...draft,
      validation: {
        status: "invalid",
        issues: [
          { code: "subagent_name_reserved", field: "name", message: null },
          { code: "subagent_output_schema_too_deep", field: "output_schema", message: null },
        ],
      },
      actions: { ...draft.actions, create: false },
    });
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={draft} onClose={vi.fn()} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findAllByRole("checkbox");
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("创建智能体"));

    const reason = await screen.findByText((text) => text.includes(zh.create_subagent.issue_name_reserved));
    expect(reason.textContent).toContain(zh.create_subagent.issue_schema_too_complex);
    expect(reason.textContent).not.toMatch(LEAK);
  });

  it("a dependency issue names the Tool and what to do, not the generic changed-resources sentence", async () => {
    const draft = {
      schema_version: 2 as const, kind: "team_subagent_manual_draft_detail" as const,
      draft_id: "sad_1", draft_status: "editing" as const, source_mode: "manual" as const, entry_mode: "canvas" as const,
      workspace_id: "ws_a", etag: 'W/"sad_1:1"', version: 1, purpose: "分析销售",
      definition: { name: "sales-bot", display_name: "销售助手", description: "分析销售", system_prompt: "分析证据", tool_refs: ["bi_query"], max_iterations: 12, model: "analyst", output_schema: null, output_schema_text: "" },
      warnings: [], validation: { status: "unknown", issues: [] },
      mutation_id: "11111111-1111-4111-8111-111111111111", expires_at: "2026-10-01T00:00:00Z", result: null,
      actions: { edit: true, validate: true, create: false, discard: true },
    };
    mocks.saveManualDraft.mockImplementation(async (_d, purpose, definition) => ({ ...draft, version: 2, purpose, definition }));
    mocks.validateManualDraft.mockResolvedValue({
      ...draft,
      validation: {
        status: "invalid",
        issues: [{
          code: "subagent_dependencies_unavailable_or_changed", field: "skill_grants", message: null,
          details: { reason: "plugin_dependencies_unavailable", rule: "tool_unavailable", tool_refs: ["bi_query"] },
        }],
      },
      actions: { ...draft.actions, create: false },
    });
    renderIntl(<CreateSubagentCard unifiedDraftEnabled manualDraft={draft} onClose={vi.fn()} />);
    fireEvent.click(screen.getByText(/下一步 · 选择工具/));
    await screen.findAllByRole("checkbox");
    fireEvent.click(screen.getByText(/下一步 · 配置/));
    fireEvent.click(screen.getByText("创建智能体"));

    const reason = await screen.findByText((text) => text.includes(zh.agents_marketplace.blocked_reason_tool_unavailable));
    expect(reason.textContent).toMatch(/工具「[^」]+」/);
    expect(reason.textContent).not.toContain(zh.create_subagent.issue_dependencies_changed);
    expect(reason.textContent).not.toMatch(LEAK);
  });

  it.each([
    [{ code: "subagent_name_invalid" }, "slug_invalid"],
    [{ code: "subagent_tool_required", field: "tool_refs" }, "issue_tool_required"],
    [{ code: "something_new", field: "description" }, "issue_description_too_long"],
    [{ status: 403 }, "issue_forbidden"],
    [{ status: 500 }, "draft_save_failed"],
    [new SyntaxError("Unexpected token } in JSON"), "schema_invalid_hint"],
    [new TypeError("Failed to fetch"), "issue_network"],
  ] as const)("%o → %s", (error, key) => {
    expect(createAgentErrorKey(error)).toBe(key);
  });

  it.each([
    ["zh-CN", zh],
    ["en-US", en],
    ["de-DE", de],
  ] as const)("%s: every sentence the card can pick exists and carries no code or status", (_locale, catalog) => {
    const keys = [
      "slug_invalid", "issue_name_reserved", "issue_name_taken", "issue_display_name_too_long", "issue_description_too_long",
      "issue_persona_required", "issue_persona_too_long", "issue_prompt_security", "issue_tool_required", "issue_too_many_tools",
      "issue_tool_unavailable", "issue_skills_invalid", "issue_model_not_allowed", "issue_max_iterations", "schema_invalid_hint",
      "issue_schema_too_complex", "issue_forbidden", "issue_network", "draft_chat_conflict", "draft_review_failed", "draft_save_failed",
      "catalog_load_failed_plain", "skills_load_failed_plain",
    ];
    for (const key of keys) {
      const text = (catalog.create_subagent as Record<string, string>)[key];
      expect(text, key).toBeTruthy();
      expect(text).not.toMatch(/\{|subagent_|\b[45]\d\d\b/);
    }
  });

  it("tool catalog load failure: localized reason, Copy diagnostics outside the callout", async () => {
    mocks.getToolsCatalog.mockRejectedValue(Object.assign(new Error("tools catalog: 503"), { status: 503, requestId: REQUEST_ID }));
    renderIntl(<CreateSubagentCard onClose={() => {}} />);
    goToConfig();

    const reason = await screen.findByText(zh.create_subagent.catalog_load_failed_plain);
    expect(reason.textContent).not.toMatch(LEAK);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "复制诊断" }).length).toBeGreaterThan(0));
  });
});
