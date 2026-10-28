import { PRODUCT_NAME } from "@/lib/product-identity";
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { AgentAuthoringPluginSection, AgentAuthoringSkill } from "@/lib/agents-api";

import { ResourcePluginPicker, ResourceSkillPicker, ResourceSummary, ResourceToolPicker, pluginToolOwners, pluginsFromCatalog, toolGroups } from "./resource-picker";

vi.mock("@/lib/use-app-router", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/use-app-router")>(),
  useAppRouter: () => ({ push: vi.fn() }),
}));

const section: AgentAuthoringPluginSection = {
  builtin: [
    {
      id: "builtin:data-analysis",
      name: "data-analysis",
      version: 1,
      family: "general",
      listing: {
        "en-US": { name: "Data analysis", description: "Query data", use_when: "you need numbers from your data", example: "“Sales last month?”" },
        "zh-CN": { name: "数据分析", description: "查询数据", use_when: "要从数据源里查数时", example: "「上月销售额」" },
      },
      tools: ["bi_query", "bi_schema"],
      prompt_modules: ["data-query-datarc-protocol@1"],
      ready: true,
      unavailable_tools: [],
      selectable: true,
      locked_reason: null,
      team_disabled: false,
      my_disabled: false,
    },
    {
      id: "builtin:email",
      name: "email",
      version: 1,
      family: "general",
      listing: { "en-US": { name: "Email", description: "Send email", use_when: "results should be sent by email", example: "“Send it”" } },
      tools: ["send_email"],
      prompt_modules: [],
      ready: true,
      unavailable_tools: [],
      selectable: true,
      locked_reason: null,
      team_disabled: false,
      my_disabled: false,
    },
    {
      id: "builtin:settings-space-metadata",
      name: "settings-space-metadata",
      version: 3,
      family: "settings",
      listing: {},
      tools: [],
      prompt_modules: [],
      ready: true,
      unavailable_tools: [],
      selectable: false,
      locked_reason: "settings_assistant_only",
      team_disabled: false,
      my_disabled: false,
    },
    {
      id: "builtin:image-generation",
      name: "image-generation",
      version: 1,
      family: "general",
      listing: { "en-US": { name: "Image generation", description: "Images", use_when: "you want an image", example: "“Draw it”" } },
      tools: ["image_generate"],
      prompt_modules: [],
      ready: false,
      unavailable_tools: ["image_generate"],
      selectable: false,
      locked_reason: "not_ready",
      team_disabled: false,
      my_disabled: false,
    },
  ],
  team: [
    {
      id: "tcap_pack",
      name: "Store pack",
      description: "KPI queries",
      version: 3,
      tools: ["web_map"],
      skill_count: 1,
      ready: true,
      unavailable_tools: [],
      selectable: false,
      locked_reason: "disabled_by_team",
      team_disabled: true,
      my_disabled: false,
    },
  ],
};

