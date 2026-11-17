import { createRef, useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { getAgentAuthoringCatalog, type AgentAuthoringCatalog, type AgentAuthoringTool, type AgentExactSkillGrant } from "@/lib/agents-api";
import { createTeamPlugin, listTeamPluginTemplates, listTeamPlugins, updateTeamPlugin, type TeamPlugin, type TeamPluginTemplate } from "@/lib/team-plugins-api";
import { confirmCanvasNavigation, registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { TeamPluginsContent } from "./team-plugins-content";

vi.mock("@/lib/agents-api", () => ({ getAgentAuthoringCatalog: vi.fn() }));
vi.mock("@/lib/team-plugins-api", () => ({ listTeamPlugins: vi.fn(), listTeamPluginTemplates: vi.fn(), createTeamPlugin: vi.fn(), updateTeamPlugin: vi.fn(), setTeamPluginEnabled: vi.fn(), deleteTeamPlugin: vi.fn() }));
const auth = vi.hoisted(() => ({ session: null as null | { me: { capabilities: Record<string, boolean> } } }));
vi.mock("@/lib/auth/auth-session-context", () => ({ useOptionalAuthSession: () => auth.session }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn(), back: vi.fn(), forward: vi.fn(), refresh: vi.fn() }) }));
const grant: AgentExactSkillGrant = { skill_ref: { kind: "builtin", id: "builtin:method", name: "method" }, permissions: ["read"], revision: `sha256:${"a".repeat(64)}`, distribution: "reference" };
const plugin: TeamPlugin = { id: "tcap_original", team_id: "team_1", name: "Original Plugin", description: "Original purpose", status: "active", revision: 1, row_version: 1, content_hash: "original", tool_refs: [], skill_grants: [grant], agent_count: 1 };
const newerGrant = { ...grant, revision: `sha256:${"b".repeat(64)}` };
const catalog = { tools: [], skills: [{ name: "method", description: "Method", source_kind: "builtin", grant: newerGrant, allowed_audiences: ["teamwide"] }] } as unknown as AgentAuthoringCatalog;
beforeEach(() => { vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(catalog); vi.mocked(listTeamPluginTemplates).mockResolvedValue([]); });
afterEach(() => { cleanup(); vi.clearAllMocks(); auth.session = null; });
function host() {
  return { plugin, readOnly: false, controllerRef: createRef<{ requestClose: () => void }>(), onClose: vi.fn(), onActivate: vi.fn(), onSaved: vi.fn() };
}
/** Hosted in Canvas the shared save bar (C9) appears once something changed; wait for the editable form. */
async function ready() {
  await waitFor(() => expect(getAgentAuthoringCatalog).toHaveBeenCalled());
  await waitFor(() => expect(screen.getByRole("textbox", { name: "名称" }).closest("fieldset")?.disabled).toBe(false));
}
const save = () => screen.getByRole("button", { name: "保存" });
async function answer(label: string) { const dialog = await screen.findByRole("dialog", { name: "放弃未保存的修改？" }); await act(async () => fireEvent.click(within(dialog).getByRole("button", { name: label }))); }

it("preserves captured Skill grants and advances only the acknowledged original Plugin baseline", async () => {
  const hosted = host();
  const saved = { ...plugin, name: "Updated Plugin", row_version: 2, revision: 2 };
  vi.mocked(updateTeamPlugin).mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error("Conflict"));
  renderIntl(<TeamPluginsContent hosted={hosted} />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: saved.name } });
  fireEvent.click(save());
  await waitFor(() => expect(hosted.onSaved).toHaveBeenCalledWith(saved));
  expect(updateTeamPlugin).toHaveBeenNthCalledWith(1, plugin.id, 1, { name: saved.name, description: plugin.description, tool_refs: [], skill_grants: [grant] });
  fireEvent.change(screen.getByRole("textbox", { name: "介绍" }), { target: { value: "Retain on conflict" } });
  fireEvent.click(save());
  await screen.findByRole("alert");
  expect(updateTeamPlugin).toHaveBeenNthCalledWith(2, plugin.id, 2, expect.objectContaining({ description: "Retain on conflict", skill_grants: [grant] }));
  expect((screen.getByRole("textbox", { name: "介绍" }) as HTMLTextAreaElement).value).toBe("Retain on conflict");
  expect(hosted.onClose).not.toHaveBeenCalled();
});

