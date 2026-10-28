import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import {
  act,
  cleanup,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { useAgentCanvasObjects } from "@/lib/use-agent-canvas-objects";
import { confirmCanvasNavigation } from "@/lib/canvas-navigation-guard";
import type {
  CanvasDraftSuggestion,
  ManualSubagentDraftDetail,
} from "@/lib/subagent-drafts-api";
import { SubagentDraftApiError } from "@/lib/subagent-drafts-api";
import { AgentDraftCanvasObject } from "./sections/agent-draft-canvas-object";
import { ConfigurationCanvasHost } from "./configuration-canvas-host";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  save: vi.fn(),
  list: vi.fn(),
  apply: vi.fn(),
  generate: vi.fn(),
}));
vi.mock("@/lib/use-app-router", () => ({
  BEFORE_APP_NAVIGATION_EVENT: "bowhead:before-app-navigation",
  useAppRouter: () => ({ push: vi.fn() }),
}));
vi.mock("@/lib/team-subagents-api", () => ({
  getToolsCatalog: async () => ({
    team_tools: [],
    native_tools: ["bi_query"],
    text_only_allowed: false,
  }),
  getSkillsCatalog: async () => ({ builtin: [], team_skills: [] }),
}));
vi.mock("@/lib/subagent-drafts-api", async (original) => ({
  ...(await original<typeof import("@/lib/subagent-drafts-api")>()),
  getManualSubagentDraft: api.get,
  saveManualSubagentDraft: api.save,
  listCanvasDraftSuggestions: api.list,
  applyCanvasDraftSuggestion: api.apply,
  generateCanvasDraftSuggestion: api.generate,
}));

const draft = (id: string): ManualSubagentDraftDetail => ({
  schema_version: 2,
  kind: "team_subagent_manual_draft_detail",
  draft_id: id,
  workspace_id: "ws_a",
  draft_status: "editing",
  source_mode: "turn",
  entry_mode: "canvas",
  purpose: "Analyze evidence",
  definition: {
    name: id === "sad_a" ? "agent-a" : "agent-b",
    display_name: id,
    description: "Analyze evidence",
    system_prompt: "Use evidence",
    tool_refs: ["bi_query"],
    model: "analyst",
    max_iterations: 12,
    output_schema: null,
    output_schema_text: "",
  },
  version: 1,
  etag: `W/"${id}:1"`,
  mutation_id: "11111111-1111-4111-8111-111111111111",
  expires_at: "2099-10-01T00:00:00Z",
  result: null,
  warnings: [],
  validation: { status: "unknown", issues: [] },
  actions: { edit: true, validate: true, create: false, discard: true },
});
const suggestion: CanvasDraftSuggestion = {
  schema_version: 2,
  kind: "team_subagent_canvas_suggestion",
  suggestion_id: "sas_a",
  draft_id: "sad_a",
  base_version: 1,
  suggestion_kind: "definition",
  status: "pending",
  created_at: "2026-10-03T00:00:00Z",
  expires_at: "2099-10-01T00:00:00Z",
  payload: {
    changed_fields: ["description"],
    definition: {
      name: "unrelated-merged-name",
      description: "Proposed description",
    },
  },
};

