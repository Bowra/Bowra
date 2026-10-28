import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiFetchMock = vi.fn();
vi.mock("./api-client", () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  readApiError: vi.fn(),
}));

import {
  createSubagentFromDraft,
  generateSubagentDraft,
  getSubagentDraft,
  updateSubagentDraft,
  type SubagentDraftDetail,
} from "./subagent-drafts-api";

const DRAFT = {
  schema_version: 1,
  kind: "team_subagent_draft_detail",
  draft_id: "sad_api",
  draft_status: "ready",
  authoring_state: "reviewing",
  source_mode: "intent",
  entry_mode: "conversational",
  purpose: "Analyze sales",
  goal_summary: "Analyze sales",
  definition: {
    name: "sales",
    display_name: "Sales",
    description: "Analyze sales",
    system_prompt: "Analyze sales.",
    model: "analyst",
    max_iterations: 10,
    output_schema: null,
    visibility: "creator_only",
  },
  tools: [],
  skills: [],
  assumptions: [],
  warnings: [],
  clarification: null,
  validation: {
    status: "valid",
    issues: [],
    catalog_fingerprint: "sha256:test",
  },
  mutation_id: "11111111-1111-4111-8111-111111111111",
  version: 1,
  etag: 'W/"sad_api:1"',
  expires_at: "2026-08-01T00:00:00Z",
  result: null,
  actions: {
    clarify: false,
    edit: true,
    validate: true,
    create: true,
    discard: true,
    regenerate: false,
  },
} satisfies SubagentDraftDetail;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const storageDescriptor = Object.getOwnPropertyDescriptor(window, "localStorage");
beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", { configurable: true, value: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  } });
});
afterEach(() => {
  vi.clearAllMocks();
  if (storageDescriptor) Object.defineProperty(window, "localStorage", storageDescriptor);
});

describe("subagent drafts API", () => {
  it("uses an idempotent intent POST and returns a typed draft", async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(DRAFT, 201));

    await expect(generateSubagentDraft("Analyze sales", {
      idempotencyKey: "idem-1",
      locale: "en-US",
    })).resolves.toEqual(DRAFT);

    const [url, init] = apiFetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/team-subagents/drafts");
    expect(init.headers).toMatchObject({ "Idempotency-Key": "idem-1" });
    expect(JSON.parse(String(init.body))).toMatchObject({
      schema_version: 1,
      source_mode: "intent",
      purpose: "Analyze sales",
      locale: "en-US",
    });
  });

  it("loads owner-scoped detail without accepting config from an event", async () => {
    apiFetchMock.mockResolvedValue(jsonResponse(DRAFT));
    await expect(getSubagentDraft("sad/api")).resolves.toEqual(DRAFT);
    expect(apiFetchMock).toHaveBeenCalledWith(
      "/api/v1/team-subagents/drafts/sad%2Fapi",
      { signal: undefined },
    );
  });

  it("sends If-Match on update and final create", async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse(DRAFT))
      .mockResolvedValueOnce(jsonResponse({
        schema_version: 1,
        kind: "team_subagent_created_from_draft",
        replayed: false,
        draft_id: "sad_api",
        definition_id: "osa_1",
      }, 201));
    if (!DRAFT.definition) throw new Error("fixture definition missing");
    await updateSubagentDraft(
      DRAFT.draft_id,
      {
        schema_version: 1,
        definition: DRAFT.definition,
        selected_tool_refs: [],
        selected_skills: [],
      },
      DRAFT.etag,
    );
    await createSubagentFromDraft(DRAFT, []);

    expect((apiFetchMock.mock.calls[0][1] as RequestInit).headers)
      .toMatchObject({ "If-Match": DRAFT.etag });
    expect((apiFetchMock.mock.calls[1][1] as RequestInit).headers)
      .toMatchObject({ "If-Match": DRAFT.etag });
  });
});

const scope = { teamId: "team_original", workspaceId: "ws_original" };
const MANUAL = {
  schema_version: 2, kind: "team_subagent_manual_draft_detail", draft_id: "sad_manual",
  workspace_id: scope.workspaceId, etag: 'W/"sad_manual:1"', mutation_id: DRAFT.mutation_id,
  definition: DRAFT.definition,
};
const SUGGESTION = { schema_version: 2, kind: "team_subagent_canvas_suggestion", suggestion_id: "sas_one", payload: {} };

it("keeps all draft effects and recovery reads in the original scope after ambient selection changes", async () => {
  const api = await import("./subagent-drafts-api");
  apiFetchMock.mockResolvedValueOnce(jsonResponse(MANUAL));
  const draft = await api.getManualSubagentDraft("sad_manual", scope);
  window.localStorage.setItem("bowhead.team_id", "team_other");
  window.localStorage.setItem("bowhead.space_id", "ws_other");
  apiFetchMock.mockImplementation(async () => jsonResponse(MANUAL));
  await api.saveManualSubagentDraft(draft, "Original task", DRAFT.definition!, false);
  await api.validateManualSubagentDraft(draft);
  await api.applyCanvasDraftSuggestion(draft, "sas_one", ["display_name"], "mutation-one");
  await api.getManualSubagentDraft(draft.draft_id, draft.requestContext);
  apiFetchMock.mockResolvedValueOnce(jsonResponse(SUGGESTION));
  await api.generateCanvasDraftSuggestion(draft, "Original task", { idempotencyKey: "generation-one", locale: "en-US" });
  apiFetchMock.mockResolvedValueOnce(jsonResponse({ schema_version: 2, kind: "team_subagent_canvas_suggestions", items: [SUGGESTION] }));
  await api.listCanvasDraftSuggestions(draft.draft_id, draft.requestContext!);
  apiFetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await api.dismissCanvasDraftSuggestion(draft.draft_id, "sas_one", draft.requestContext!);
  apiFetchMock.mockResolvedValueOnce(jsonResponse({ schema_version: 2, kind: "team_subagent_created_from_draft", definition_id: "osa_one" }));
  await api.createSubagentFromManualDraft(draft, []);
  for (const [, init] of apiFetchMock.mock.calls as [string, RequestInit][]) {
    expect(init.headers).toMatchObject({ "X-Team-Id": scope.teamId, "X-Space-Id": scope.workspaceId, "X-Workspace-Id": scope.workspaceId });
    if (init.body) expect(JSON.parse(String(init.body))).not.toHaveProperty("requestContext");
  }
});

it("rejects a response from another Space before installing a draft", async () => {
  const api = await import("./subagent-drafts-api");
  apiFetchMock.mockResolvedValueOnce(jsonResponse({ ...MANUAL, workspace_id: "ws_other" }));
  await expect(api.getManualSubagentDraft("sad_manual", scope)).rejects.toMatchObject({ code: "subagent_draft_response_invalid" });
});

it("never substitutes ambient scope for a retained draft without its original context", async () => {
  const api = await import("./subagent-drafts-api");
  await expect(api.saveManualSubagentDraft(MANUAL as unknown as import("./subagent-drafts-api").ManualSubagentDraftDetail, "Task", DRAFT.definition!, false))
    .rejects.toMatchObject({ code: "subagent_draft_context_required" });
  expect(apiFetchMock).not.toHaveBeenCalled();
});
