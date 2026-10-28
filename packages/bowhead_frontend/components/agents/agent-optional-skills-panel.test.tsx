import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderIntl } from "@/__tests__/intl-render";
import { getAgentOptionalSkillCatalog } from "@/lib/agents-api";
import { emptyRuntimeExtensions, type AgentRuntimeExtensions, type OptionalSkillItem } from "@/lib/agent-runtime-extensions";
import { AgentOptionalSkillsPanel } from "./agent-optional-skills-panel";

vi.mock("@/lib/agents-api", () => ({ getAgentOptionalSkillCatalog: vi.fn() }));
const changed = vi.fn();
const version = `sha256:${"a".repeat(64)}`;
const items: OptionalSkillItem[] = (["builtin", "team", "user"] as const).map((source) => ({
  resource: { kind: "skill", source_kind: source, id: source === "builtin" ? "builtin:dupont" : `${source}_dupont`, version },
  label: "杜邦分析", description: "分析盈利能力", allowed_audiences: source === "user" ? ["private"] : ["private", "custom", "teamwide"],
}));
function response(rows = items, next: number | null = null, agentId = "osa_1") {
  return { kind: "agent_optional_skill_catalog", schema_version: 1, agent_id: agentId, items: rows, next_offset: next };
}
function initial(): AgentRuntimeExtensions {
  const policy = emptyRuntimeExtensions();
  policy.skill_read.allowed_sources = ["builtin", "team", "user"];
  policy.skill_read.resource_allowlist = [{ source_kind: "user", id: "user_dupont", version }];
  policy.skill_read.enabled = true;
  return policy;
}
function Harness({ initialValue, disabled = false }: { initialValue?: unknown; disabled?: boolean }) {
  const [value, setValue] = useState(initialValue);
  const [agent, setAgent] = useState("osa_1");
  const [audience, setAudience] = useState<"private" | "teamwide">("private");
  return <>
    <button onClick={() => setAgent("osa_2")}>Switch Agent</button>
    <button onClick={() => setAudience("teamwide")}>Share</button>
    <AgentOptionalSkillsPanel key={agent} agentId={agent} value={value} disabled={disabled} audience={audience}
      onChange={(next) => { changed(next); setValue(next); }} />
  </>;
}