function Workbench() {
  const canvas = useAgentCanvasObjects("user/team/ws_a", true);
  const [results, setResults] = useState(false);
  const open = (id: string) => {
    canvas.open({
      draft_id: id,
      draft_title: id,
      draft_status: "editing",
      purpose_hint: null,
    });
    setResults(false);
  };
  return (
    <>
      <button onClick={() => open("sad_a")}>Open A</button>
      <button onClick={() => open("sad_b")}>Open B</button>
      <button onClick={() => canvas.open({ draft_id: "sad_a", draft_title: "sad_a", draft_status: "editing", draft_version: 3, purpose_hint: null }, false)}>Refresh A</button>
      <output data-testid="selected-binding">{canvas.objects.find((object) => object.key === canvas.selectedKey)?.binding?.draftId ?? "none"}</output>
      <button
        onClick={() => {
          canvas.objects
            .find((object) => object.payload.draft_id === "sad_a")
            ?.editorRef.current?.requestClose();
          open("sad_a");
        }}
      >
        Reopen A
      </button>
      <button onClick={() => setResults(true)}>Show results</button>
      <button onClick={() => setResults(false)}>Show editors</button>
      <div hidden={results}>
        {canvas.selectedKey ? (
          <ConfigurationCanvasHost
            selectedKey={canvas.selectedKey}
            onSelect={canvas.select}
            objects={canvas.objects.map((object) => ({
              key: object.key,
              kind: "agent" as const,
              title: object.payload.draft_title!,
              onClose: () => object.editorRef.current?.requestClose(),
              content: (
                <AgentDraftCanvasObject
                  key={object.incarnation}
                  object={object}
                  workspaceId="ws_a"
                  unified
                  continuous
                  connected={canvas.selectedKey === object.key}
                  onBinding={canvas.bind}
                  onTitle={canvas.title}
                  onClose={canvas.close}
                  onActivate={canvas.select}
                />
              ),
            }))}
          />
        ) : null}
      </div>
    </>
  );
}
/** The Agent name field (the identifier now lives under Advanced, decision D9). */
const field = () => screen.getByRole("textbox", { name: "名称" });

beforeEach(() => {
  api.get.mockImplementation(async (id: string) => draft(id));
  api.list.mockResolvedValue([]);
  api.save.mockImplementation(
    async (
      current: ManualSubagentDraftDetail,
      purpose: string,
      definition,
    ) => ({
      ...current,
      purpose,
      definition,
      version: current.version + 1,
      etag: `W/"${current.draft_id}:${current.version + 1}"`,
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

/** D4: drafts save automatically about a second after the last edit. */
const AUTOSAVE = { timeout: 4000 };
const UNKNOWN_SAVE = "还没确认是否保存成功";

it("retains two real editable buffers across selection, Results, and duplicate opens", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  const originalA = field();
  fireEvent.change(originalA, { target: { value: "local-a" } });
  fireEvent.click(screen.getByText("Open B"));
  await waitFor(() => expect(field()).toHaveValue("sad_b"));
  const originalB = field();
  fireEvent.change(originalB, { target: { value: "local-b" } });
  fireEvent.click(screen.getByText("Show results"));
  fireEvent.click(screen.getByText("Show editors"));
  fireEvent.click(screen.getByRole("tab", { name: /^(sad_a|local-a)/ }));
  expect(field()).toBe(originalA);
  expect(field()).toHaveValue("local-a");
  fireEvent.click(screen.getByText("Open B"));
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  expect(field()).toBe(originalB);
  expect(field()).toHaveValue("local-b");
  await waitFor(() =>
    expect(api.save).toHaveBeenCalledWith(
      expect.objectContaining({ draft_id: "sad_b" }),
      "Analyze evidence",
      expect.objectContaining({ display_name: "local-b" }),
      false,
    ), AUTOSAVE);
});

it("reads a fresh original controller when clean close and reopen share one render", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  const original = field();
  api.get.mockImplementation(async (id: string) => ({
    ...draft(id),
    definition: { ...draft(id).definition, display_name: "updated-on-server" },
    version: 2,
    etag: `W/"${id}:2"`,
  }));
  fireEvent.click(screen.getByText("Reopen A"));
  await waitFor(() => expect(field()).toHaveValue("updated-on-server"));
  expect(field()).not.toBe(original);
  expect(api.get).toHaveBeenCalledTimes(2);
  expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
});

it("adopts only supported changed fields through the real retained writer without remounting", async () => {
  api.list.mockResolvedValue([suggestion]);
  api.apply.mockImplementation(async (current) => ({
    ...current,
    version: 2,
    etag: 'W/"sad_a:2"',
    definition: { ...current.definition, description: "Proposed description" },
  }));
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  const original = field();
  // The merged-in internal name is not a changed field, so it is not offered.
  expect(screen.getAllByRole("button", { name: "采用" })).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() =>
    expect(api.apply).toHaveBeenCalledWith(
      expect.objectContaining({ draft_id: "sad_a", version: 1 }),
      "sas_a",
      ["description"],
      expect.any(String),
    ),
  );
  expect(field()).toBe(original);
  expect(field()).toHaveValue("sad_a");
  await waitFor(() => expect(screen.getByRole("textbox", { name: /用途说明/ })).toHaveValue("Proposed description"));
  expect(api.save).not.toHaveBeenCalled();
});

it("autosaves each dirty object before one navigation and keeps the views when a failed save is kept", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  fireEvent.change(field(), { target: { value: "local-a" } });
  fireEvent.click(screen.getByText("Open B"));
  await waitFor(() => expect(field()).toHaveValue("sad_b"));
  // The second editor's save fails: only then does the shared close guard ask.
  api.save.mockImplementation(async (current: ManualSubagentDraftDetail, purpose: string, definition) => {
    if (current.draft_id === "sad_b") throw new SubagentDraftApiError(422, "subagent_draft_invalid", "Invalid");
    return { ...current, purpose, definition, version: current.version + 1, etag: `W/"${current.draft_id}:${current.version + 1}"` };
  });
  fireEvent.change(field(), { target: { value: "local-b" } });
  let allowed: boolean | undefined;
  act(() => {
    void confirmCanvasNavigation().then((value) => {
      allowed = value;
    });
  });
  const dialog = await screen.findByRole("dialog", { name: "放弃未保存的修改？" }, AUTOSAVE);
  expect(within(dialog).getByText("「local-b」有未保存的修改，关闭后会丢失。已保存的内容不受影响。")).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "继续编辑" }));
  await waitFor(() => expect(allowed).toBe(false));
  expect(screen.getAllByRole("tab")).toHaveLength(2);
  expect(field()).toHaveValue("local-b");
  expect(api.save).toHaveBeenCalledWith(expect.objectContaining({ draft_id: "sad_a" }), "Analyze evidence",
    expect.objectContaining({ display_name: "local-a" }), false);
});