describe("ResourcePluginPicker", () => {
  it("says when to use each Plugin, gives an example and what it adds", () => {
    const plugins = pluginsFromCatalog(section, "en-US");
    renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set()} onToggle={() => {}} />, { locale: "en-US" });
    expect(screen.getByText("Built-in Plugins")).toBeTruthy();
    expect(screen.getByText("Use when you need numbers from your data. For example: “Sales last month?”")).toBeTruthy();
    expect(screen.getByText(/^Adds: .* · Includes guidance$/)).toBeTruthy();
    // Selecting Email is the grant, so it is selectable like any other Plugin.
    const email = screen.getByRole("checkbox", { name: /Email/ }) as HTMLInputElement;
    expect(email.disabled).toBe(false);
  });

  it("hides settings Plugins always, and reasons only administrators can act on from members", () => {
    const plugins = pluginsFromCatalog(section, "en-US");
    const { unmount } = renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set()} onToggle={() => {}} />, { locale: "en-US" });
    expect(screen.queryByRole("checkbox", { name: /settings-space-metadata|Space/ })).toBeNull();
    expect(screen.queryByText("Image generation")).toBeNull();
    // With no visible team Plugin the team group is hidden.
    expect(screen.queryByText("Team Plugins")).toBeNull();
    unmount();
    renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set()} onToggle={() => {}} canManage />, { locale: "en-US" });
    expect(screen.getByText("Image generation")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Set it up" })).toBeTruthy();
    expect(screen.getByText("Team Plugins")).toBeTruthy();
  });

  it("marks Email not ready with its reason and links an administrator to the mail service", () => {
    const notReady: AgentAuthoringPluginSection = {
      ...section,
      builtin: section.builtin.map((item) => item.id === "builtin:email"
        ? { ...item, ready: false, unavailable_tools: ["send_email"], selectable: false, locked_reason: "not_ready" as const }
        : item),
    };
    renderIntl(<ResourcePluginPicker plugins={pluginsFromCatalog(notReady, "zh-CN")} selected={new Set()} onToggle={() => {}} canManage />);
    expect(screen.getByText(/未就绪：团队还没配置邮件服务，发不出邮件/)).toBeTruthy();
    expect(screen.getAllByRole("link", { name: "去配置" }).map((link) => link.getAttribute("href"))).toContain("/team/settings/email-service/");
  });

  it("states the consequence once Email is checked", () => {
    const plugins = pluginsFromCatalog(section, "zh-CN");
    renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set(["builtin:email"])} onToggle={() => {}} />);
    expect(screen.getByText("选上后，这个智能体会直接发邮件，不再每次问你。")).toBeTruthy();
  });

  it("keeps a selected Plugin that became unavailable removable", () => {
    const onToggle = vi.fn();
    const plugins = pluginsFromCatalog(section, "en-US");
    renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set(["tcap_pack"])} onToggle={onToggle} />, { locale: "en-US" });
    const pack = screen.getByRole("checkbox", { name: /Store pack/ }) as HTMLInputElement;
    expect(pack.checked).toBe(true);
    expect(pack.disabled).toBe(false);
    expect(screen.getByText(/Disabled by your team · Clear to remove/)).toBeTruthy();
    fireEvent.click(pack);
    expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ id: "tcap_pack", kind: "team" }));
  });

  it("filters by one search and shows a plain empty state", () => {
    const plugins = pluginsFromCatalog(section, "en-US");
    renderIntl(<ResourcePluginPicker plugins={plugins} selected={new Set()} onToggle={() => {}} query="sms" />, { locale: "en-US" });
    expect(screen.getByText("No Plugins, Tools or Skills match “sms”")).toBeTruthy();
    expect(screen.getByText("Try other words.")).toBeTruthy();
  });

  it("explains recommended picks in the compact creation draft", () => {
    const onToggle = vi.fn();
    const plugins = pluginsFromCatalog(section, "zh-CN");
    renderIntl(
      <ResourcePluginPicker plugins={plugins} selected={new Set(["builtin:data-analysis"])} recommended={new Set(["builtin:data-analysis"])} onToggle={onToggle} density="compact" />,
    );
    expect(screen.getByText("推荐")).toBeTruthy();
    expect(screen.getByText("适合：要从数据源里查数时。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "+ Email" }));
    expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ id: "builtin:email" }));
  });
});

describe("pluginToolOwners and ResourceSummary", () => {
  it("maps Tools to the selected Plugin that brings them", () => {
    const owners = pluginToolOwners(pluginsFromCatalog(section, "en-US"), new Set(["builtin:data-analysis"]));
    expect([...owners]).toEqual([
      ["bi_query", "Data analysis"],
      ["bi_schema", "Data analysis"],
    ]);
  });

  it("summarizes counts and attached guidance", () => {
    renderIntl(<ResourceSummary tools={4} skills={1} needsConfirmation={2} guidance={["Data analysis"]} />, { locale: "en-US" });
    expect(screen.getByText("What this Agent can do")).toBeTruthy();
    expect(screen.getByText(`Write outside ${PRODUCT_NAME} or delete`).nextElementSibling?.textContent).toBe("2");
    expect(screen.getByText("Data analysis")).toBeTruthy();
  });
});

