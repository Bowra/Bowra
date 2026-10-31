import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chooseRowAction, menuItemNames, openRowMenu } from "@/__tests__/row-actions";
import zh from "@/messages/zh-CN.json";
import en from "@/messages/en-US.json";
import type { BuiltinSubagent } from "@/lib/team-subagents-api";

const putMock = vi.fn();
const clearMock = vi.fn();
const skillsMock = vi.fn();
const listMock = vi.fn();
vi.mock("@/lib/team-subagents-api", () => ({
  putBuiltinSubagentOverride: (...a: unknown[]) => putMock(...a),
  clearBuiltinSubagentOverride: (...a: unknown[]) => clearMock(...a),
  getSkillsCatalog: (...a: unknown[]) => skillsMock(...a),
  listBuiltinSubagents: (...a: unknown[]) => listMock(...a),
}));

import { BuiltinSubagentCards } from "./builtin-subagents-section";

function item(over: Partial<BuiltinSubagent> = {}): BuiltinSubagent {
  return {
    name: "consultant",
    enabled: true,
    model: "analyst",
    description: "默认路由描述",
    display_name: "",
    max_iterations: 100,
    primary_capability: "cap_presentation",
    primary_capability_options: ["cap_analysis", "cap_data_query", "cap_presentation"],
    skills: [],
    prompt_append: "",
    defaults: {
      model: "analyst",
      description: "默认路由描述",
      max_iterations: 100,
      primary_capability: "cap_presentation",
    },
    overridden_fields: [],
    override: null,
    row_version: 0,
    etag: '"builtin:consultant:0"',
    disable_warning: null,
    ...over,
  };
}

function renderCards(items: BuiltinSubagent[], manageable = true, onItemChanged = vi.fn(), onItemRemoved = vi.fn()) {
  render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <BuiltinSubagentCards items={items} manageable={manageable} onItemChanged={onItemChanged} onItemRemoved={onItemRemoved} />
    </NextIntlClientProvider>,
  );
  return onItemChanged;
}

const MENU = /的更多操作$/;
/** Enablement and configuration live in the row ⋯ (polish spec 2d); the row itself only shows state. */
const disable = () => chooseRowAction(MENU, "停用");
const configure = () => chooseRowAction(MENU, "配置");