it("allows adoption retry after a failed read without declaring an unknown write", async () => {
  api.list.mockResolvedValue([suggestion]);
  api.apply.mockImplementation(async (current) => ({
    ...current,
    version: 2,
    etag: 'W/"sad_a:2"',
    definition: { ...current.definition, description: "Proposed description" },
  }));
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  api.get.mockRejectedValueOnce(new TypeError("Network request failed"));
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  const failure = (await screen.findByText("建议未能应用。请检查最新草稿并重试。")).parentElement!;
  expect(api.apply).not.toHaveBeenCalled();
  expect(screen.queryByText(UNKNOWN_SAVE)).not.toBeInTheDocument();
  // The failed commit returns the rows to pending and retries with the same decisions.
  const retry = within(failure).getByRole("button", { name: "重试" });
  await waitFor(() => expect(retry).toBeEnabled());
  fireEvent.click(retry);
  await waitFor(() => expect(api.apply).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole("textbox", { name: /用途说明/ })).toHaveValue("Proposed description"));
  expect(api.save).not.toHaveBeenCalled();
});

it("blocks repeated adoption after an unconfirmed dispatched write", async () => {
  api.list.mockResolvedValue([suggestion]);
  api.apply.mockRejectedValueOnce(new TypeError("Network request failed"));
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await screen.findByText(UNKNOWN_SAVE);
  const failure = (await screen.findByText("建议未能应用。请检查最新草稿并重试。")).parentElement!;
  fireEvent.click(within(failure).getByRole("button", { name: "重试" }));
  await act(async () => {});
  expect(api.apply).toHaveBeenCalledTimes(1);
  expect(api.save).not.toHaveBeenCalled();
});

it("keeps uncertain adoption input intact when a background version arrives", async () => {
  api.list.mockResolvedValue([suggestion]);
  api.apply.mockRejectedValueOnce(new TypeError("Network request failed"));
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await screen.findByText(UNKNOWN_SAVE);
  const reads = api.get.mock.calls.length;
  api.get.mockResolvedValue({ ...draft("sad_a"), version: 3, definition: { ...draft("sad_a").definition, display_name: "remote-name" } });
  fireEvent.click(screen.getByText("Refresh A"));
  await act(async () => {});
  expect(field()).toHaveValue("sad_a");
  expect(api.get).toHaveBeenCalledTimes(reads);
});