describe("ResourceToolPicker", () => {
  const tools = [
    { ref: "bi_query", name: "Query data", description: "Pull rows", selectable: true },
    { ref: "web_search", name: "Web search", description: "Search the web", selectable: true },
    { ref: "send_email", name: "Send email", description: "Email people", selectable: false, lockedLabel: "Not available yet" },
  ];

  it("groups Tools like the Tools page and locks Tools a selected Plugin brings", () => {
    const onToggle = vi.fn();
    renderIntl(
      <ResourceToolPicker
        tools={tools}
        plugins={pluginsFromCatalog(section, "en-US")}
        selected={new Set(["send_email"])}
        selectedPluginIds={new Set(["builtin:data-analysis"])}
        onToggle={onToggle}
      />,
      { locale: "en-US" },
    );
    const query = screen.getByRole("checkbox", { name: /Query data/ }) as HTMLInputElement;
    expect(query.checked && query.disabled).toBe(true);
    expect(screen.getByText("Included in the Data analysis Plugin")).toBeTruthy();
    // A selected Tool that became unavailable stays removable.
    const email = screen.getByRole("checkbox", { name: /Send email/ }) as HTMLInputElement;
    expect(email.checked && !email.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox", { name: /Web search/ }));
    expect(onToggle).toHaveBeenCalledWith("web_search");
    expect(screen.getAllByRole("heading", { level: 4 }).length).toBeGreaterThanOrEqual(3);
  });

  it("falls back to Tool categories without a Plugin section", () => {
    expect(toolGroups([])("web_search")).toBe("web");
    expect(toolGroups(pluginsFromCatalog(section, "en-US"))("bi_query")).toBe("data");
  });
});

describe("ResourceSkillPicker", () => {
  const builtinSkill: AgentAuthoringSkill = {
    name: "analyst-sop-output-contract",
    display_name: "Analysis output contract",
    display_name_zh: "分析产出契约",
    description: "Pack a bounded Chat answer or a report AnalysisPayload via subagent_return.",
    description_en: "Turns finished analysis into a clear chat answer or a report.",
    description_zh: "把完成的分析整理成清晰的对话回答或报告。",
    description_de: "Bringt eine fertige Analyse in eine klare Chat-Antwort oder einen Bericht.",
    description_fr: "Met une analyse terminée sous forme de réponse claire ou de rapport.",
    source_kind: "builtin",
    linked_tools: [],
    allowed_audiences: ["private", "custom", "teamwide"],
    grant: { skill_ref: { id: "builtin:analyst-sop-output-contract" } } as AgentAuthoringSkill["grant"],
  };
  const teamSkill: AgentAuthoringSkill = {
    ...builtinSkill,
    name: "weekly-report",
    display_name: "Weekly report",
    description: "Our team's weekly report format.",
    source_kind: "team",
    grant: { skill_ref: { id: "oskill_weekly" } } as AgentAuthoringSkill["grant"],
  };

  it.each([
    ["zh-CN", "分析产出契约", builtinSkill.description_zh],
    ["en-US", "Analysis output contract", builtinSkill.description_en],
    ["de-DE", "Analysis output contract", builtinSkill.description_de],
    ["fr-FR", "Analysis output contract", builtinSkill.description_fr],
  ])("shows the %s listing text for built-ins and authored text for team Skills", (locale, title, description) => {
    renderIntl(<ResourceSkillPicker skills={[builtinSkill, teamSkill]} selected={new Set()} onToggle={() => {}} />, { locale: locale as "en-US" });
    expect(screen.getByText(title)).toBeTruthy();
    expect(screen.getByText(description!)).toBeTruthy();
    expect(screen.queryByText(/subagent_return/)).toBeNull();
    expect(screen.getByText("Our team's weekly report format.")).toBeTruthy();
  });
});