it("keeps current input and prevents a pending discard from approving newer edits", async () => {
  const hosted = host();
  let replace!: (value: typeof hosted) => void;
  function Workbench() { const [current, setCurrent] = useState(hosted); replace = setCurrent; return <TeamPluginsContent hosted={current} />; }
  renderIntl(<Workbench />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Local draft" } });
  act(() => replace({ ...hosted, plugin: { ...plugin, name: "New server label", revision: 3 } }));
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Local draft");
  const input = screen.getByRole("textbox", { name: "名称" });
  act(() => hosted.controllerRef.current?.requestClose());
  fireEvent.change(input, { target: { value: "Newer local draft" } });
  await answer("放弃修改");
  expect(hosted.onClose).not.toHaveBeenCalled();
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Newer local draft");
});

it("does not discard a Plugin buffer when another original editor cancels navigation", async () => {
  const hosted = host();
  renderIntl(<TeamPluginsContent hosted={hosted} />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Keep original input" } });
  const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved: () => true, confirmLeave: async () => false });
  try {
    let departure!: Promise<boolean>;
    act(() => { departure = confirmCanvasNavigation(); });
    await answer("放弃修改");
    expect(await departure).toBe(false);
    expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Keep original input");
    expect(hosted.onClose).not.toHaveBeenCalled();
  } finally { unregister(); }
});

