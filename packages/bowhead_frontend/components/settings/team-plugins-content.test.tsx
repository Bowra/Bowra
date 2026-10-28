import { createRef, useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { getAgentAuthoringCatalog, type AgentAuthoringCatalog, type AgentExactSkillGrant } from "@/lib/agents-api";
import { updateTeamPlugin, type TeamPlugin } from "@/lib/team-plugins-api";
import { confirmCanvasNavigation, registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { TeamPluginsContent } from "./team-plugins-content";

vi.mock("@/lib/agents-api", () => ({ getAgentAuthoringCatalog: vi.fn() }));
vi.mock("@/lib/team-plugins-api", () => ({ listTeamPlugins: vi.fn(), createTeamPlugin: vi.fn(), updateTeamPlugin: vi.fn(), setTeamPluginEnabled: vi.fn(), deleteTeamPlugin: vi.fn() }));
const grant: AgentExactSkillGrant = { skill_ref: { kind: "builtin", id: "builtin:method", name: "method" }, permissions: ["read"], revision: `sha256:${"a".repeat(64)}`, distribution: "reference" };
const plugin: TeamPlugin = { id: "tcap_original", team_id: "team_1", name: "Original Plugin", description: "Original purpose", status: "active", revision: 1, row_version: 1, content_hash: "original", tool_refs: [], skill_grants: [grant], agent_count: 1 };
const newerGrant = { ...grant, revision: `sha256:${"b".repeat(64)}` };
const catalog = { tools: [], skills: [{ name: "method", description: "Method", source_kind: "builtin", grant: newerGrant, allowed_audiences: ["teamwide"] }] } as unknown as AgentAuthoringCatalog;
beforeEach(() => { vi.mocked(getAgentAuthoringCatalog).mockResolvedValue(catalog); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });
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