describe("Agent optional methods", () => {
  beforeEach(() => {
    changed.mockReset();
    vi.mocked(getAgentOptionalSkillCatalog).mockReset().mockResolvedValue(response());
  });
  afterEach(cleanup);

  it("configures an exact personal method with explicit enable and task limit", async () => {
    renderIntl(<Harness />);
    expect(getAgentOptionalSkillCatalog).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "配置可选方法" }));
    const choices = await screen.findAllByRole("checkbox", { name: /杜邦分析/ });
    expect(choices).toHaveLength(3);
    fireEvent.click(choices[2]);
    fireEvent.click(screen.getByRole("checkbox", { name: "允许用户为单个任务添加方法" }));
    fireEvent.change(screen.getByRole("combobox", { name: "每个任务最多选择" }), { target: { value: "2" } });
    const policy = changed.mock.lastCall![0] as AgentRuntimeExtensions;
    expect(policy.skill_read.resource_allowlist).toEqual([{ source_kind: "user", id: "user_dupont", version }]);
    expect(policy.skill_read.enabled).toBe(true);
    expect(policy.skill_read.max_selected).toBe(2);
    expect(policy.skill_read.selection_sources).toEqual(["user_explicit"]);
    expect(policy.skill_read.require_read_only_contract).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    expect(screen.getByRole("alert").textContent).toContain("个人方法只能配置给使用范围为“仅自己”的智能体");
    expect(screen.getByRole("checkbox", {
      name: /杜邦分析/, description: /当前智能体的使用范围为“全团队”/,
    }).hasAttribute("disabled")).toBe(false);
    expect(changed.mock.lastCall![0]).toEqual(policy);
  });

  it("opens and closes an untouched method picker without changing the draft", async () => {
    renderIntl(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "配置可选方法" }));
    await screen.findAllByRole("checkbox", { name: /杜邦分析/ });
    expect(changed).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "清除可选方法策略" }));

    expect(changed).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "配置可选方法" })).toBeTruthy();
  });

  it("preserves an older pin until the Owner explicitly selects the current version", async () => {
    const policy = initial();
    policy.skill_read.resource_allowlist[0].version = `sha256:${"b".repeat(64)}`;
    renderIntl(<Harness initialValue={policy} />);
    const update = await screen.findByRole("button", { name: "使用当前版本" });
    expect(changed).not.toHaveBeenCalled();
    fireEvent.click(update);
    expect(changed.mock.lastCall![0].skill_read.resource_allowlist[0].version).toBe(version);
    expect(policy.skill_read.resource_allowlist[0].version).toBe(`sha256:${"b".repeat(64)}`);
  });

  it("keeps unpinned rules unpinned when changing only the limit", async () => {
    const policy = initial();
    delete policy.skill_read.resource_allowlist[0].version;
    renderIntl(<Harness initialValue={policy} />);
    await screen.findAllByRole("checkbox", { name: /杜邦分析/ });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1" } });
    expect(changed.mock.lastCall![0].skill_read.resource_allowlist).toEqual([{ source_kind: "user", id: "user_dupont" }]);
  });

  it("keeps selection on a catalog failure and allows removing the last method", async () => {
    vi.mocked(getAgentOptionalSkillCatalog).mockRejectedValue(new Error("unavailable"));
    renderIntl(<Harness initialValue={initial()} />);
    await screen.findByRole("alert");
    expect(changed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "移除 已选方法 1" }));
    expect(changed.mock.lastCall![0].skill_read.resource_allowlist).toEqual([]);
    expect(changed.mock.lastCall![0].skill_read.enabled).toBe(false);
  });

  it("loads another page while preserving distinct exact sources", async () => {
    vi.mocked(getAgentOptionalSkillCatalog).mockResolvedValueOnce(response([items[0]], 1)).mockResolvedValueOnce(response(items.slice(1)));
    renderIntl(<Harness initialValue={emptyRuntimeExtensions()} />);
    fireEvent.click(await screen.findByRole("button", { name: "加载更多方法" }));
    await waitFor(() => expect(screen.getAllByRole("checkbox", { name: /杜邦分析/ })).toHaveLength(3));
    expect(vi.mocked(getAgentOptionalSkillCatalog).mock.calls[1][2]).toBe(1);
  });

  it("discards an old query response after a newer search resolves", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(getAgentOptionalSkillCatalog).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(response([{ ...items[0], label: "New Method" }]));
    renderIntl(<Harness initialValue={emptyRuntimeExtensions()} />);
    await waitFor(() => expect(getAgentOptionalSkillCatalog).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "New" } });
    await screen.findByRole("checkbox", { name: /New Method/ });
    await act(async () => resolveOld(response([{ ...items[0], label: "Old Method" }])));
    expect(screen.queryByText("Old Method")).toBeNull();
    expect(screen.getByRole("checkbox", { name: /New Method/ })).toBeTruthy();
    expect(changed).not.toHaveBeenCalled();
  });

  it("rejects a changed version across pages without offering the rejected version", async () => {
    vi.mocked(getAgentOptionalSkillCatalog).mockResolvedValueOnce(response([items[2]], 1)).mockResolvedValueOnce(response([
      { ...items[2], resource: { ...items[2].resource, version: `sha256:${"b".repeat(64)}` } },
    ]));
    renderIntl(<Harness initialValue={initial()} />);
    fireEvent.click(await screen.findByRole("button", { name: "加载更多方法" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "使用当前版本" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /杜邦分析/ })).toBeNull();
    expect(changed).not.toHaveBeenCalled();
  });

  it("discards the previous Agent response and only presents the current Agent's catalog", async () => {
    let resolveOld!: (value: unknown) => void;
    vi.mocked(getAgentOptionalSkillCatalog).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce(response([{ ...items[1], label: "Current Agent Method" }], null, "osa_2"));
    renderIntl(<Harness initialValue={emptyRuntimeExtensions()} />);
    await waitFor(() => expect(getAgentOptionalSkillCatalog).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Switch Agent" }));
    await screen.findByRole("checkbox", { name: /Current Agent Method/ });
    await act(async () => resolveOld(response()));
    expect(screen.queryByText("杜邦分析")).toBeNull();
  });

  it("preserves unsupported policies until explicit clear", () => {
    const policy = { schema_version: 99, future: { preserve: true } };
    renderIntl(<Harness initialValue={policy} />);
    expect(getAgentOptionalSkillCatalog).not.toHaveBeenCalled();
    expect(changed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "清除可选方法策略" }));
    expect(changed).toHaveBeenCalledWith(null);
    expect(policy).toEqual({ schema_version: 99, future: { preserve: true } });
  });

  it("cannot mutate a read-only Agent or start catalog requests for it", () => {
    renderIntl(<Harness initialValue={initial()} disabled />);
    const panel = screen.getByText("可选只读方法").closest(".am-optional-skills")!;
    for (const button of within(panel as HTMLElement).getAllByRole("button")) expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "移除 已选方法 1" }));
    expect(changed).not.toHaveBeenCalled();
    expect(getAgentOptionalSkillCatalog).not.toHaveBeenCalled();
  });
});