it("blocks close and editing during an original save and ignores a disposed completion", async () => {
  const hosted = host();
  let finish!: (value: TeamPlugin) => void;
  vi.mocked(updateTeamPlugin).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const view = renderIntl(<TeamPluginsContent hosted={hosted} />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Pending save" } });
  fireEvent.click(save());
  expect(screen.getByRole("textbox", { name: "名称" }).closest("fieldset")?.disabled).toBe(true);
  await act(async () => hosted.controllerRef.current?.requestClose());
  expect(hosted.onClose).not.toHaveBeenCalled();
  view.unmount();
  await act(async () => finish({ ...plugin, name: "Pending save", row_version: 2, revision: 2 }));
  expect(hosted.onSaved).not.toHaveBeenCalled();
  expect(hosted.controllerRef.current).toBeNull();
});

it("shows authorized contents but cannot save after write access becomes read only", async () => {
  const hosted = host();
  let replace!: (value: typeof hosted) => void;
  function Workbench() { const [current, setCurrent] = useState(hosted); replace = setCurrent; return <TeamPluginsContent hosted={current} />; }
  renderIntl(<Workbench />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Retained text" } });
  act(() => replace({ ...hosted, readOnly: true }));
  expect(screen.getByRole("textbox", { name: "名称" }).closest("fieldset")?.disabled).toBe(true);
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Retained text");
  fireEvent.submit(screen.getByRole("textbox", { name: "名称" }).closest("form")!);
  expect(updateTeamPlugin).not.toHaveBeenCalled();
});

it("reads selected Plugin resources without requiring authoring-catalog permission", async () => {
  vi.mocked(getAgentAuthoringCatalog).mockRejectedValue(new Error("Authoring is unavailable"));
  const hosted = { ...host(), readOnly: true };
  renderIntl(<TeamPluginsContent hosted={hosted} />);
  expect(screen.getByText("method")).toBeTruthy();
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe(plugin.name);
  await act(async () => {});
  expect(getAgentAuthoringCatalog).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("shows the server's catalog copy for a coded save failure instead of a fixed message", async () => {
  const { readApiError } = await import("@/lib/api-client");
  const copy = "所选的部分 Skill 不是可用的已审核团队 Skill：它们已被移除、停用、正在等待重新审核，或是个人 Skill。请从插件中移除，或完成审核后重新保存。";
  // The gateway answers {detail: {code, message}} after adding the localized catalog message.
  const coded = await readApiError(new Response(JSON.stringify({ detail: { code: "capability_skill_unavailable", message: copy } }), { status: 422, headers: { "Content-Type": "application/json" } }));
  vi.mocked(updateTeamPlugin).mockRejectedValueOnce(coded).mockRejectedValueOnce(new Error("Failed to fetch"));
  renderIntl(<TeamPluginsContent hosted={host()} />);
  await ready();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Renamed" } });
  fireEvent.click(save());
  expect((await screen.findByRole("alert")).textContent).toContain(copy);
  // An uncoded failure keeps the generic save message rather than raw transport text.
  fireEvent.click(save());
  await waitFor(() => expect(updateTeamPlugin).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole("alert").textContent).not.toContain(copy));
  expect(screen.getByRole("alert").textContent).not.toContain("Failed to fetch");
});

function teamTool(operation: string, overrides: Partial<AgentAuthoringTool> = {}): AgentAuthoringTool {
  return {
    stable_ref: `teammcp.ots_pet.${operation}`, display_name: operation, description: `${operation} description`, source_kind: "team_mcp",
    source_display_name: "Petstore", connection_id: "ots_pet", capability_classes: [], risk: "read", selectable: true, locked_reason: null,
    approval_required: false, confirmation_summary: null, ...overrides,
  };
}
const builtinQuery: AgentAuthoringTool = {
  stable_ref: "bi_query", display_name: "Query data", description: "Query the data source", source_kind: "native", source_display_name: "Built-in tools",
  connection_id: null, capability_classes: [], risk: "read", selectable: true, locked_reason: null, approval_required: false, confirmation_summary: null,
};
function withTools(tools: AgentAuthoringTool[]) {
  vi.mocked(getAgentAuthoringCatalog).mockResolvedValue({ ...catalog, tools } as AgentAuthoringCatalog);
}
function hostWith(overrides: Partial<TeamPlugin>) {
  return { ...host(), plugin: { ...plugin, ...overrides } };
}
const groupOf = (title: string) => screen.getByText(title).closest("details, section") as HTMLElement;

it("groups Team tools by connection with operation names, counts and locked reasons", async () => {
  withTools([teamTool("getPetById"), teamTool("addPet", { risk: "external_write", selectable: false, locked_reason: "approval_executor_unavailable" }), builtinQuery]);
  renderIntl(<TeamPluginsContent hosted={hostWith({ tool_refs: ["teammcp.ots_pet.getPetById"] })} />);
  await ready();

  const petstore = groupOf("Petstore");
  expect(petstore.tagName).toBe("DETAILS");
  expect((petstore as HTMLDetailsElement).open).toBe(true);
  expect(within(petstore).getByText("已选 1/2")).toBeTruthy();
  expect(within(petstore).getByText("团队连接")).toBeTruthy();
  expect(within(petstore).getByText("getPetById")).toBeTruthy();
  expect(screen.queryByText("teammcp.ots_pet.getPetById")).toBeNull();
  const addPet = within(petstore).getByText("addPet").closest("label")!;
  expect(within(addPet).getByText("当前部署无法执行此工具所需的审批流程，暂不可新增；已选工具可移除。")).toBeTruthy();
  expect((within(addPet).getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
  // Locked rows come after the ones that can be chosen.
  const names = within(petstore).getAllByRole("checkbox").map((box) => box.closest("label")!.querySelector("strong")!.textContent);
  expect(names).toEqual(["getPetById", "addPet"]);

  const data = groupOf("数据");
  expect((data as HTMLDetailsElement).open).toBe(false);
  expect(within(data).getByText("内置工具")).toBeTruthy();
  expect(screen.getByText("已选 1 个工具、1 个技能")).toBeTruthy();
});

it("searches names and sources, says when nothing matches and keeps hidden selections", async () => {
  withTools([teamTool("getPetById"), builtinQuery]);
  renderIntl(<TeamPluginsContent hosted={hostWith({ tool_refs: ["bi_query"] })} />);
  await ready();
  const search = screen.getByRole("searchbox", { name: "搜索工具和技能" });

  fireEvent.change(search, { target: { value: "petstore" } });
  expect(groupOf("Petstore").tagName).toBe("SECTION");
  expect(screen.getByText("getPetById")).toBeTruthy();
  expect(screen.queryByText("数据")).toBeNull();
  expect(screen.getByText("已选 1 个工具、1 个技能")).toBeTruthy();

  fireEvent.change(search, { target: { value: "zzz" } });
  expect(screen.getByRole("status").textContent).toBe("没有匹配「zzz」的工具或技能。 换个说法试试。");

  fireEvent.change(search, { target: { value: "" } });
  expect(groupOf("Petstore").tagName).toBe("DETAILS");
});

it("links an uncategorized Team tool to Tools settings only when categories can be confirmed", async () => {
  withTools([teamTool("lookup", { risk: "unknown", selectable: false, locked_reason: "risk_metadata_unknown" })]);
  const view = renderIntl(<TeamPluginsContent hosted={host()} />);
  await ready();
  fireEvent.click(screen.getByText("Petstore"));
  expect(screen.getByText(/缺少可信风险标注/)).toBeTruthy();
  expect(screen.queryByRole("link", { name: "去工具设置确认类别" })).toBeNull();
  view.unmount();

  auth.session = { me: { capabilities: { team_tool_confirmations: true } } };
  renderIntl(<TeamPluginsContent hosted={host()} />);
  await waitFor(() => expect(screen.getByText("Petstore")).toBeTruthy());
  fireEvent.click(screen.getByText("Petstore"));
  expect(screen.getByRole("link", { name: "去工具设置确认类别" }).getAttribute("href")).toBe("/team/settings/tools/");
});

it("names a selection that is no longer available and lets it be removed", async () => {
  withTools([builtinQuery]);
  renderIntl(<TeamPluginsContent hosted={hostWith({ tool_refs: ["teammcp.ots_gone.removedOp"] })} />);
  await ready();
  const gone = groupOf("已失效的所选内容");
  const row = within(gone).getByText("removedOp").closest("label")!;
  expect(within(row).getByText("已不可用 · 取消勾选即可移除")).toBeTruthy();
  const box = within(row).getByRole("checkbox") as HTMLInputElement;
  expect(box.checked && !box.disabled).toBe(true);
  fireEvent.click(box);
  expect(screen.queryByText("已失效的所选内容")).toBeNull();
  expect(screen.getByText("已选 0 个工具、1 个技能")).toBeTruthy();
});

it("hides New Plugin while a form is open", async () => {
  vi.mocked(listTeamPlugins).mockResolvedValue([]);
  renderIntl(<TeamPluginsContent />);
  fireEvent.click(await screen.findByRole("button", { name: "新建插件" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "名称" })).toBeTruthy());
  expect(screen.queryByRole("button", { name: "新建插件" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(await screen.findByRole("button", { name: "新建插件" })).toBeTruthy();
});

it("groups read-only contents by kind with readable names", async () => {
  renderIntl(<TeamPluginsContent hosted={{ ...hostWith({ tool_refs: ["teammcp.ots_pet.getPetById", "bi_query"] }), readOnly: true }} />);
  expect(screen.getByRole("heading", { name: "团队工具" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "内置工具" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "技能" })).toBeTruthy();
  expect(screen.getByText("getPetById")).toBeTruthy();
  expect(screen.queryByText("teammcp.ots_pet.getPetById")).toBeNull();
  await act(async () => {});
  expect(getAgentAuthoringCatalog).not.toHaveBeenCalled();
});

const shopTemplate: TeamPluginTemplate = {
  id: "shopify_analysis:ibt_1", kind: "shopify_analysis", connection_id: "ibt_1", connection_name: "Demo",
  tool_refs: ["teammcp.ibt_1.shopify_shop_context"], skill_grants: [newerGrant],
};
const shopContext = teamTool("shopify_shop_context", { stable_ref: "teammcp.ibt_1.shopify_shop_context", source_display_name: "Shopify - Demo", connection_id: "ibt_1" });

async function openCreateForm() {
  vi.mocked(listTeamPlugins).mockResolvedValue([]);
  renderIntl(<TeamPluginsContent />);
  fireEvent.click(await screen.findByRole("button", { name: "新建插件" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "名称" })).toBeTruthy());
}

it("fills a new Plugin from a template, asks before replacing edits and saves through the ordinary create", async () => {
  withTools([shopContext]);
  vi.mocked(listTeamPluginTemplates).mockResolvedValue([shopTemplate]);
  vi.mocked(createTeamPlugin).mockResolvedValue({ ...plugin, id: "tcap_new" });
  await openCreateForm();
  const choice = await screen.findByRole("combobox", { name: "从模板开始" });
  expect(within(choice).getByRole("option", { name: "Shopify 经营分析 · Demo" })).toBeTruthy();

  fireEvent.change(choice, { target: { value: shopTemplate.id } });
  await waitFor(() => expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Shopify 经营分析 - Demo"));
  expect((screen.getByRole("textbox", { name: "介绍" }) as HTMLTextAreaElement).value).toBe("只读分析该店铺的订单、商品、库存和店铺内容。");
  expect(screen.getByText("已选 1 个工具、1 个技能")).toBeTruthy();
  expect((groupOf("Shopify - Demo") as HTMLDetailsElement).open).toBe(true);

  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Store desk" } });
  fireEvent.change(choice, { target: { value: "" } });
  const dialog = await screen.findByRole("dialog", { name: "替换已填写的内容？" });
  await act(async () => fireEvent.click(within(dialog).getByRole("button", { name: "取消" })));
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Store desk");

  fireEvent.click(screen.getByRole("button", { name: "保存插件" }));
  await waitFor(() => expect(createTeamPlugin).toHaveBeenCalledWith({
    name: "Store desk", description: "只读分析该店铺的订单、商品、库存和店铺内容。", tool_refs: shopTemplate.tool_refs, skill_grants: [newerGrant],
  }));
});

it("says when templates could not load and retries without blocking a blank Plugin", async () => {
  vi.mocked(listTeamPluginTemplates).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce([shopTemplate]);
  withTools([shopContext]);
  await openCreateForm();
  const status = await screen.findByText(/模板暂时无法加载。/);
  expect(screen.getByRole("textbox", { name: "名称" })).toBeTruthy();
  fireEvent.click(within(status).getByRole("button", { name: "重试" }));
  expect(await screen.findByRole("combobox", { name: "从模板开始" })).toBeTruthy();
  expect(listTeamPluginTemplates).toHaveBeenCalledTimes(2);
});

it("offers templates only when creating on the page", async () => {
  vi.mocked(listTeamPluginTemplates).mockResolvedValue([shopTemplate]);
  renderIntl(<TeamPluginsContent hosted={host()} />);
  await ready();
  expect(screen.queryByRole("combobox", { name: "从模板开始" })).toBeNull();
  expect(listTeamPluginTemplates).not.toHaveBeenCalled();
});

it("reports a failed save beside Save and keeps what was entered", async () => {
  const { readApiError } = await import("@/lib/api-client");
  const copy = "服务器正忙于其他任务，这次插件操作没有执行，也没有丢失任何内容。请几秒后重试。";
  const busy = await readApiError(new Response(JSON.stringify({ detail: { code: "capability_store_busy", message: copy } }), { status: 503, headers: { "Content-Type": "application/json" } }));
  vi.mocked(createTeamPlugin).mockRejectedValueOnce(busy);
  withTools([builtinQuery]);
  await openCreateForm();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Store desk" } });
  fireEvent.click(within(screen.getByText("Query data").closest("label")!).getByRole("checkbox"));
  fireEvent.click(screen.getByRole("button", { name: "保存插件" }));

  // The form can be taller than the window, so the message sits next to the button that was clicked.
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe(copy);
  expect(alert.closest("form")).not.toBeNull();
  expect(alert.nextElementSibling?.contains(screen.getByRole("button", { name: "保存插件" }))).toBe(true);
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Store desk");
});

it("lists Plugins before the Tools and Skills catalog arrives and keeps the form waiting for it", async () => {
  // On 116 the catalog once took 9.8 s, and the list used to wait for it.
  let deliver!: (value: AgentAuthoringCatalog) => void;
  vi.mocked(getAgentAuthoringCatalog).mockImplementationOnce(() => new Promise((resolve) => (deliver = resolve)));
  vi.mocked(listTeamPlugins).mockResolvedValue([plugin]);
  renderIntl(<TeamPluginsContent />);
  fireEvent.click(await screen.findByRole("button", { name: "Original Plugin" }));

  expect(screen.getByText("正在加载可选的工具和技能…")).toBeTruthy();
  // Nothing is called unavailable before the catalog says so, and nothing can be saved yet.
  expect(screen.queryByText("已失效的所选内容")).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: "名称" }), { target: { value: "Renamed" } });
  expect((screen.getByRole("button", { name: "保存插件" }) as HTMLButtonElement).disabled).toBe(true);

  await act(async () => deliver(catalog));
  expect(screen.queryByText("正在加载可选的工具和技能…")).toBeNull();
  expect((screen.getByRole("button", { name: "保存插件" }) as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value).toBe("Renamed");
});

it("keeps the list when the catalog fails and retries it inside the form", async () => {
  vi.mocked(getAgentAuthoringCatalog).mockRejectedValueOnce(new Error("offline"));
  vi.mocked(listTeamPlugins).mockResolvedValue([plugin]);
  renderIntl(<TeamPluginsContent />);
  fireEvent.click(await screen.findByRole("button", { name: "Original Plugin" }));

  const failure = await screen.findByRole("alert");
  expect(failure.textContent).toContain("可选的工具和技能暂时无法加载。已填写的内容会保留。");
  expect(failure.closest("form")).not.toBeNull();
  fireEvent.click(within(failure).getByRole("button", { name: "重试" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(getAgentAuthoringCatalog).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("正在加载可选的工具和技能…")).toBeNull();
});