describe("BuiltinSubagentCards", () => {
  beforeEach(() => {
    putMock.mockReset().mockImplementation(async (name: string) => item({ name, enabled: false, overridden_fields: ["enabled"], row_version: 1 }));
    clearMock.mockReset().mockImplementation(async (name: string) => item({ name }));
    skillsMock.mockReset().mockResolvedValue({ builtin: [{ name: "pptx", description: "deck" }], team_skills: [] });
    listMock.mockReset().mockResolvedValue({ items: [item()], manageable: true });
  });
  afterEach(cleanup);

  it("renders one settings row per builtin: name, purpose, skill count and a disabled badge instead of greying out", () => {
    renderCards([item(), item({ name: "compute", enabled: false, skills: ["sop", "pptx"] })]);
    const consultant = screen.getByTestId("builtin-row-consultant");
    const compute = screen.getByTestId("builtin-row-compute");
    expect(within(consultant).queryByText("已停用")).toBeNull();
    expect(within(compute).getByText("已停用")).toBeInTheDocument();
    // Zero skills is a state the owner should see, not hidden.
    expect(within(consultant).getByText("未配置技能")).toHaveAttribute("data-empty", "true");
    expect(within(compute).getByText("2 个技能")).not.toHaveAttribute("data-empty");
    // The slug and model role stay out of the row (they are in the configuration details).
    expect(within(consultant).queryByText("consultant")).toBeNull();
    expect(within(consultant).queryByText(/analyst/)).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  it("names the built-in configuration Agent 配置智能体 with its purpose", () => {
    renderCards([item({ name: "configuration-agent", display_name: "Configuration Agent" })]);
    expect(screen.getByRole("heading", { name: "配置智能体" })).toBeInTheDocument();
    expect(screen.getByText(zh.agents_marketplace.system_configuration_agent_summary)).toBeInTheDocument();
  });

  it("names the team's actual model in Details with the tier as secondary text", async () => {
    renderCards([item({ model_display_name: "deepseek-v4-pro" })], false);
    await chooseRowAction(MENU, "详情");
    const dialog = await screen.findByRole("dialog");
    const model = within(dialog).getByText("模型").nextElementSibling;
    expect(model).toHaveTextContent("deepseek-v4-pro");
    expect(model).toHaveTextContent("分析");
  });

  it("shows the model and a copyable identifier in the configuration details", async () => {
    renderCards([item()]);
    await configure();
    const details = await screen.findByRole("region", { name: "详情" });
    // The model is the localized tier label, never the routing value "analyst".
    expect(within(details).getByText("模型").nextElementSibling).toHaveTextContent("分析");
    expect(within(details).queryByText("analyst")).toBeNull();
    expect(within(details).getByText("consultant").tagName).toBe("CODE");
    expect(within(details).getByRole("button", { name: "复制标识" })).toBeInTheDocument();
  });

  it("localizes system names while retaining administrator display-name overrides", () => {
    const custom = item({ name: "consultant", display_name: "团队顾问", overridden_fields: ["display_name"] });
    render(<NextIntlClientProvider locale="en-US" messages={en}>
      <BuiltinSubagentCards items={[item({ name: "compute", display_name: "主分析" }), custom]} manageable={false} onItemChanged={vi.fn()} onItemRemoved={vi.fn()} />
    </NextIntlClientProvider>);
    // Built-in names are the Marketplace display names (one source with the Marketplace and the chat trace).
    expect(screen.getByRole("heading", { name: en.agents_marketplace.system_compute_name })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "团队顾问" })).toBeInTheDocument();
    cleanup();
    renderCards([item({ name: "compute", display_name: "Data Analyst" })]);
    expect(screen.getByRole("heading", { name: zh.agents_marketplace.system_compute_name })).toBeInTheDocument();
  });

  it("shows localized purpose summaries with contextual configuration actions", () => {
    render(<NextIntlClientProvider locale="en-US" messages={en}>
      <BuiltinSubagentCards items={[item(), item({ name: "compute" })]} manageable onItemChanged={vi.fn()} onItemRemoved={vi.fn()} />
    </NextIntlClientProvider>);
    expect(screen.getByText(en.agents_marketplace.system_consultant_summary)).toBeInTheDocument();
    expect(screen.getByText(en.agents_marketplace.system_compute_summary)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `More actions for ${en.agents_marketplace.system_consultant_name}` })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `More actions for ${en.agents_marketplace.system_compute_name}` })).toBeInTheDocument();
    expect(screen.getAllByText("No skills")).toHaveLength(2);
  });

  it("retries a failed skill catalog without losing the configuration draft or selected skills", async () => {
    skillsMock.mockRejectedValueOnce(new Error("Private network diagnostics"));
    renderCards([item({ override: { skills: ["team-sop"], display_name: null, description: null, model: null, prompt_append: null, max_iterations: null } })]);
    await configure();
    fireEvent.change(await screen.findByLabelText("追加团队口径"), { target: { value: "Keep this draft" } });
    expect(await screen.findByRole("alert")).toHaveTextContent(zh.settings.subagents.builtin.skills_load_failed);
    expect(screen.getByRole("checkbox", { name: "team-sop" })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "重试加载技能" }));
    expect(await screen.findByRole("checkbox", { name: /pptx/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "team-sop" })).toBeChecked();
    expect(screen.getByLabelText("追加团队口径")).toHaveValue("Keep this draft");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(putMock.mock.calls[0][1]).toMatchObject({ skills: ["team-sop"], prompt_append: "Keep this draft" });
  });

  it("lets read-only viewers open the model and identifier from the name", async () => {
    renderCards([item()], false);
    const name = zh.agents_marketplace.system_consultant_name;
    fireEvent.click(screen.getByRole("button", { name }));
    const dialog = await screen.findByRole("dialog", { name });
    expect(within(dialog).getByText("模型").nextElementSibling).toHaveTextContent("分析");
    expect(within(dialog).getByText("consultant").tagName).toBe("CODE");
    expect(within(dialog).queryByRole("button", { name: "保存" })).toBeNull();
  });

  it("offers read-only Details in the row menu to viewers and managers alike", async () => {
    renderCards([item()], false);
    expect(menuItemNames(await openRowMenu(MENU))).toEqual(["详情"]);
    cleanup();
    renderCards([item()], true);
    expect(menuItemNames(await openRowMenu(MENU))).toEqual(["配置", "详情", "停用"]);
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    await chooseRowAction(MENU, "详情");
    const dialog = await screen.findByRole("dialog", { name: zh.agents_marketplace.system_consultant_name });
    expect(within(dialog).getByText("consultant").tagName).toBe("CODE");
    expect(within(dialog).getByRole("button", { name: "复制标识" })).toBeInTheDocument();
  });

  it("keeps internal routing text, tier codes and built-in skill descriptions out of the configuration dialog", async () => {
    skillsMock.mockResolvedValue({
      builtin: [{ name: "pptx", description: "Use when the model must emit a deck" }],
      team_skills: [{ name: "team-sop", display_name: "Weekly review SOP", description: "How our team reviews weekly numbers" }],
    });
    renderCards([item()]);
    await configure();
    const description = await screen.findByLabelText("路由描述");
    expect(description).toHaveAttribute("placeholder", zh.settings.subagents.builtin.placeholder_description);
    const tiers = within(screen.getByLabelText("模型档位")).getAllByRole("option").map((option) => option.textContent);
    expect(tiers).toEqual(["跟随系统默认（分析）", "分析", "写作", "数据建模", "轻量任务"]);
    expect(await screen.findByText("Weekly review SOP")).toBeInTheDocument();
    expect(screen.getByText("How our team reviews weekly numbers")).toBeInTheDocument();
    expect(screen.queryByText("Use when the model must emit a deck")).toBeNull();
  });

  it("hides every write affordance when not manageable", () => {
    renderCards([item({ overridden_fields: ["model"] })], false);
    expect(screen.queryByRole("button", { name: /^配置 / })).toBeNull();
    expect(screen.queryByRole("switch")).toBeNull();
    // Read-only still shows effective values and changed marker.
    expect(screen.getByText("已改动")).toBeInTheDocument();
  });

  it("offers configure and enablement on every row and reset only for customized rows", async () => {
    renderCards([item()]);
    expect(menuItemNames(await openRowMenu(MENU))).toEqual(["配置", "详情", "停用"]);
    cleanup();
    renderCards([item({ enabled: false, overridden_fields: ["description"] })]);
    expect(menuItemNames(await openRowMenu(MENU))).toEqual(["配置", "详情", "启用", "恢复默认"]);
  });

  it("toggles without confirmation when disabling has no team-wide consequence", async () => {
    const onChanged = renderCards([item()]);
    await disable();
    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    expect(putMock.mock.calls[0][1]).toMatchObject({ enabled: false });
    expect(putMock.mock.calls[0][2]).toBe('"builtin:consultant:0"'); // If-Match uses list etag.
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("keeps the row menu focusable but closed to other writes until its save finishes", async () => {
    let resolveSave: ((next: BuiltinSubagent) => void) | undefined;
    putMock.mockReturnValueOnce(new Promise((resolve) => { resolveSave = resolve; }));
    renderCards([item()]);
    await disable();
    const trigger = screen.getByRole("button", { name: MENU });
    expect(trigger).toHaveAttribute("aria-busy", "true");
    expect(trigger).not.toBeDisabled();
    fireEvent.click(trigger);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(putMock).toHaveBeenCalledTimes(1);
    resolveSave?.(item({ enabled: false }));
    await waitFor(() => expect(trigger).not.toHaveAttribute("aria-busy"));
  });

  it("keeps enablement unchanged when a consequence confirmation is canceled", async () => {
    renderCards([item({ name: "compute", disable_warning: "disables_data_analysis" })]);
    await disable();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(putMock).not.toHaveBeenCalled();
    expect(screen.queryByText("已停用")).toBeNull();
  });

  it("removes a stale card and explains when its built-in was deleted", async () => {
    const onRemoved = vi.fn();
    putMock.mockRejectedValueOnce(Object.assign(new Error("该系统内置智能体已移除，已从当前列表隐藏。"), {
      code: "builtin_subagent_removed",
    }));
    renderCards([item({ name: "canvas-writer" })], true, vi.fn(), onRemoved);

    await disable();

    await waitFor(() => expect(onRemoved).toHaveBeenCalledWith("canvas-writer"));
    expect(screen.getByText("该系统内置智能体已移除，已从当前列表隐藏。")).toBeInTheDocument();
  });

  it("requires confirmation before disabling a capability the team would lose", async () => {
    renderCards([item({ name: "compute", disable_warning: "disables_data_analysis" })]);
    await disable();
    // Cannot write directly; must first see consequence explanation.
    expect(putMock).not.toHaveBeenCalled();
    expect(screen.getByText(/主力数据分析智能体/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
  });

  it("carries the existing delta through an enable/disable write (PUT is a full state, not a patch)", async () => {
    renderCards([
      item({
        enabled: true,
        model: "narrative",
        description: "团队描述",
        skills: ["sop"],
        overridden_fields: ["model", "description", "skills"],
        override: {
          display_name: null,
          description: "团队描述",
          model: "narrative",
          skills: ["sop"],
          prompt_append: null,
        },
      }),
    ]);
    await disable();
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    // Toggle only enabled, preserving every other overridden field; otherwise toggling erases admin configuration.
    expect(putMock.mock.calls[0][1]).toEqual({
      enabled: false,
      display_name: null,
      description: "团队描述",
      model: "narrative",
      primary_capability: null,
      prompt_append: null,
      skills: ["sop"],
    });
  });

  it("writes back the RAW delta, never the sanitized effective value", async () => {
    // review P1-2: effective description already sanitized (inducing language→placeholder, NFKC/whitespace normalization).
    // Writing effective value back permanently replaces admin original with sanitized result on a toggle.
    renderCards([
      item({
        description: "(描述含诱导措辞, 已隐藏)", // Effective value: neutralized display text.
        overridden_fields: ["description"],
        override: {
          display_name: null,
          description: "团队原文：尽调优先", // Raw delta: actual admin input.
          model: null,
          skills: null,
          prompt_append: null,
        },
      }),
    ]);
    await disable();
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(putMock.mock.calls[0][1].description).toBe("团队原文：尽调优先");
  });

  it("resets to default by clearing the delta", async () => {
    renderCards([item({ overridden_fields: ["model"] })]);
    await chooseRowAction(MENU, "恢复默认");
    await waitFor(() => expect(clearMock).toHaveBeenCalledWith("consultant"));
  });

  it("opens the config dialog with unchanged fields left empty so they keep following the default", async () => {
    renderCards([item({ description: "默认路由描述", overridden_fields: [] })]);
    await configure();
    const desc = await screen.findByLabelText("路由描述");
    // No override → empty input, default only placeholder. Filling default freezes override on opening,
    // preventing future manifest upgrades from propagating.
    expect(desc).toHaveValue("");
    // The system routing text is model-facing; the placeholder only explains what empty means.
    expect(desc).toHaveAttribute("placeholder", zh.settings.subagents.builtin.placeholder_description);
  });

  it("prefills the config dialog from an existing override", async () => {
    renderCards([
      item({
        description: "团队描述",
        model: "narrative",
        overridden_fields: ["description", "model"],
        override: {
          display_name: null,
          description: "团队描述",
          model: "narrative",
          skills: null,
          prompt_append: null,
        },
      }),
    ]);
    await configure();
    expect(await screen.findByLabelText("路由描述")).toHaveValue("团队描述");
    expect(screen.getByLabelText("模型档位")).toHaveValue("narrative");
  });

  it("exposes exactly the whitelisted fields and nothing else", async () => {
    renderCards([item()]);
    await configure();
    await screen.findByLabelText("路由描述");
    // Closed assertion, not searching a few absent labels (always green, proves nothing).
    // Enumerate all dialog labeled controls and compare exact allowlist; any extra field (especially tools / return contract /
    // callable peers / name carrying I2+I3 invariants/payload contracts) fails.
    const labelled = Array.from(document.querySelectorAll("[data-settings-dialog-portal] label"))
      .map((el) => el.textContent?.trim() ?? "")
      .filter((text) => text.length > 0 && !text.includes("pptx")); // Exclude skill checkboxes.
    expect(new Set(labelled)).toEqual(new Set(["显示名称", "路由描述", "模型档位", "主要职责", "自动阅读的技能", "追加团队口径"]));
    expect(screen.getByRole("option", { name: "跟随系统默认（制作演示文稿）" })).toBeInTheDocument();
    expect(screen.queryByText(/cap_presentation/)).not.toBeInTheDocument();
  });

  it("shows only the fields the Agent's manifest lets a Team change", async () => {
    const coach = item({
      name: "agent-coach",
      etag: '"builtin:agent-coach:0"',
      team_overrides: ["enabled", "display_name", "description"],
    });
    renderCards([coach]);
    expect(screen.getByRole("heading", { name: "Coach" })).toBeInTheDocument();
    expect(screen.getByTestId("builtin-row-agent-coach").querySelector(".builtin-agent-skill-count")).toBeNull();
    await configure();
    await screen.findByLabelText("路由描述");
    const labelled = Array.from(document.querySelectorAll("[data-settings-dialog-portal] label"))
      .map((el) => el.textContent?.trim() ?? "")
      .filter((text) => text.length > 0);
    expect(new Set(labelled)).toEqual(new Set(["显示名称", "路由描述"]));
    expect(skillsMock).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "教练" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(putMock.mock.calls[0][1]).toEqual({
      enabled: true,
      display_name: "教练",
      description: null,
      model: null,
      primary_capability: null,
      prompt_append: null,
      skills: null,
    });
  });

  it("saves the dialog draft as a full override body", async () => {
    renderCards([item()]);
    await configure();
    fireEvent.change(await screen.findByLabelText("路由描述"), { target: { value: "只接尽调" } });
    fireEvent.change(screen.getByLabelText("模型档位"), { target: { value: "narrative" } });
    fireEvent.change(screen.getByLabelText("主要职责"), { target: { value: "cap_data_query" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(putMock.mock.calls[0][1]).toMatchObject({
      description: "只接尽调",
      model: "narrative",
      primary_capability: "cap_data_query",
    });
  });

  it("offers only proven responsibilities and explains stale overrides", async () => {
    renderCards([
      item({
        override: {
          display_name: null,
          description: null,
          model: null,
          skills: null,
          prompt_append: null,
          primary_capability: "cap_visualize",
        },
        overridden_fields: ["primary_capability"],
      }),
    ]);
    await configure();
    const select = await screen.findByLabelText("主要职责");
    expect(select).toHaveValue("cap_visualize");
    expect(screen.getByRole("option", { name: "原职责缺少能力依据，请重新选择" })).toBeDisabled();
    expect(select.querySelector('option[value="cap_compute"]')).toBeNull();
    fireEvent.change(select, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(putMock.mock.calls[0][1].primary_capability).toBeNull();
  });

  it("surfaces a write failure instead of silently keeping the old value", async () => {
    putMock.mockRejectedValueOnce(new Error("409 版本冲突"));
    renderCards([item()]);
    await disable();
    expect(await screen.findByText(/409 版本冲突/)).toBeInTheDocument();
  });

  it("reloads the latest delta and retries a stale enable/disable write once", async () => {
    const stale = item({
      override: {
        display_name: null,
        description: "旧描述",
        model: null,
        skills: null,
        prompt_append: null,
      },
    });
    const latest = item({
      description: "新描述",
      etag: '"builtin:consultant:2"',
      row_version: 2,
      override: {
        display_name: null,
        description: "新描述",
        model: "narrative",
        skills: null,
        prompt_append: null,
      },
    });
    putMock
      .mockRejectedValueOnce(Object.assign(new Error("版本冲突"), { code: "builtin_override_version_conflict" }))
      .mockResolvedValueOnce(item({ enabled: false, row_version: 3, etag: '"builtin:consultant:3"' }));
    listMock.mockResolvedValueOnce({ items: [latest], manageable: true });
    const onChanged = renderCards([stale]);

    await disable();

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(2));
    expect(putMock.mock.calls[1][1]).toMatchObject({
      enabled: false,
      description: "新描述",
      model: "narrative",
    });
    expect(putMock.mock.calls[1][2]).toBe('"builtin:consultant:2"');
    expect(onChanged).toHaveBeenCalled();
  });
});