it.each([403, 404])("clears private fields and Chat binding after canonical read denial (%s)", async (status) => {
  api.list.mockResolvedValue([suggestion]);
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  api.get.mockRejectedValueOnce(new SubagentDraftApiError(status, "subagent_draft_not_found", "Unavailable"));
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() => expect(screen.queryByRole("textbox", { name: "名称" })).not.toBeInTheDocument());
  expect(screen.getByTestId("selected-binding")).toHaveTextContent("none");
  expect(api.apply).not.toHaveBeenCalled();
  expect(api.save).not.toHaveBeenCalled();
});

it("updates the retained Tab title from an acknowledged display-name save", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  fireEvent.change(field(), { target: { value: "Renamed Agent" } });
  await waitFor(() => expect(api.save).toHaveBeenCalledOnce(), AUTOSAVE);
  fireEvent.click(screen.getByText("Open B"));
  await waitFor(() => expect(field()).toHaveValue("sad_b"));
  expect(screen.getByRole("tab", { name: "Renamed Agent" })).toBeInTheDocument();
  fireEvent.click(screen.getByText("Refresh A"));
  expect(screen.getByRole("tab", { name: "Renamed Agent" })).toBeInTheDocument();
});

it("keeps a readable draft after a field/action authorization rejection", async () => {
  api.list.mockResolvedValue([suggestion]);
  api.apply.mockRejectedValueOnce(new SubagentDraftApiError(403, "subagent_tool_not_selectable", "Unavailable action"));
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await screen.findByText("建议未能应用。请检查最新草稿并重试。");
  expect(field()).toHaveValue("sad_a");
  expect(screen.getByTestId("selected-binding")).toHaveTextContent("sad_a");
  expect(api.save).not.toHaveBeenCalled();
  expect(screen.queryByText(UNKNOWN_SAVE)).not.toBeInTheDocument();
});

it("isolates a delayed read denial from a closed and reopened controller", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  let rejectRead!: (reason: unknown) => void;
  api.get.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRead = reject; }));
  fireEvent.click(screen.getByRole("button", { name: "根据新的描述重新生成…" }));
  await waitFor(() => expect(rejectRead).toBeTypeOf("function"));
  fireEvent.click(screen.getByText("Reopen A"));
  await waitFor(() => expect(screen.getByRole("button", { name: "根据新的描述重新生成…" })).not.toBeDisabled());
  await act(async () => rejectRead(new SubagentDraftApiError(403, "subagent_draft_not_found", "Unavailable")));
  expect(field()).toHaveValue("sad_a");
  expect(screen.getByTestId("selected-binding")).toHaveTextContent("sad_a");
});

it("closes a clean inactive object from its tab × without moving the selected editor", async () => {
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await waitFor(() => expect(field()).toHaveValue("sad_a"));
  fireEvent.click(screen.getByText("Open B"));
  await waitFor(() => expect(field()).toHaveValue("sad_b"));
  fireEvent.click(screen.getAllByRole("button", { name: "关闭「sad_a」" })[0]);
  expect(field()).toHaveValue("sad_b");
  expect(screen.queryByRole("tab", { name: "sad_a" })).not.toBeInTheDocument();
  expect(api.save).not.toHaveBeenCalled();
});

it("protects input typed while adoption is reading the original saved baseline", async () => {
  api.list.mockResolvedValue([suggestion]);
  renderIntl(<Workbench />);
  fireEvent.click(screen.getByText("Open A"));
  await screen.findByRole("button", { name: "全部采用" });
  let finishRead!: (value: ManualSubagentDraftDetail) => void;
  api.get.mockReturnValueOnce(
    new Promise((resolve) => {
      finishRead = resolve;
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "全部采用" }));
  await waitFor(() => expect(finishRead).toBeTypeOf("function"));
  // Force the ordering at the controller boundary even though the visible controls are disabled.
  fireEvent.change(field(), { target: { value: "newer-local" } });
  await act(async () => {
    finishRead(draft("sad_a"));
  });
  expect(api.apply).not.toHaveBeenCalled();
  expect(field()).toHaveValue("newer-local");
});
