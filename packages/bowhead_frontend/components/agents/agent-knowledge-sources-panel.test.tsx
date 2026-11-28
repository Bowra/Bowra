import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { getAgentKnowledgeBaseSources, getAgentKnowledgeSources } from "@/lib/agents-api";
import type { KnowledgeBaseSourceCatalog } from "@/lib/agent-knowledge-sources";
import { AgentKnowledgeSourcesPanel } from "./agent-knowledge-sources-panel";

const session = vi.hoisted(() => ({ knowledgeBases: false }));
vi.mock("@/lib/agents-api", () => ({ getAgentKnowledgeSources: vi.fn(), getAgentKnowledgeBaseSources: vi.fn() }));
vi.mock("@/lib/auth/auth-session-context", () => ({
  useOptionalAuthSession: () => ({ status: "authenticated", me: { id: "u_me", capabilities: { knowledge_bases: session.knowledgeBases } } }),
}));
const changed = vi.fn();
const selected = { schema_version: 1, mode: "selected_files", file_ids: ["kbf_one"] };
const catalog = { enabled: true, available: true, items: [{ id: "kbf_one", name: "Source One", scope: "personal" }], selected_items: [{ id: "kbf_one", name: "Source One", scope: "personal" }], total: 1, max_selected: 64, top_k_min: 1, top_k_max: 50 };
const MANUAL = "kbase_" + "a".repeat(24);
const PAYROLL = "kbase_" + "b".repeat(24);
const bases: KnowledgeBaseSourceCatalog = {
  enabled: true,
  state: "ready",
  max_selected: 32,
  selected_items: [],
  groups: [
    { home: "personal", home_id: "u_me", name: null, items: [{ id: "kbase_" + "1".repeat(24), is_default: true, home: "personal", home_id: "u_me", name: null, audience: "owner", audience_level: "use" }] },
    {
      home: "space", home_id: "ws_store", name: "门店", items: [
        { id: "kbase_" + "2".repeat(24), is_default: true, home: "space", home_id: "ws_store", name: null, audience: "space", audience_level: "edit" },
        { id: MANUAL, is_default: false, home: "space", home_id: "ws_store", name: "门店运营手册", audience: "space", audience_level: "use" },
      ],
    },
    { home: "team", home_id: "team_1", name: null, items: [{ id: PAYROLL, is_default: false, home: "team", home_id: "team_1", name: "公司制度", audience: "team", audience_level: "use" }] },
  ],
};
function Harness({ initial = selected }: { initial?: unknown }) {
  const [value, setValue] = useState(initial);
  return <AgentKnowledgeSourcesPanel agentId="osa_1" value={value} disabled={false} onChange={(next) => { changed(next); setValue(next); }} />;
}
const modes = () => within(screen.getByTestId("knowledge-source-modes"));
const radio = (name: RegExp) => modes().getByRole("radio", { name });
describe("Knowledge source selection", () => {
  beforeEach(() => {
    session.knowledgeBases = false;
    changed.mockReset();
    vi.mocked(getAgentKnowledgeSources).mockReset().mockResolvedValue(catalog);
    vi.mocked(getAgentKnowledgeBaseSources).mockReset().mockResolvedValue(bases);
  });
  afterEach(cleanup);
  it("keeps removing the last source distinct from broadening to current context", async () => {
    renderIntl(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: "移除 Source One" }));
    expect(changed.mock.lastCall![0]).toEqual({ ...selected, file_ids: [] });
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "context_readable" } });
    expect(changed.mock.lastCall![0]).toEqual({ schema_version: 1, mode: "context_readable" });
    // With knowledge bases off nothing new is offered or requested.
    expect(screen.queryByRole("option", { name: "指定知识库" })).toBeNull();
    expect(getAgentKnowledgeBaseSources).not.toHaveBeenCalled();
  });
  it("preserves unavailable identities without exposing their names", async () => {
    vi.mocked(getAgentKnowledgeSources).mockResolvedValue({ ...catalog, items: [], selected_items: [] });
    renderIntl(<Harness />);
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    expect(screen.getByText("第 1 个已选来源当前不可用")).toBeTruthy();
    expect(screen.queryByText("Source One")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("ignores stale search responses", async () => {
    let complete!: (value: typeof catalog) => void;
    vi.mocked(getAgentKnowledgeSources).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; })).mockResolvedValueOnce({ ...catalog, items: [], selected_items: [] });
    renderIntl(<Harness />);
    await waitFor(() => expect(getAgentKnowledgeSources).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "new" } });
    await waitFor(() => expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(false));
    await act(async () => complete(catalog));
    expect(screen.queryByText("Source One")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });
  it("explains unavailable source services while preserving the selection", async () => {
    vi.mocked(getAgentKnowledgeSources).mockResolvedValue({ ...catalog, available: false, items: [], selected_items: [] });
    renderIntl(<Harness />);
    expect((await screen.findByRole("alert")).textContent).toContain("无法加载来源");
    expect((screen.getByRole("combobox") as HTMLSelectElement).disabled).toBe(true);
    expect(changed).not.toHaveBeenCalled();
  });
  it("keeps unknown policy fields unchanged", async () => {
    renderIntl(<Harness initial={{ ...selected, future: true }} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });

  describe("selected knowledge bases", () => {
    beforeEach(() => { session.knowledgeBases = true; });
    it("offers the mode and lists usable knowledge bases grouped My / each Space / Team", async () => {
      renderIntl(<Harness initial={{ schema_version: 1, mode: "context_readable", defaults: { top_k: 4 } }} />);
      await waitFor(() => expect(radio(/指定知识库/).getAttribute("aria-disabled") === "true" || (radio(/指定知识库/) as HTMLButtonElement).disabled).toBe(false));
      expect(modes().getAllByRole("radio")).toHaveLength(3);
      fireEvent.click(radio(/指定知识库/));
      expect(changed.mock.lastCall![0]).toEqual({ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [], defaults: { top_k: 4 } });
      expect(screen.getByText("还没有选择知识库。选择后，此智能体才能读取其中的知识。")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "添加知识库" }));
      const picker = within(screen.getByTestId("knowledge-base-picker"));
      expect(picker.getAllByRole("group").map((group) => group.querySelector("legend")!.textContent)).toEqual(["我的知识库", "空间「门店」", "团队"]);
      // Defaults are named after their home: My knowledge, the Space's name; named ones keep their names.
      expect(picker.getByRole("checkbox", { name: /我的知识/ })).toBeTruthy();
      expect(picker.getByRole("checkbox", { name: /^门店默认/ })).toBeTruthy();
      fireEvent.click(picker.getByRole("checkbox", { name: /门店运营手册/ }));
      fireEvent.click(picker.getByRole("checkbox", { name: /公司制度/ }));
      expect(changed.mock.lastCall![0]).toEqual({ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [MANUAL, PAYROLL].sort(), defaults: { top_k: 4 } });
      const row = within(screen.getByTestId(`knowledge-base-selected-${MANUAL}`));
      expect(row.getByText("空间「门店」")).toBeTruthy();
      expect(row.getByText("空间成员可使用")).toBeTruthy();
      expect(screen.getByText(/只会检索到这些知识库里他自己也能使用的部分/)).toBeTruthy();
      // Leaving the mode drops the knowledge base field and returns to schema 1.
      fireEvent.click(radio(/指定文件/));
      expect(changed.mock.lastCall![0]).toEqual({ schema_version: 1, mode: "selected_files", file_ids: [], defaults: { top_k: 4 } });
    });
    it("stops at the selection limit", async () => {
      vi.mocked(getAgentKnowledgeBaseSources).mockResolvedValue({ ...bases, max_selected: 2 });
      renderIntl(<Harness initial={{ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [MANUAL, PAYROLL] }} />);
      await waitFor(() => expect((screen.getByRole("button", { name: "添加知识库" }) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(screen.getByRole("button", { name: "添加知识库" }));
      const picker = within(screen.getByTestId("knowledge-base-picker"));
      expect(picker.getByText("最多可以选择 2 个知识库。要添加其他知识库，请先移除一个。")).toBeTruthy();
      expect((picker.getByRole("checkbox", { name: /我的知识/ }) as HTMLInputElement).disabled).toBe(true);
      expect((picker.getByRole("checkbox", { name: /门店运营手册/ }) as HTMLInputElement).disabled).toBe(false);
      expect(screen.getByText("已选择 2 个知识库（最多 2 个）")).toBeTruthy();
    });
    it("disables the mode with the reason before the Team's knowledge bases are prepared", async () => {
      vi.mocked(getAgentKnowledgeBaseSources).mockResolvedValue({ ...bases, state: "preparing" });
      renderIntl(<Harness initial={{ schema_version: 1, mode: "context_readable" }} />);
      const reason = await screen.findByText(/知识库正在为智能体做准备/);
      const option = radio(/指定知识库/);
      expect((option as HTMLButtonElement).disabled || option.getAttribute("aria-disabled") === "true").toBe(true);
      expect(option.getAttribute("aria-describedby")).toBe(reason.id);
      fireEvent.click(screen.getByRole("button", { name: "重新检查" }));
      await waitFor(() => expect(getAgentKnowledgeBaseSources).toHaveBeenCalledTimes(2));
    });
    it("keeps a saved selection visible with the switch off, says it does not apply, and lets the owner leave it", async () => {
      session.knowledgeBases = false;
      vi.mocked(getAgentKnowledgeBaseSources).mockResolvedValue({ enabled: false, state: "disabled", max_selected: 32, groups: [], selected_items: [] });
      renderIntl(<Harness initial={{ schema_version: 2, mode: "selected_knowledge_bases", knowledge_base_ids: [MANUAL] }} />);
      expect(await screen.findByText(/知识库功能没有开启/)).toBeTruthy();
      expect(screen.getByText("第 1 个已选知识库当前不可用")).toBeTruthy();
      expect(radio(/指定知识库/).getAttribute("aria-checked")).toBe("true");
      await waitFor(() => expect((radio(/当前上下文中可读的知识/) as HTMLButtonElement).disabled).toBe(false));
      fireEvent.click(radio(/当前上下文中可读的知识/));
      expect(changed.mock.lastCall![0]).toEqual({ schema_version: 1, mode: "context_readable" });
    });
  });
});
